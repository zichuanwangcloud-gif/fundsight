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
        self.assertEqual(pos, {"shares": 0.0, "cost_amount": 0.0, "avg_cost": 0.0})

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
