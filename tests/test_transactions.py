# -*- coding: utf-8 -*-
"""交易流水记录测试 —— 重点覆盖 compute_position 的加权成本推导，

以及流水增删查按 user_id 隔离、鉴权。

沿用 test_auth.py 的「临时 DB 文件 + monkeypatch db.DB_PATH」手法，不起真实 HTTP。
"""
import os
import tempfile
import unittest

from backend.models import db
from backend.api import transactions as tx
from backend.api._router import Ctx


class TransactionTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        self._tmp_path = path
        self._orig_path = db.DB_PATH
        db.DB_PATH = path
        db.init_db(with_seed=False)

    def tearDown(self):
        db.DB_PATH = self._orig_path
        if os.path.exists(self._tmp_path):
            os.remove(self._tmp_path)


class TestComputePosition(TransactionTestBase):
    """compute_position 纯函数：TDD 重点。"""

    def _buy(self, user_id, code, shares, price, trade_date, amount=None):
        tx.add_transaction(
            {
                "fund_code": code,
                "action": "buy",
                "shares": shares,
                "price": price,
                "amount": amount,
                "trade_date": trade_date,
            },
            user_id,
        )

    def _sell(self, user_id, code, shares, price, trade_date, amount=None):
        tx.add_transaction(
            {
                "fund_code": code,
                "action": "sell",
                "shares": shares,
                "price": price,
                "amount": amount,
                "trade_date": trade_date,
            },
            user_id,
        )

    def test_empty_transactions_returns_zero(self):
        pos = tx.compute_position("020608", 1)
        # 契约随「已实现盈亏」升级新增 realized_pnl / has_tx 两键(向后兼容,只增不改)
        self.assertEqual(pos["shares"], 0.0)
        self.assertEqual(pos["cost_amount"], 0.0)
        self.assertEqual(pos["avg_cost"], 0.0)
        self.assertEqual(pos["realized_pnl"], 0.0)
        self.assertFalse(pos["has_tx"])

    def test_single_buy(self):
        self._buy(1, "020608", 100, 1.0, "2026-01-01")  # amount 推导 = 100
        pos = tx.compute_position("020608", 1)
        self.assertAlmostEqual(pos["shares"], 100.0)
        self.assertAlmostEqual(pos["cost_amount"], 100.0)
        self.assertAlmostEqual(pos["avg_cost"], 1.0)

    def test_multiple_buys_weighted_cost(self):
        # 100 份 @1.0（成本100） + 100 份 @1.2（成本120）→ 200 份，成本220，均价1.1
        self._buy(1, "020608", 100, 1.0, "2026-01-01")
        self._buy(1, "020608", 100, 1.2, "2026-01-05")
        pos = tx.compute_position("020608", 1)
        self.assertAlmostEqual(pos["shares"], 200.0)
        self.assertAlmostEqual(pos["cost_amount"], 220.0)
        self.assertAlmostEqual(pos["avg_cost"], 1.1)

    def test_buy_then_partial_sell_keeps_unit_cost(self):
        # 买 200 份成本 220（均价1.1），卖 50 份：份额剩150，成本按比例结转
        # 冲减成本 = 均价1.1 * 50 = 55；剩余成本 = 220-55=165；均价仍 1.1
        self._buy(1, "020608", 100, 1.0, "2026-01-01")
        self._buy(1, "020608", 100, 1.2, "2026-01-05")
        self._sell(1, "020608", 50, 1.5, "2026-02-01")  # 卖出价不影响成本冲减
        pos = tx.compute_position("020608", 1)
        self.assertAlmostEqual(pos["shares"], 150.0)
        self.assertAlmostEqual(pos["cost_amount"], 165.0)
        self.assertAlmostEqual(pos["avg_cost"], 1.1)

    def test_sell_all_shares_zeroes_position(self):
        self._buy(1, "020608", 100, 1.0, "2026-01-01")
        self._sell(1, "020608", 100, 1.3, "2026-02-01")
        pos = tx.compute_position("020608", 1)
        self.assertEqual(pos["shares"], 0.0)
        self.assertEqual(pos["cost_amount"], 0.0)
        self.assertEqual(pos["avg_cost"], 0.0)

    def test_multiple_sells_sequential(self):
        # 买 300 份成本 300（均价1.0），先卖100（剩200，成本200），再卖50（剩150，成本150）
        self._buy(1, "020608", 300, 1.0, "2026-01-01")
        self._sell(1, "020608", 100, 1.1, "2026-02-01")
        self._sell(1, "020608", 50, 1.2, "2026-03-01")
        pos = tx.compute_position("020608", 1)
        self.assertAlmostEqual(pos["shares"], 150.0)
        self.assertAlmostEqual(pos["cost_amount"], 150.0)
        self.assertAlmostEqual(pos["avg_cost"], 1.0)

    def test_oversell_clamped_to_held_shares(self):
        # 策略：卖出份额超过当前持仓时，按实际持仓全部卖出，不做空、不报错
        self._buy(1, "020608", 100, 1.0, "2026-01-01")
        self._sell(1, "020608", 500, 1.5, "2026-02-01")  # 超卖
        pos = tx.compute_position("020608", 1)
        self.assertEqual(pos["shares"], 0.0)
        self.assertEqual(pos["cost_amount"], 0.0)

    def test_sell_before_any_buy_is_noop_position(self):
        # 边界：还没有买入就卖出（脏数据/误操作），不应导致负份额或负成本
        self._sell(1, "020608", 50, 1.0, "2026-01-01")
        pos = tx.compute_position("020608", 1)
        self.assertEqual(pos["shares"], 0.0)
        self.assertEqual(pos["cost_amount"], 0.0)

    def test_amount_explicit_overrides_shares_times_price(self):
        # amount 显式传入时以 amount 为准（如手续费导致 amount != shares*price）
        self._buy(1, "020608", 100, 1.0, "2026-01-01", amount=105.0)
        pos = tx.compute_position("020608", 1)
        self.assertAlmostEqual(pos["cost_amount"], 105.0)
        self.assertAlmostEqual(pos["avg_cost"], 1.05)

    def test_position_scoped_by_fund_code(self):
        self._buy(1, "020608", 100, 1.0, "2026-01-01")
        self._buy(1, "005827", 50, 2.0, "2026-01-01")
        pos_a = tx.compute_position("020608", 1)
        pos_b = tx.compute_position("005827", 1)
        self.assertAlmostEqual(pos_a["shares"], 100.0)
        self.assertAlmostEqual(pos_b["shares"], 50.0)

    def test_position_scoped_by_user(self):
        self._buy(1, "020608", 100, 1.0, "2026-01-01")
        self._buy(2, "020608", 40, 1.0, "2026-01-01")
        self.assertAlmostEqual(tx.compute_position("020608", 1)["shares"], 100.0)
        self.assertAlmostEqual(tx.compute_position("020608", 2)["shares"], 40.0)

    def test_order_independent_of_insertion_uses_trade_date(self):
        # 插入顺序与交易日期顺序相反，加权推导应按 trade_date 排序而非插入顺序
        self._buy(1, "020608", 100, 1.2, "2026-01-05")
        self._buy(1, "020608", 100, 1.0, "2026-01-01")
        pos = tx.compute_position("020608", 1)
        self.assertAlmostEqual(pos["shares"], 200.0)
        self.assertAlmostEqual(pos["cost_amount"], 220.0)


