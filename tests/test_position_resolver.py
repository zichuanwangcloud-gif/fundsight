# -*- coding: utf-8 -*-
"""地基:transactions.resolve_position / compute_position(含已实现盈亏)单测。

覆盖:
  - 纯买入:份额/成本/均价,realized_pnl=0
  - 部分卖出:按均摊成本冲减 + 已实现盈亏(落袋)累计
  - 全部卖出:清仓归零 + realized 反映全部落袋
  - 超卖:按实际持有量清仓,卖出金额等比例折算,不做空
  - 未持有先卖(脏数据):忽略,不产生负份额
  - resolve_position:有流水→流水口径;无流水→回退 holding 手填;都无→empty
  - position_market_value:两种口径市值一致
"""
import os
import sqlite3
import tempfile
import unittest

from backend.api import transactions as tx
from backend.models import db as db_mod


class _T(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._o = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._o
        os.unlink(self.path)

    def _tx(self, user_id, code, action, shares, amount, trade_date):
        c = db_mod.get_conn()
        c.execute(
            "INSERT INTO fund_transaction(user_id,fund_code,action,shares,price,amount,"
            "trade_date,created_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
            (user_id, code, action, shares, amount / shares, amount, trade_date),
        )
        c.commit()
        c.close()

    def _hold(self, user_id, code, hold_amount, cost_amount):
        c = db_mod.get_conn()
        c.execute(
            "INSERT INTO holding(user_id,fund_code,hold_amount,cost_amount,created_at) "
            "VALUES (?,?,?,?,datetime('now'))", (user_id, code, hold_amount, cost_amount),
        )
        c.commit()
        c.close()


class TestComputePosition(_T):
    def test_pure_buy(self):
        self._tx(1, "A", "buy", 1000, 1000, "2026-01-01")
        self._tx(1, "A", "buy", 500, 600, "2026-02-01")  # 加仓贵一点
        p = tx.compute_position("A", 1)
        self.assertEqual(p["shares"], 1500.0)
        self.assertEqual(p["cost_amount"], 1600.0)
        self.assertAlmostEqual(p["avg_cost"], 1600 / 1500, places=6)
        self.assertEqual(p["realized_pnl"], 0.0)
        self.assertTrue(p["has_tx"])

    def test_partial_sell_realized(self):
        # 买 1000 份花 1000(均价1.0);卖 400 份得 600(价1.5)→ 落袋 600-400*1.0=200
        self._tx(1, "A", "buy", 1000, 1000, "2026-01-01")
        self._tx(1, "A", "sell", 400, 600, "2026-03-01")
        p = tx.compute_position("A", 1)
        self.assertEqual(p["shares"], 600.0)
        self.assertEqual(p["cost_amount"], 600.0)      # 1000 - 1.0*400
        self.assertEqual(p["realized_pnl"], 200.0)

    def test_full_sell_clears(self):
        self._tx(1, "A", "buy", 1000, 1000, "2026-01-01")
        self._tx(1, "A", "sell", 1000, 1300, "2026-03-01")  # 落袋 300
        p = tx.compute_position("A", 1)
        self.assertEqual(p["shares"], 0.0)
        self.assertEqual(p["cost_amount"], 0.0)
        self.assertEqual(p["realized_pnl"], 300.0)

    def test_oversell_prorates_amount(self):
        # 持 500 份(成本500),卖 1000 份报 2000 → 实际只卖 500 份,
        # 折算卖出金额 = 2000*(500/1000)=1000,落袋 = 1000 - 1.0*500 = 500
        self._tx(1, "A", "buy", 500, 500, "2026-01-01")
        self._tx(1, "A", "sell", 1000, 2000, "2026-03-01")
        p = tx.compute_position("A", 1)
        self.assertEqual(p["shares"], 0.0)
        self.assertEqual(p["realized_pnl"], 500.0)

    def test_sell_before_buy_ignored(self):
        self._tx(1, "A", "sell", 100, 100, "2026-01-01")  # 脏数据
        self._tx(1, "A", "buy", 200, 200, "2026-02-01")
        p = tx.compute_position("A", 1)
        self.assertEqual(p["shares"], 200.0)
        self.assertEqual(p["cost_amount"], 200.0)
        self.assertEqual(p["realized_pnl"], 0.0)


class TestResolvePosition(_T):
    def test_transaction_source_wins(self):
        self._tx(1, "A", "buy", 1000, 1000, "2026-01-01")
        self._hold(1, "A", 9999, 8888)  # 手填也在,但应被流水覆盖
        c = db_mod.get_conn()
        pos = tx.resolve_position(c, "A", 1)
        c.close()
        self.assertEqual(pos["source"], "transaction")
        self.assertEqual(pos["shares"], 1000.0)
        self.assertEqual(pos["cost_amount"], 1000.0)
        self.assertIsNone(pos["hold_amount"])

    def test_holding_fallback(self):
        self._hold(1, "A", 5000, 4000)
        c = db_mod.get_conn()
        pos = tx.resolve_position(c, "A", 1)
        c.close()
        self.assertEqual(pos["source"], "holding")
        self.assertIsNone(pos["shares"])
        self.assertEqual(pos["cost_amount"], 4000)
        self.assertEqual(pos["hold_amount"], 5000)
        self.assertEqual(pos["realized_pnl"], 0.0)

    def test_empty(self):
        c = db_mod.get_conn()
        pos = tx.resolve_position(c, "ZZZ", 1)
        c.close()
        self.assertEqual(pos["source"], "empty")
        self.assertIsNone(pos["shares"])

    def test_isolation_by_user(self):
        self._tx(1, "A", "buy", 1000, 1000, "2026-01-01")
        c = db_mod.get_conn()
        pos2 = tx.resolve_position(c, "A", 2)  # 别的用户看不到
        c.close()
        self.assertEqual(pos2["source"], "empty")


class TestMarketValue(_T):
    def test_transaction_and_holding_agree(self):
        # 流水口径:shares=1000,现价1.1 → 1100
        pos_tx = {"shares": 1000.0, "hold_amount": None}
        v_tx = tx.position_market_value(pos_tx, dwjz=1.0, gsz=1.1, nav=None)
        self.assertAlmostEqual(v_tx, 1100.0, places=4)
        # 手填口径:hold_amount=1000@dwjz1.0 → shares=1000,现价1.1 → 1100
        pos_h = {"shares": None, "hold_amount": 1000.0}
        v_h = tx.position_market_value(pos_h, dwjz=1.0, gsz=1.1, nav=None)
        self.assertAlmostEqual(v_h, 1100.0, places=4)

    def test_nav_preferred_over_gsz(self):
        pos = {"shares": 100.0, "hold_amount": None}
        v = tx.position_market_value(pos, dwjz=1.0, gsz=1.5, nav=2.0)
        self.assertEqual(v, 200.0)  # 用 nav 收盘价

    def test_no_price_returns_none(self):
        pos = {"shares": 100.0, "hold_amount": None}
        self.assertIsNone(tx.position_market_value(pos, dwjz=1.0, gsz=None, nav=None))


if __name__ == "__main__":
    unittest.main()
