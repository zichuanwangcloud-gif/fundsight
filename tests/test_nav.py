# -*- coding: utf-8 -*-
"""backend.datasource.akshare_nav.refresh_nav 单元测试。

核心守护:收盘官方净值写入 fund_quote 的 nav/nav_date 列时,
不得覆盖 fundgz 写入的 dwjz(昨日单位净值)—— 估值/净值字段分离。

不发起真实网络请求:mock fetch_nav。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.datasource import akshare_nav
from backend.models import db as db_mod


class TestRefreshNav(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _conn(self):
        return sqlite3.connect(self.path)

    def _seed_fundgz_row(self, code):
        # 模拟 fundgz 先写入的盘中估值行(含 dwjz=昨日净值)
        conn = self._conn()
        conn.execute(
            "INSERT INTO fund_quote(fund_code,name,dwjz,gsz,gszzl,gztime,updated_at)"
            " VALUES (?,?,?,?,?,?,datetime('now'))",
            (code, "测试基金", 1.2000, 1.2100, 0.83, "2026-07-09 15:00"),
        )
        conn.commit()
        conn.close()

    def test_nav_written_without_touching_dwjz(self):
        code = "020608"
        self._seed_fundgz_row(code)

        fake = {"fund_code": code, "name": "测试基金", "nav": 1.2345, "nav_date": "2026-07-08"}
        with patch.object(akshare_nav, "fetch_nav", return_value=fake):
            conn = self._conn()
            conn.row_factory = sqlite3.Row
            n = akshare_nav.refresh_nav(conn, [code])
            conn.close()
        self.assertEqual(n, 1)

        conn = self._conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM fund_quote WHERE fund_code=?", (code,)).fetchone()
        conn.close()
        # nav / nav_date 已写入
        self.assertEqual(row["nav"], 1.2345)
        self.assertEqual(row["nav_date"], "2026-07-08")
        # 关键:dwjz 仍是 fundgz 的昨日净值,未被覆盖
        self.assertEqual(row["dwjz"], 1.2000)
        # gsz 估值也未被动
        self.assertEqual(row["gsz"], 1.2100)

    def test_nav_upsert_when_row_absent(self):
        # 该基金还没有 fundgz 行时,refresh_nav 应插入新行
        code = "005827"
        fake = {"fund_code": code, "name": "易方达蓝筹", "nav": 1.5000, "nav_date": "2026-07-08"}
        with patch.object(akshare_nav, "fetch_nav", return_value=fake):
            conn = self._conn()
            conn.row_factory = sqlite3.Row
            akshare_nav.refresh_nav(conn, [code])
            conn.close()

        conn = self._conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM fund_quote WHERE fund_code=?", (code,)).fetchone()
        conn.close()
        self.assertEqual(row["nav"], 1.5000)
        self.assertIsNone(row["dwjz"])  # 没有 fundgz 数据,dwjz 保持空

    def test_none_result_skipped(self):
        code = "000001"
        with patch.object(akshare_nav, "fetch_nav", return_value=None):
            conn = self._conn()
            conn.row_factory = sqlite3.Row
            n = akshare_nav.refresh_nav(conn, [code])
            conn.close()
        self.assertEqual(n, 0)

        conn = self._conn()
        row = conn.execute("SELECT * FROM fund_quote WHERE fund_code=?", (code,)).fetchone()
        conn.close()
        self.assertIsNone(row)  # 未写入


if __name__ == "__main__":
    unittest.main()
