# -*- coding: utf-8 -*-
"""PRD-07 移动止盈测试:scheduler 巡检 peak_nav 更新 + 触发通知 + expectation 实时判断。"""
import os
import sqlite3
import tempfile
import unittest

from backend.api import expectation
from backend.api._router import Ctx
from backend.scheduler import _check_trailing_stops
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

    def _hold(self, user_id, code, trailing, peak, hold_amount=1000, cost=1000):
        c = sqlite3.connect(self.path)
        c.execute(
            "INSERT INTO holding(user_id,fund_code,hold_amount,cost_amount,"
            "trailing_stop_pct,peak_nav,created_at) "
            "VALUES(?,?,?,?,?,?,datetime('now'))",
            (user_id, code, hold_amount, cost, trailing, peak),
        )
        c.commit()
        c.close()

    def _quote(self, code, gsz, dwjz=1.0, nav=None):
        c = sqlite3.connect(self.path)
        c.execute(
            "INSERT OR REPLACE INTO fund_quote(fund_code,name,gsz,dwjz,nav,updated_at) "
            "VALUES(?,?,?,?,?,datetime('now'))", (code, code, gsz, dwjz, nav),
        )
        c.commit()
        c.close()


class TestCheckTrailingStops(_T):
    def test_updates_peak_when_higher(self):
        self._hold(1, "X", trailing=8, peak=1.0)
        self._quote("X", gsz=1.2, nav=1.2)   # cur=1.2 > peak 1.0
        n = _check_trailing_stops()
        self.assertEqual(n, 0)               # 新高不触发
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        peak = c.execute("SELECT peak_nav FROM holding WHERE fund_code='X'").fetchone()["peak_nav"]
        c.close()
        self.assertEqual(peak, 1.2)

    def test_triggers_on_drawdown(self):
        self._hold(1, "X", trailing=8, peak=1.5)   # 线=1.5×0.92=1.38
        self._quote("X", gsz=1.3, nav=1.3)          # cur=1.3<=1.38 触发
        n = _check_trailing_stops()
        self.assertEqual(n, 1)
        c = sqlite3.connect(self.path)
        cnt = c.execute(
            "SELECT COUNT(*) FROM notification WHERE kind='trailing_stop_hit'").fetchone()[0]
        c.close()
        self.assertEqual(cnt, 1)

    def test_dedup_unread(self):
        self._hold(1, "X", trailing=8, peak=1.5)
        self._quote("X", gsz=1.3, nav=1.3)
        _check_trailing_stops()
        n = _check_trailing_stops()           # 第二次,已有未读
        self.assertEqual(n, 0)

    def test_no_trailing_skip(self):
        self._hold(1, "X", trailing=None, peak=None)
        self._quote("X", gsz=1.0, nav=1.0)
        self.assertEqual(_check_trailing_stops(), 0)


class TestExpectationHitTrailing(_T):
    def test_hit_trailing_stop(self):
        self._hold(1, "X", trailing=8, peak=1.5)
        self._quote("X", gsz=1.3, dwjz=1.0, nav=1.3)   # cur=1.3<=1.38
        r = expectation.get_expectations(Ctx(user_id=1))
        self.assertTrue(r["items"][0]["hit_trailing_stop"])
        self.assertEqual(r["items"][0]["peak_nav"], 1.5)

    def test_not_hit_on_new_high(self):
        self._hold(1, "X", trailing=8, peak=1.0)
        self._quote("X", gsz=1.2, nav=1.2)              # cur=1.2>0.92 不触发
        r = expectation.get_expectations(Ctx(user_id=1))
        self.assertFalse(r["items"][0]["hit_trailing_stop"])


if __name__ == "__main__":
    unittest.main()
