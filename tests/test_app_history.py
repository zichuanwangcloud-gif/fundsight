# -*- coding: utf-8 -*-
"""backend.app 的 /api/nav_history 读缓存单元测试。

守 M6 只读红线:nav_history() 只读 fund_nav_history 缓存,不触发外部抓取。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend import app as app_mod
from backend.models import db as db_mod


class TestNavHistoryApi(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)
        conn = sqlite3.connect(self.path)
        rows = [("020608", f"2026-04-{d:02d}", 1.0 + d / 100) for d in range(1, 29)]
        conn.executemany(
            "INSERT INTO fund_nav_history(fund_code,nav_date,nav) VALUES (?,?,?)", rows
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def test_returns_points_ascending(self):
        result = app_mod.nav_history("020608", days=90)
        self.assertEqual(result["code"], "020608")
        pts = result["points"]
        self.assertEqual(len(pts), 28)
        # 升序
        self.assertLess(pts[0]["d"], pts[-1]["d"])
        self.assertIn("v", pts[0])

    def test_days_limit(self):
        # 只取最近 N 天
        result = app_mod.nav_history("020608", days=10)
        self.assertEqual(len(result["points"]), 10)
        # 应是最后 10 天(升序返回)
        self.assertEqual(result["points"][-1]["d"], "2026-04-28")

    def test_does_not_trigger_fetch(self):
        # 守只读红线:不触发历史抓取
        with patch("backend.datasource.nav_history.refresh_nav_history") as m1, \
             patch("backend.datasource.nav_history.fetch_nav_history") as m2:
            app_mod.nav_history("020608", days=90)
        m1.assert_not_called()
        m2.assert_not_called()

    def test_empty_for_unknown_code(self):
        result = app_mod.nav_history("999999", days=90)
        self.assertEqual(result["points"], [])


if __name__ == "__main__":
    unittest.main()
