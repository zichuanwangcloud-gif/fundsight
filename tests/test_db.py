# -*- coding: utf-8 -*-
"""backend.models.db 单元测试。

使用临时数据库文件（monkeypatch db.DB_PATH），避免污染真实的
data/fundsight.db。每个测试方法结束后清理临时文件。
"""
import os
import tempfile
import unittest

from backend.models import db


class TestInitDb(unittest.TestCase):
    def setUp(self):
        # 用临时文件路径替换 DB_PATH，隔离对真实数据库的影响
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)  # 让 sqlite3.connect 自己创建全新文件
        self._tmp_path = path
        self._orig_path = db.DB_PATH
        db.DB_PATH = path

    def tearDown(self):
        db.DB_PATH = self._orig_path
        if os.path.exists(self._tmp_path):
            os.remove(self._tmp_path)

    def test_init_db_creates_three_tables(self):
        db.init_db(with_seed=True)
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        conn.close()
        table_names = {r["name"] for r in rows}
        # sqlite 因 holding 表使用 AUTOINCREMENT 会自动附加 sqlite_sequence，
        # 这里只断言我们关心的 3 张业务表都存在，不强制要求集合完全相等
        self.assertTrue(
            {"fund_list", "fund_quote", "holding"}.issubset(table_names)
        )

    def test_init_db_seeds_15_funds(self):
        db.init_db(with_seed=True)
        conn = db.get_conn()
        n = conn.execute("SELECT COUNT(*) AS n FROM fund_list").fetchone()["n"]
        conn.close()
        self.assertEqual(n, 15)
        self.assertEqual(len(db.SEED_FUNDS), 15)

    def test_init_db_without_seed_leaves_fund_list_empty(self):
        db.init_db(with_seed=False)
        conn = db.get_conn()
        n = conn.execute("SELECT COUNT(*) AS n FROM fund_list").fetchone()["n"]
        conn.close()
        self.assertEqual(n, 0)

    def test_init_db_is_idempotent_no_duplicate_seed(self):
        db.init_db(with_seed=True)
        db.init_db(with_seed=True)  # 再跑一次不应重复插入种子
        conn = db.get_conn()
        n = conn.execute("SELECT COUNT(*) AS n FROM fund_list").fetchone()["n"]
        conn.close()
        self.assertEqual(n, 15)

    def test_get_conn_can_query_after_init(self):
        db.init_db(with_seed=True)
        conn = db.get_conn()
        row = conn.execute(
            "SELECT fund_code, name FROM fund_list WHERE fund_code=?",
            ("020608",),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["fund_code"], "020608")


if __name__ == "__main__":
    unittest.main()
