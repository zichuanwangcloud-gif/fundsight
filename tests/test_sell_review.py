# -*- coding: utf-8 -*-
"""卖出复盘 / 已实现盈亏端点(returns._sell_review / get_realized)单测。

覆盖:
  - 无卖出:has_sells=False, realized_pnl=0
  - 单笔部分卖出:落袋盈亏 / 收益率 / 持有天数 / 年化
  - 多笔买入后卖出:加权成本口径 + 加权建仓日
  - 全部卖出后再买再卖:池清零重置
  - 用户隔离 + 未登录 401
"""
import os
import tempfile
import unittest

from backend.api import returns
from backend.api._router import Ctx
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

    def _tx(self, uid, code, action, shares, amount, d):
        c = db_mod.get_conn()
        c.execute("INSERT INTO fund_transaction(user_id,fund_code,action,shares,price,amount,"
                  "trade_date,created_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
                  (uid, code, action, shares, amount / shares, amount, d))
        c.commit(); c.close()

    def _review(self, code, uid=1):
        c = db_mod.get_conn()
        try:
            return returns._sell_review(c, code, uid)
        finally:
            c.close()


class TestSellReview(_T):
    def test_no_sells(self):
        self._tx(1, "A", "buy", 1000, 1000, "2026-01-01")
        r = self._review("A")
        self.assertFalse(r["has_sells"])
        self.assertEqual(r["realized_pnl"], 0.0)
        self.assertEqual(r["sells"], [])

    def test_single_partial_sell(self):
        # 买1000份@1.0(2026-01-01),卖400份得600(2026-01-31,价1.5)
        self._tx(1, "A", "buy", 1000, 1000, "2026-01-01")
        self._tx(1, "A", "sell", 400, 600, "2026-01-31")
        r = self._review("A")
        self.assertTrue(r["has_sells"])
        s = r["sells"][0]
        self.assertEqual(s["realized_pnl"], 200.0)      # 600 - 1.0*400
        self.assertEqual(s["realized_pct"], 50.0)        # 200/400
        self.assertEqual(s["hold_days"], 30)             # 01-01 → 01-31
        self.assertIsNotNone(s["annualized_pct"])
        self.assertEqual(r["realized_pnl"], 200.0)

    def test_weighted_cost_two_buys(self):
        # 买500@1.0 + 买500@2.0 → 均价1.5;卖500得1000(价2.0)→ 落袋 1000-1.5*500=250
        self._tx(1, "A", "buy", 500, 500, "2026-01-01")
        self._tx(1, "A", "buy", 500, 1000, "2026-02-01")
        self._tx(1, "A", "sell", 500, 1000, "2026-03-01")
        r = self._review("A")
        s = r["sells"][0]
        self.assertAlmostEqual(s["realized_pnl"], 250.0, places=2)

    def test_reset_after_full_sell(self):
        self._tx(1, "A", "buy", 1000, 1000, "2026-01-01")
        self._tx(1, "A", "sell", 1000, 1200, "2026-02-01")   # 落袋 200,池清零
        self._tx(1, "A", "buy", 1000, 1000, "2026-03-01")
        self._tx(1, "A", "sell", 1000, 900, "2026-04-01")    # 割肉 -100
        r = self._review("A")
        self.assertEqual(len(r["sells"]), 2)
        self.assertEqual(r["sells"][0]["realized_pnl"], 200.0)
        self.assertEqual(r["sells"][1]["realized_pnl"], -100.0)
        self.assertEqual(r["realized_pnl"], 100.0)           # 200 - 100

    def test_user_isolation(self):
        self._tx(1, "A", "buy", 1000, 1000, "2026-01-01")
        self._tx(1, "A", "sell", 500, 700, "2026-02-01")
        r2 = self._review("A", uid=2)
        self.assertFalse(r2["has_sells"])

    def test_requires_login(self):
        code, _ = returns.get_realized(Ctx(user_id=None, params={"code": "A"}))
        self.assertEqual(code, 401)

    def test_route_registered(self):
        paths = {p for _, p, _ in returns.ROUTES}
        self.assertIn("/api/fund/{code}/realized", paths)


if __name__ == "__main__":
    unittest.main()