class TestAmountFirstEntry(TransactionTestBase):
    """金额优先录入:给 amount+price 缺 shares 时反推份额。"""

    def test_amount_and_price_derives_shares(self):
        # 加仓 2000 元 @ 净值 1.25 → 份额 = 1600
        tx.add_transaction(
            {"fund_code": "020608", "action": "buy", "amount": 2000, "price": 1.25,
             "trade_date": "2026-01-01"}, 1)
        pos = tx.compute_position("020608", 1)
        self.assertAlmostEqual(pos["shares"], 1600.0)
        self.assertAlmostEqual(pos["cost_amount"], 2000.0)

    def test_shares_and_price_still_works(self):
        # 原有 shares+price 路径不受影响
        tx.add_transaction(
            {"fund_code": "020608", "action": "buy", "shares": 100, "price": 1.0,
             "trade_date": "2026-01-01"}, 1)
        pos = tx.compute_position("020608", 1)
        self.assertAlmostEqual(pos["shares"], 100.0)
        self.assertAlmostEqual(pos["cost_amount"], 100.0)

    def test_amount_only_without_price_rejected(self):
        # 只有金额、无净值 → 无法反推份额 → 拒绝
        tid = tx.add_transaction(
            {"fund_code": "020608", "action": "buy", "amount": 2000,
             "trade_date": "2026-01-01"}, 1)
        self.assertIsNone(tid)


