# -*- coding: utf-8 -*-
"""M9-F 阶段收益率测试:基于 fund_nav_history 只读计算。"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from backend.api import returns
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

    def _nav(self, code, days_ago, nav):
        d = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        c = sqlite3.connect(self.path)
        c.execute("INSERT OR REPLACE INTO fund_nav_history(fund_code,nav_date,nav) VALUES(?,?,?)", (code, d, nav))
        c.commit()
        c.close()


class TestPeriods(_T):
    def test_no_data_all_null(self):
        from backend.models.db import get_conn
        c = get_conn()
        p = returns._compute_periods(c, "X")
        c.close()
        self.assertEqual(p, {"m1": None, "m3": None, "ytd": None, "max": None})

    def test_full_data(self):
        self._nav("X", 0, 2.0)
        self._nav("X", 30, 1.0)
        self._nav("X", 90, 1.5)
        self._nav("X", 200, 1.0)
        from backend.models.db import get_conn
        c = get_conn()
        p = returns._compute_periods(c, "X")
        c.close()
        self.assertEqual(p["m1"], 100.0)
        self.assertEqual(p["m3"], 33.33)
        self.assertEqual(p["max"], 100.0)
        self.assertIsNotNone(p["ytd"])

    def test_insufficient_data(self):
        self._nav("X", 0, 2.0)
        self._nav("X", 5, 1.9)
        from backend.models.db import get_conn
        c = get_conn()
        p = returns._compute_periods(c, "X")
        c.close()
        self.assertIsNone(p["m1"])
        self.assertIsNone(p["m3"])
        self.assertIsNone(p["ytd"])
        self.assertEqual(p["max"], round((2.0 - 1.9) / 1.9 * 100, 2))


class TestApi(_T):
    def _ctx(self, code=None):
        from backend.api._router import Ctx
        return Ctx(params={"code": code} if code else {})

    def test_missing_code(self):
        code, _ = returns.get_returns(self._ctx())
        self.assertEqual(code, 400)

    def test_returns_periods(self):
        self._nav("X", 0, 2.0)
        self._nav("X", 30, 1.0)
        r = returns.get_returns(self._ctx("X"))
        self.assertEqual(r["fund_code"], "X")
        self.assertEqual(r["periods"]["m1"], 100.0)

    def test_empty_code_after_strip(self):
        code, _ = returns.get_returns(self._ctx("   "))
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
