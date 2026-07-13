# -*- coding: utf-8 -*-
"""M9-C 净值断点检测测试:_detect_nav_gaps 落库 + 告警判定。

不发起真实网络/抓取,用临时库隔离,直接构造 holding/fund_nav_history。
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from backend import scheduler
from backend.models import db as db_mod


class _TmpDbMixin:
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _exec(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def _last_gap_run(self):
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT status,affected,error FROM task_run "
            "WHERE task_name='nav_gap_check' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row

    def _add_holding(self, code):
        self._exec(
            "INSERT INTO holding(fund_code,hold_amount,created_at) "
            "VALUES(?,?,datetime('now'))",
            (code, 1000.0),
        )

    def _add_nav(self, code, days_ago):
        d = (datetime.now().date() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        self._exec(
            "INSERT OR REPLACE INTO fund_nav_history(fund_code,nav_date,nav) "
            "VALUES(?,?,?)",
            (code, d, 1.23),
        )


class TestDetectNavGaps(_TmpDbMixin, unittest.TestCase):
    def test_no_holdings_records_ok(self):
        n = scheduler._detect_nav_gaps()
        self.assertEqual(n, 0)
        row = self._last_gap_run()
        self.assertEqual(row[0], "ok")
        self.assertIsNone(row[2])

    def test_recent_nav_not_flagged(self):
        self._add_holding("020608")
        self._add_nav("020608", 1)
        n = scheduler._detect_nav_gaps()
        self.assertEqual(n, 0)
        self.assertEqual(self._last_gap_run()[0], "ok")

    def test_stale_nav_flagged(self):
        self._add_holding("020608")
        self._add_nav("020608", 10)  # 10 天前 > 阈值 5
        n = scheduler._detect_nav_gaps()
        self.assertEqual(n, 1)
        status, affected, error = self._last_gap_run()
        self.assertEqual(status, "fail")
        self.assertEqual(affected, 1)
        self.assertIn("020608", error)

    def test_no_nav_record_flagged(self):
        self._add_holding("020608")  # 持仓但 fund_nav_history 无记录
        n = scheduler._detect_nav_gaps()
        self.assertEqual(n, 1)
        self.assertEqual(self._last_gap_run()[0], "fail")

    def test_mixed_only_stale_flagged(self):
        self._add_holding("020608")
        self._add_nav("020608", 1)  # 正常
        self._add_holding("005827")
        self._add_nav("005827", 10)  # 断点
        n = scheduler._detect_nav_gaps()
        self.assertEqual(n, 1)
        error = self._last_gap_run()[2]
        self.assertIn("005827", error)
        self.assertNotIn("020608", error)

    def test_threshold_boundary(self):
        # 阈值 5 天:cutoff = today-5。nav_date = today-5 应不告警(>= cutoff),
        # today-6 应告警。边界用 today-5 测不告警。
        self._add_holding("020608")
        self._add_nav("020608", 5)
        n = scheduler._detect_nav_gaps()
        self.assertEqual(n, 0)


class TestStartNavGapCheck(unittest.TestCase):
    def test_returns_daemon_thread(self):
        t = scheduler.start_nav_gap_check(interval_hours=24, run_now=False)
        self.assertTrue(t.daemon)
        self.assertTrue(t.is_alive())


if __name__ == "__main__":
    unittest.main()
