# -*- coding: utf-8 -*-
"""backend.app 的组合汇总与单条富集纯函数单元测试。

覆盖 M3 组合看板增强抽出的两个纯函数：
  enrich_holding(h, quote) —— 单条持仓 + 行情 → 富集后的展示项
  summarize(items)         —— 富集项列表 → 组合总览汇总

不发起网络请求、不读数据库，纯数值计算。
"""
import unittest

from backend.app import enrich_holding, summarize


class TestEnrichHolding(unittest.TestCase):
    """enrich_holding 直接调用真实函数，校验各派生字段。"""

    def _holding(self, **kw):
        base = {
            "id": 1, "user_id": 0, "fund_code": "020608",
            "hold_amount": None, "cost_amount": None, "target_rate": None,
            "target_price": None, "stop_profit": None, "stop_loss": None,
            "created_at": "2026-07-09 10:00",
        }
        base.update(kw)
        return base

    def _quote(self, **kw):
        base = {
            "fund_code": "020608", "name": "南方中证机器人ETF发起联接C",
            "dwjz": 1.0000, "gsz": 1.0500, "gszzl": 5.0,
            "gztime": "2026-07-09 11:30", "nav": None, "nav_date": None,
            "updated_at": "2026-07-09 11:30",
        }
        base.update(kw)
        return base

    def test_known_sample(self):
        # 持仓 10000、成本 8500、止盈线 5%、目标净值 1.80、目标收益率 15%
        # dwjz=1.0 gsz=1.05 → shares=10000, today_pl=500, est_value=10500
        # cost_return_rate=(10500-8500)/8500*100=23.53
        h = self._holding(hold_amount=10000.0, cost_amount=8500.0,
                          stop_profit=5.0, target_price=1.80, target_rate=15.0)
        item = enrich_holding(h, self._quote())

        self.assertEqual(item["name"], "南方中证机器人ETF发起联接C")
        self.assertEqual(item["gszzl"], 5.0)
        self.assertEqual(item["today_pl"], 500.0)
        self.assertEqual(item["est_value"], 10500.0)
        self.assertEqual(item["gap_to_target"], round(1.80 - 1.05, 4))
        self.assertEqual(item["cost_return_rate"], 23.53)
        self.assertTrue(item["hit_stop_profit"])
        self.assertEqual(item["gap_to_target_rate"], round(15.0 - 23.53, 2))

    def test_stop_loss_triggers(self):
        # 亏损：dwjz=2.0 gsz=1.90 成本 5200,止损线 -5%
        # shares=2500, est_value=4750, cost_return_rate=(4750-5200)/5200*100=-8.65
        h = self._holding(hold_amount=5000.0, cost_amount=5200.0, stop_loss=-5.0)
        item = enrich_holding(h, self._quote(dwjz=2.0, gsz=1.90, gszzl=-5.0))

        self.assertEqual(item["est_value"], 4750.0)
        self.assertLess(item["cost_return_rate"], 0)
        self.assertTrue(item["hit_stop_loss"])

    def test_no_quote_leaves_base_only(self):
        h = self._holding(hold_amount=10000.0, cost_amount=8500.0)
        item = enrich_holding(h, None)

        self.assertEqual(item["fund_code"], "020608")
        self.assertNotIn("today_pl", item)
        self.assertNotIn("est_value", item)

    def test_no_hold_amount_no_pl(self):
        # 只加自选未录金额：不应算出 today_pl / est_value
        h = self._holding()
        item = enrich_holding(h, self._quote())

        self.assertEqual(item["gszzl"], 5.0)  # 涨幅仍展示
        self.assertNotIn("today_pl", item)
        self.assertNotIn("est_value", item)

    def test_real_pl_from_nav(self):
        # 有官方净值 nav：算出真实盈亏,与估算并存
        # dwjz=1.0 nav=1.03 gsz=1.05 hold=10000 → shares=10000
        # real_value=10300, real_pl=300; est_value=10500, today_pl=500
        h = self._holding(hold_amount=10000.0, cost_amount=8500.0)
        item = enrich_holding(h, self._quote(nav=1.03, nav_date="2026-07-08"))

        # 估算口径仍在
        self.assertEqual(item["est_value"], 10500.0)
        self.assertEqual(item["today_pl"], 500.0)
        # 真实口径新增
        self.assertEqual(item["nav"], 1.03)
        self.assertEqual(item["nav_date"], "2026-07-08")
        self.assertEqual(item["real_value"], 10300.0)
        self.assertEqual(item["real_pl"], 300.0)
        self.assertEqual(item["real_return_rate"], round((10300.0 - 8500.0) / 8500.0 * 100, 2))

    def test_no_nav_no_real_fields(self):
        # 无 nav：不产生真实字段,优雅降级
        h = self._holding(hold_amount=10000.0, cost_amount=8500.0)
        item = enrich_holding(h, self._quote(nav=None))

        self.assertIn("est_value", item)
        self.assertNotIn("real_value", item)
        self.assertNotIn("real_pl", item)
        self.assertNotIn("real_return_rate", item)

    def test_stop_profit_uses_real_rate_when_nav_present(self):
        # 止盈改用真实收益率:估算收益率 23.53% 触发,但真实收益率仅 3%
        # nav=1.0 → real_value=cost 附近,real_return_rate≈2.94% < 止盈线5% → 不触发
        # dwjz=1.0 hold=10000 cost=8500 nav=1.0 → real_value=10000
        # real_return_rate=(10000-8500)/8500*100=17.65 —— 仍>5,换更贴近的数
        # 用 nav=0.90: real_value=9000, real_return_rate=(9000-8500)/8500*100=5.88>5 触发
        # 反例:nav=0.85: real_value=8500, rate=0 <5 不触发,而估算 est 10500 rate=23.53>5
        h = self._holding(hold_amount=10000.0, cost_amount=8500.0, stop_profit=5.0)
        item = enrich_holding(h, self._quote(nav=0.85, nav_date="2026-07-08"))

        # 估算口径收益率高(23.53%)但真实口径为 0% → 应以真实口径判定,不触发止盈
        self.assertEqual(item["cost_return_rate"], 23.53)
        self.assertEqual(item["real_return_rate"], 0.0)
        self.assertFalse(item["hit_stop_profit"])

    def test_stop_profit_triggers_on_real_rate(self):
        # 真实收益率达标 → 触发止盈
        h = self._holding(hold_amount=10000.0, cost_amount=8500.0, stop_profit=5.0)
        item = enrich_holding(h, self._quote(nav=0.90, nav_date="2026-07-08"))
        # real_value=9000, real_return_rate=5.88 ≥ 5 → 触发
        self.assertTrue(item["hit_stop_profit"])