class TestConversion(TransactionTestBase):
    """基金转换(A→B):成对流水 + 市价重置会计 + 删除级联。"""

    def _convert(self, user_id, from_code, to_code, out_shares, out_nav, in_nav,
                 fee=0.0, trade_date="2026-02-01"):
        return tx.add_conversion(
            {"from_code": from_code, "to_code": to_code, "out_shares": out_shares,
             "out_nav": out_nav, "in_nav": in_nav, "fee": fee, "trade_date": trade_date},
            user_id)

    def test_conversion_writes_linked_pair(self):
        res = self._convert(1, "020608", "005827", 100, 1.5, 2.0)
        self.assertIsNotNone(res)
        items = tx.list_transactions(1)
        self.assertEqual(len(items), 2)
        out = [i for i in items if i["action"] == "convert_out"][0]
        inn = [i for i in items if i["action"] == "convert_in"][0]
        self.assertEqual(out["fund_code"], "020608")
        self.assertEqual(inn["fund_code"], "005827")
        # 两腿共享同一 link_id
        self.assertEqual(out["link_id"], inn["link_id"])
        self.assertEqual(out["link_id"], res["link_id"])

    def test_conversion_market_value_reset_accounting(self):
        # 先建仓 A:100 份 @1.0(成本100)。再转出 100 份 @1.5,无费。
        # 转出市值=150 → A 已实现盈亏 = 150 - 100 = 50,A 清仓。
        # 转入 B:金额=150,净值 2.0 → 份额=75,成本=150。
        tx.add_transaction(
            {"fund_code": "020608", "action": "buy", "shares": 100, "price": 1.0,
             "trade_date": "2026-01-01"}, 1)
        self._convert(1, "020608", "005827", 100, 1.5, 2.0)
        pos_a = tx.compute_position("020608", 1)
        self.assertAlmostEqual(pos_a["shares"], 0.0)
        self.assertAlmostEqual(pos_a["realized_pnl"], 50.0)
        pos_b = tx.compute_position("005827", 1)
        self.assertAlmostEqual(pos_b["shares"], 75.0)
        self.assertAlmostEqual(pos_b["cost_amount"], 150.0)
        self.assertAlmostEqual(pos_b["avg_cost"], 2.0)

    def test_conversion_fee_reduces_in_amount(self):
        # 转出市值=150,转换费=6 → 转入金额=144,净值2.0 → 份额=72
        tx.add_transaction(
            {"fund_code": "020608", "action": "buy", "shares": 100, "price": 1.0,
             "trade_date": "2026-01-01"}, 1)
        self._convert(1, "020608", "005827", 100, 1.5, 2.0, fee=6.0)
        pos_b = tx.compute_position("005827", 1)
        self.assertAlmostEqual(pos_b["cost_amount"], 144.0)
        self.assertAlmostEqual(pos_b["shares"], 72.0)

    def test_conversion_partial_out(self):
        # 建仓 A 200 份 @1.0(成本200)。转出 50 份 @1.2(市值60)。
        # A 已实现 = 60 - (均价1.0×50)=10;剩余 150 份成本 150。
        tx.add_transaction(
            {"fund_code": "020608", "action": "buy", "shares": 200, "price": 1.0,
             "trade_date": "2026-01-01"}, 1)
        self._convert(1, "020608", "005827", 50, 1.2, 1.0)
        pos_a = tx.compute_position("020608", 1)
        self.assertAlmostEqual(pos_a["shares"], 150.0)
        self.assertAlmostEqual(pos_a["cost_amount"], 150.0)
        self.assertAlmostEqual(pos_a["realized_pnl"], 10.0)

    def test_delete_conversion_cascades_both_legs(self):
        res = self._convert(1, "020608", "005827", 100, 1.5, 2.0)
        tx.delete_transaction(res["out_id"], 1)  # 删转出腿
        self.assertEqual(len(tx.list_transactions(1)), 0)  # 转入腿也应消失

    def test_delete_conversion_from_in_leg_also_cascades(self):
        res = self._convert(1, "020608", "005827", 100, 1.5, 2.0)
        tx.delete_transaction(res["in_id"], 1)  # 从转入腿删
        self.assertEqual(len(tx.list_transactions(1)), 0)

    def test_delete_conversion_isolation(self):
        res = self._convert(1, "020608", "005827", 100, 1.5, 2.0)
        tx.delete_transaction(res["out_id"], 2)  # 越权:不生效
        self.assertEqual(len(tx.list_transactions(1)), 2)

    def test_reject_same_fund_conversion(self):
        self.assertIsNone(self._convert(1, "020608", "020608", 100, 1.5, 2.0))

    def test_reject_fee_exceeds_principal(self):
        # 转换费吃掉全部本金 → 非法
        self.assertIsNone(self._convert(1, "020608", "005827", 100, 1.5, 2.0, fee=200.0))

    def test_reject_missing_codes_or_navs(self):
        self.assertIsNone(self._convert(1, "", "005827", 100, 1.5, 2.0))
        self.assertIsNone(self._convert(1, "020608", "005827", 100, 0, 2.0))
        self.assertIsNone(self._convert(1, "020608", "005827", 0, 1.5, 2.0))

    def test_convert_handler_requires_auth(self):
        code, obj = tx._h_convert(Ctx(user_id=None, body={}))
        self.assertEqual(code, 401)

    def test_convert_handler_invalid_returns_400(self):
        code, obj = tx._h_convert(Ctx(user_id=1, body={"from_code": ""}))
        self.assertEqual(code, 400)

    def test_convert_handler_ok(self):
        result = tx._h_convert(Ctx(user_id=1, body={
            "from_code": "020608", "to_code": "005827", "out_shares": 100,
            "out_nav": 1.5, "in_nav": 2.0, "trade_date": "2026-02-01"}))
        self.assertNotIsInstance(result, tuple)
        self.assertTrue(result["ok"])
        self.assertIn("link_id", result)


