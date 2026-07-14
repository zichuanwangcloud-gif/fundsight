# -*- coding: utf-8 -*-
"""PRD-04 P1 定投计划 CRUD + scheduler 到点提醒测试。"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from backend.api import dca
from backend.api._router import Ctx
from backend.scheduler import _check_dca_plans
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

    def _ctx(self, user_id=None, body=None, params=None):
        return Ctx(user_id=user_id, body=body or {}, params=params or {})

    def _plan(self, user_id, code, per_amount, freq, invest_day, next_date, active=1):
        c = sqlite3.connect(self.path)
        c.execute(
            "INSERT INTO dca_plan(user_id,fund_code,per_amount,freq,invest_day,"
            "next_date,active,created_at) VALUES(?,?,?,?,?,?,?,'now')",
            (user_id, code, per_amount, freq, invest_day, next_date, active),
        )
        c.commit()
        c.close()


class TestPlanCrud(_T):
    def test_add_returns_next_date(self):
        r = dca.add_dca_plan(self._ctx(1, body={
            "fund_code": "X", "per_amount": 1000, "freq": "monthly", "invest_day": 1}))
        self.assertTrue(r["ok"])
        self.assertIsNotNone(r["id"])
        self.assertIsNotNone(r["next_date"])   # 下月1号 > 今天
        self.assertGreater(r["next_date"], date.today().isoformat())

    def test_list_and_isolation(self):
        dca.add_dca_plan(self._ctx(1, body={
            "fund_code": "A", "per_amount": 500, "freq": "weekly", "invest_day": 1}))
        r1 = dca.list_dca_plans(self._ctx(1))
        r2 = dca.list_dca_plans(self._ctx(2))
        self.assertEqual(len(r1["items"]), 1)
        self.assertEqual(r1["items"][0]["fund_code"], "A")
        self.assertEqual(len(r2["items"]), 0)

    def test_update_and_delete(self):
        r = dca.add_dca_plan(self._ctx(1, body={
            "fund_code": "A", "per_amount": 500, "freq": "monthly", "invest_day": 1}))
        pid = r["id"]
        upd = dca.update_dca_plan(self._ctx(1, body={"active": 0}, params={"id": pid}))
        self.assertTrue(upd["ok"])
        items = dca.list_dca_plans(self._ctx(1))["items"]
        self.assertEqual(items[0]["active"], 0)
        dca.delete_dca_plan(self._ctx(1, params={"id": pid}))
        self.assertEqual(len(dca.list_dca_plans(self._ctx(1))["items"]), 0)

    def test_cross_user_update_denied(self):
        r = dca.add_dca_plan(self._ctx(1, body={
            "fund_code": "A", "per_amount": 500, "freq": "monthly", "invest_day": 1}))
        # user2 改 user1 的计划 → 不生效(行不变)
        dca.update_dca_plan(self._ctx(2, body={"active": 0}, params={"id": r["id"]}))
        items = dca.list_dca_plans(self._ctx(1))["items"]
        self.assertEqual(items[0]["active"], 1)

    def test_invalid_freq_400(self):
        code, _ = dca.add_dca_plan(self._ctx(1, body={
            "fund_code": "A", "per_amount": 500, "freq": "daily", "invest_day": 1}))
        self.assertEqual(code, 400)

    def test_unauthorized_401(self):
        self.assertEqual(dca.list_dca_plans(self._ctx(None))[0], 401)


class TestCheckDcaPlans(_T):
    def test_due_triggers_and_rolls(self):
        today = date.today().isoformat()
        self._plan(1, "X", 1000, "monthly", 1, today)   # next_date<=today 到期
        n = _check_dca_plans()
        self.assertEqual(n, 1)
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        cnt = c.execute(
            "SELECT COUNT(*) FROM notification WHERE kind='dca_due'").fetchone()[0]
        nd = c.execute("SELECT next_date FROM dca_plan WHERE fund_code='X'").fetchone()["next_date"]
        c.close()
        self.assertEqual(cnt, 1)
        # next_date 滚到下月1号 > 今天
        self.assertGreater(nd, today)

    def test_future_not_due(self):
        future = (date.today() + timedelta(days=10)).isoformat()
        self._plan(1, "X", 1000, "monthly", 1, future)
        self.assertEqual(_check_dca_plans(), 0)

    def test_dedup_until_read(self):
        today = date.today().isoformat()
        self._plan(1, "X", 1000, "monthly", 1, today)
        _check_dca_plans()           # 首次推 + 滚动
        # next_date 已滚到下月,不再到期 → 0
        self.assertEqual(_check_dca_plans(), 0)

    def test_routes_registered(self):
        self.assertTrue(any(p == "/api/dca/plans" for _, p, _ in dca.ROUTES))
        self.assertTrue(any(p == "/api/dca/plans/{id}" for _, p, _ in dca.ROUTES))


if __name__ == "__main__":
    unittest.main()
