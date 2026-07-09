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
            "gztime": "2026-07-09 11:30", "nav_date": None,
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


if __name__ == "__main__":
    unittest.main()