class TestSemanticLabels(TransactionTestBase):
    """流水语义标签派生:建仓/加仓/减仓/清仓/转出/转入。"""

    def _label_by_action_date(self, user_id, code):
        items = tx.list_transactions(user_id, code)
        return {(i["action"], i["trade_date"]): i["label"] for i in items}

    def test_open_add_reduce_close_sequence(self):
        tx.add_transaction({"fund_code": "020608", "action": "buy", "shares": 100,
                            "price": 1.0, "trade_date": "2026-01-01"}, 1)  # 建仓
        tx.add_transaction({"fund_code": "020608", "action": "buy", "shares": 100,
                            "price": 1.2, "trade_date": "2026-01-05"}, 1)  # 加仓
        tx.add_transaction({"fund_code": "020608", "action": "sell", "shares": 50,
                            "price": 1.5, "trade_date": "2026-02-01"}, 1)  # 减仓
        tx.add_transaction({"fund_code": "020608", "action": "sell", "shares": 150,
                            "price": 1.6, "trade_date": "2026-03-01"}, 1)  # 清仓
        labels = self._label_by_action_date(1, "020608")
        self.assertEqual(labels[("buy", "2026-01-01")], "建仓")
        self.assertEqual(labels[("buy", "2026-01-05")], "加仓")
        self.assertEqual(labels[("sell", "2026-02-01")], "减仓")
        self.assertEqual(labels[("sell", "2026-03-01")], "清仓")

    def test_convert_labels(self):
        tx.add_transaction({"fund_code": "020608", "action": "buy", "shares": 100,
                            "price": 1.0, "trade_date": "2026-01-01"}, 1)
        tx.add_conversion({"from_code": "020608", "to_code": "005827", "out_shares": 100,
                           "out_nav": 1.5, "in_nav": 2.0, "trade_date": "2026-02-01"}, 1)
        items = tx.list_transactions(1)
        by_act = {i["action"]: i["label"] for i in items}
        self.assertEqual(by_act["convert_out"], "转出")
        self.assertEqual(by_act["convert_in"], "转入")

    def test_labels_scoped_per_fund(self):
        # 两只基金各自独立回放:各自第一笔买入都应是「建仓」
        tx.add_transaction({"fund_code": "020608", "action": "buy", "shares": 100,
                            "price": 1.0, "trade_date": "2026-01-01"}, 1)
        tx.add_transaction({"fund_code": "005827", "action": "buy", "shares": 50,
                            "price": 2.0, "trade_date": "2026-01-02"}, 1)
        items = tx.list_transactions(1)
        self.assertTrue(all(i["label"] == "建仓" for i in items))


