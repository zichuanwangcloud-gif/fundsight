# -*- coding: utf-8 -*-
"""PRD-07 预期深化单元测试(年化目标 / 回本所需涨幅 / 达成时间推算)。

覆盖:
  - target_annual:持有365天=30%、持有730天≈14%
  - recovery_pct:浮亏20%→回本需涨25%;浮盈→null
  - days_to_target_est:近3月上涨+未达目标→正天数;已达目标→note
  - 未登录 401、user 隔离、路由注册
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from backend.api import expectation
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

    def _hold(self, user_id, code, created_days_ago, target_rate, cost, hold_amount):
        cd = (date.today() - timedelta(days=created_days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        c = sqlite3.connect(self.path)
        c.execute(
            "INSERT INTO holding(user_id,fund_code,hold_amount,cost_amount,target_rate,created_at) "
            "VALUES (?,?,?,?,?,?)", (user_id, code, hold_amount, cost, target_rate, cd),
        )
        c.commit()
        c.close()

    def _quote(self, code, gsz, dwjz, nav=None):
        c = sqlite3.connect(self.path)
        c.execute(
            "INSERT OR REPLACE INTO fund_quote(fund_code,name,gsz,dwjz,nav,updated_at) "
            "VALUES (?,?,?,?,?,datetime('now'))", (code, code, gsz, dwjz, nav),
        )
        c.commit()
        c.close()

    def _nav(self, code, days_ago, nav_adj):
        d = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        c = sqlite3.connect(self.path)
        c.execute(
            "INSERT INTO fund_nav_history(fund_code,nav_date,nav,nav_adj) VALUES (?,?,?,?)",
            (code, d, nav_adj, nav_adj),
        )
        c.commit()
        c.close()


class TestComputeForHolding(_T):
    def test_target_annual_365_days(self):
        self._hold(1, "X", created_days_ago=365, target_rate=30, cost=1000, hold_amount=1000)
        self._quote("X", gsz=1.0, dwjz=1.0, nav=1.0)  # 不浮亏不浮盈
        c = db_mod.get_conn()
        h = c.execute("SELECT fund_code,target_rate,cost_amount,hold_amount,created_at FROM holding WHERE user_id=1").fetchone()
        q = c.execute("SELECT dwjz,gsz,nav FROM fund_quote WHERE fund_code='X'").fetchone()
        r = expectation._compute_for_holding(c, h, q)
        c.close()
        self.assertEqual(r["target_annual"], 30.0)

    def test_target_annual_730_days(self):
        self._hold(1, "X", created_days_ago=730, target_rate=30, cost=1000, hold_amount=1000)
        self._quote("X", gsz=1.0, dwjz=1.0, nav=1.0)
        c = db_mod.get_conn()
        h = c.execute("SELECT fund_code,target_rate,cost_amount,hold_amount,created_at FROM holding WHERE user_id=1").fetchone()
        q = c.execute("SELECT dwjz,gsz,nav FROM fund_quote WHERE fund_code='X'").fetchone()
        r = expectation._compute_for_holding(c, h, q)
        c.close()
        # (1.30)^(365/730) - 1 ≈ 0.1402 → 14.02
        self.assertAlmostEqual(r["target_annual"], 14.02, places=1)

    def test_recovery_pct_on_loss(self):
        # cost=1000, current_value=800(浮亏20%) → 回本需涨25%
        self._hold(1, "X", created_days_ago=365, target_rate=30, cost=1000, hold_amount=1000)
        self._quote("X", gsz=0.8, dwjz=1.0, nav=0.8)  # shares=1000, value=800
        c = db_mod.get_conn()
        h = c.execute("SELECT fund_code,target_rate,cost_amount,hold_amount,created_at FROM holding WHERE user_id=1").fetchone()
        q = c.execute("SELECT dwjz,gsz,nav FROM fund_quote WHERE fund_code='X'").fetchone()
        r = expectation._compute_for_holding(c, h, q)
        c.close()
        self.assertEqual(r["current_return_pct"], -20.0)
        self.assertEqual(r["recovery_pct"], 25.0)

    def test_recovery_null_on_profit(self):
        self._hold(1, "X", created_days_ago=365, target_rate=30, cost=1000, hold_amount=1000)
        self._quote("X", gsz=1.2, dwjz=1.0, nav=1.2)  # 浮盈
        c = db_mod.get_conn()
        h = c.execute("SELECT fund_code,target_rate,cost_amount,hold_amount,created_at FROM holding WHERE user_id=1").fetchone()
        q = c.execute("SELECT dwjz,gsz,nav FROM fund_quote WHERE fund_code='X'").fetchone()
        r = expectation._compute_for_holding(c, h, q)
        c.close()
        self.assertIsNone(r["recovery_pct"])

    def test_days_to_target_est_positive(self):
        # 近3月 nav_adj 从1.0涨到1.1(r_3m=0.1,年化>0);浮亏未达目标 → 正天数
        self._hold(1, "X", created_days_ago=365, target_rate=30, cost=1000, hold_amount=1000)
        self._quote("X", gsz=0.8, dwjz=1.0, nav=0.8)  # current_value=800, return=-20%
        self._nav("X", 95, 1.0)   # 3月前(>90天) nav_adj=1.0
        self._nav("X", 0, 1.1)    # 最新 nav_adj=1.1
        c = db_mod.get_conn()
        h = c.execute("SELECT fund_code,target_rate,cost_amount,hold_amount,created_at FROM holding WHERE user_id=1").fetchone()
        q = c.execute("SELECT dwjz,gsz,nav FROM fund_quote WHERE fund_code='X'").fetchone()
        r = expectation._compute_for_holding(c, h, q)
        c.close()
        self.assertIsNotNone(r["days_to_target_est"])
        self.assertGreater(r["days_to_target_est"], 0)

    def test_already_at_target_note(self):
        # 当前收益已达目标 → note "已达目标"
        self._hold(1, "X", created_days_ago=365, target_rate=10, cost=1000, hold_amount=1000)
        self._quote("X", gsz=1.2, dwjz=1.0, nav=1.2)  # return=+20% 已超目标10
        c = db_mod.get_conn()
        h = c.execute("SELECT fund_code,target_rate,cost_amount,hold_amount,created_at FROM holding WHERE user_id=1").fetchone()
        q = c.execute("SELECT dwjz,gsz,nav FROM fund_quote WHERE fund_code='X'").fetchone()
        r = expectation._compute_for_holding(c, h, q)
        c.close()
        self.assertIsNone(r["days_to_target_est"])
        self.assertEqual(r["note"], "已达目标")


class TestApi(_T):
    def test_unauthorized_401(self):
        code, _ = expectation.get_expectations(Ctx(user_id=None))
        self.assertEqual(code, 401)

    def test_user_isolation(self):
        self._hold(1, "A", 365, 30, 1000, 1000)
        self._hold(2, "B", 365, 30, 1000, 1000)
        r1 = expectation.get_expectations(Ctx(user_id=1))
        r2 = expectation.get_expectations(Ctx(user_id=2))
        self.assertEqual(len(r1["items"]), 1)
        self.assertEqual(r1["items"][0]["fund_code"], "A")
        self.assertEqual(r2["items"][0]["fund_code"], "B")

    def test_registered_in_routes(self):
        self.assertTrue(
            any(m == "GET" and p == "/api/holdings/expectations" for m, p, _ in expectation.ROUTES)
        )


if __name__ == "__main__":
    unittest.main()