class TestSummarize(unittest.TestCase):
    """summarize 直接喂富集项 dict，独立于 enrich_holding。"""

    def test_empty(self):
        s = summarize([])
        self.assertEqual(s["count"], 0)
        self.assertEqual(s["total_today_pl"], 0)
        self.assertEqual(s["total_est_value"], 0)
        self.assertEqual(s["total_cost"], 0)
        self.assertEqual(s["matched_count"], 0)
        self.assertIsNone(s["total_pl"])
        self.assertIsNone(s["total_return_rate"])
        # 真实口径:空组合归零/None
        self.assertEqual(s["total_real_value"], 0)
        self.assertIsNone(s["total_real_pl"])

    def test_two_holdings(self):
        items = [
            {"est_value": 10500.0, "cost_amount": 8500.0, "today_pl": 500.0},
            {"est_value": 4900.0, "cost_amount": 5200.0, "today_pl": -100.0},
        ]
        s = summarize(items)
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["total_today_pl"], 400.0)
        self.assertEqual(s["total_est_value"], 15400.0)
        self.assertEqual(s["total_cost"], 13700.0)
        self.assertEqual(s["matched_count"], 2)
        self.assertEqual(s["total_pl"], 1700.0)
        self.assertEqual(s["total_return_rate"], round(1700.0 / 13700.0 * 100, 2))

    def test_mixed_missing_cost_only_counts_matched(self):
        # 第二笔无成本：累计盈亏只计第一笔,但总市值/今日盈亏仍全计
        items = [
            {"est_value": 10500.0, "cost_amount": 8500.0, "today_pl": 500.0},
            {"est_value": 3000.0, "cost_amount": None, "today_pl": 50.0},
        ]
        s = summarize(items)
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["total_today_pl"], 550.0)
        self.assertEqual(s["total_est_value"], 13500.0)  # 两笔市值都计
        self.assertEqual(s["total_cost"], 8500.0)        # 仅匹配笔
        self.assertEqual(s["matched_count"], 1)
        self.assertEqual(s["total_pl"], 2000.0)          # 10500-8500
        self.assertEqual(s["total_return_rate"], round(2000.0 / 8500.0 * 100, 2))

    def test_holding_without_est_value_ignored_in_totals(self):
        # 只加自选没金额：不进任何金额累加,但计入 count
        items = [
            {"est_value": 10500.0, "cost_amount": 8500.0, "today_pl": 500.0},
            {"cost_amount": None, "today_pl": None},
        ]
        s = summarize(items)
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["total_today_pl"], 500.0)
        self.assertEqual(s["total_est_value"], 10500.0)
        self.assertEqual(s["matched_count"], 1)

    def test_real_totals(self):
        # 两笔有 nav 真实市值/盈亏,第三笔无 nav 不计入真实汇总
        items = [
            {"est_value": 10500.0, "cost_amount": 8500.0, "today_pl": 500.0,
             "real_value": 10300.0, "real_pl": 300.0},
            {"est_value": 4900.0, "cost_amount": 5200.0, "today_pl": -100.0,
             "real_value": 4800.0, "real_pl": -200.0},
            {"est_value": 3000.0, "cost_amount": None, "today_pl": 50.0},  # 无 real_*
        ]
        s = summarize(items)
        self.assertEqual(s["total_real_value"], 15100.0)   # 10300+4800
        self.assertEqual(s["total_real_pl"], 100.0)         # 300-200

    def test_real_totals_none_when_no_nav(self):
        items = [
            {"est_value": 10500.0, "cost_amount": 8500.0, "today_pl": 500.0},
        ]
        s = summarize(items)
        self.assertEqual(s["total_real_value"], 0)
        self.assertIsNone(s["total_real_pl"])


if __name__ == "__main__":
    unittest.main()