class TestListAddDelete(TransactionTestBase):
    def test_add_and_list(self):
        tid = tx.add_transaction(
            {"fund_code": "020608", "action": "buy", "shares": 100, "price": 1.0,
             "trade_date": "2026-01-01"},
            1,
        )
        self.assertIsNotNone(tid)
        items = tx.list_transactions(1, "020608")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["fund_code"], "020608")
        self.assertEqual(items[0]["action"], "buy")
        self.assertAlmostEqual(items[0]["amount"], 100.0)

    def test_list_all_funds_without_code_filter(self):
        tx.add_transaction({"fund_code": "020608", "action": "buy", "shares": 1, "price": 1,
                             "trade_date": "2026-01-01"}, 1)
        tx.add_transaction({"fund_code": "005827", "action": "buy", "shares": 1, "price": 1,
                             "trade_date": "2026-01-01"}, 1)
        items = tx.list_transactions(1)
        self.assertEqual(len(items), 2)

    def test_reject_invalid_action(self):
        tid = tx.add_transaction(
            {"fund_code": "020608", "action": "hold", "shares": 1, "price": 1,
             "trade_date": "2026-01-01"},
            1,
        )
        self.assertIsNone(tid)

    def test_reject_missing_fund_code(self):
        tid = tx.add_transaction(
            {"fund_code": "", "action": "buy", "shares": 1, "price": 1,
             "trade_date": "2026-01-01"},
            1,
        )
        self.assertIsNone(tid)

    def test_delete_own(self):
        tid = tx.add_transaction(
            {"fund_code": "020608", "action": "buy", "shares": 1, "price": 1,
             "trade_date": "2026-01-01"},
            1,
        )
        tx.delete_transaction(tid, 1)
        self.assertEqual(len(tx.list_transactions(1, "020608")), 0)

    def test_isolation_list_only_own(self):
        tx.add_transaction({"fund_code": "020608", "action": "buy", "shares": 1, "price": 1,
                             "trade_date": "2026-01-01"}, 1)
        self.assertEqual(len(tx.list_transactions(1)), 1)
        self.assertEqual(len(tx.list_transactions(2)), 0)

    def test_isolation_cannot_delete_others(self):
        tid = tx.add_transaction({"fund_code": "020608", "action": "buy", "shares": 1, "price": 1,
                                   "trade_date": "2026-01-01"}, 1)
        tx.delete_transaction(tid, 2)  # 越权删除，不应生效
        self.assertEqual(len(tx.list_transactions(1, "020608")), 1)
        tx.delete_transaction(tid, 1)  # 本人可删
        self.assertEqual(len(tx.list_transactions(1, "020608")), 0)


class TestHandlers(TransactionTestBase):
    """路由 handler（鉴权 + 响应结构）。"""

    def test_list_requires_auth(self):
        code, obj = tx._h_list(Ctx(user_id=None))
        self.assertEqual(code, 401)

    def test_add_requires_auth(self):
        code, obj = tx._h_add(Ctx(user_id=None, body={}))
        self.assertEqual(code, 401)

    def test_delete_requires_auth(self):
        code, obj = tx._h_delete(Ctx(user_id=None, params={"id": "1"}))
        self.assertEqual(code, 401)

    def test_add_then_list_via_handlers(self):
        add_result = tx._h_add(Ctx(
            user_id=1,
            body={"fund_code": "020608", "action": "buy", "shares": 100, "price": 1.0,
                  "trade_date": "2026-01-01"},
        ))
        self.assertNotIsInstance(add_result, tuple)  # 默认 200，直接返回 obj
        self.assertTrue(add_result["ok"])

        listed = tx._h_list(Ctx(user_id=1, query={"code": ["020608"]}))
        self.assertNotIsInstance(listed, tuple)
        self.assertEqual(len(listed["items"]), 1)
        self.assertIsNotNone(listed["position"])
        self.assertAlmostEqual(listed["position"]["shares"], 100.0)

    def test_list_without_code_has_no_position(self):
        result = tx._h_list(Ctx(user_id=1, query={}))
        self.assertIsNone(result["position"])

    def test_add_invalid_returns_400(self):
        code, obj = tx._h_add(Ctx(user_id=1, body={"fund_code": "", "action": "buy"}))
        self.assertEqual(code, 400)

    def test_delete_via_handler(self):
        tid = tx.add_transaction(
            {"fund_code": "020608", "action": "buy", "shares": 1, "price": 1,
             "trade_date": "2026-01-01"},
            1,
        )
        result = tx._h_delete(Ctx(user_id=1, params={"id": str(tid)}))
        self.assertTrue(result["ok"])
        self.assertEqual(len(tx.list_transactions(1, "020608")), 0)


if __name__ == "__main__":
    unittest.main()
