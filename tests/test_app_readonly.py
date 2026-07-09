# -*- coding: utf-8 -*-
"""守护「业务层只读缓存」架构红线。

list_holdings() 必须只读 fund_quote 缓存,绝不在请求路径上触发外部抓取
(refresh_quotes)。抓取由 scheduler 后台完成。

用临时库隔离,mock 抓取层验证其未被调用。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend import app as app_mod
from backend.models import db as db_mod


class TestListHoldingsReadOnly(unittest.TestCase):
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
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def _add_holding(self, code="020608", amount=10000.0):
        conn = self._conn()
        conn.execute(
            "INSERT INTO holding(fund_code,hold_amount,cost_amount,created_at)"
            " VALUES (?,?,?,datetime('now'))",
            (code, amount, 8500.0),
        )
        conn.commit()
        conn.close()

    def _seed_quote(self, code="020608"):
        conn = self._conn()
        conn.execute(
            "INSERT INTO fund_quote(fund_code,name,dwjz,gsz,gszzl,gztime,updated_at)"
            " VALUES (?,?,?,?,?,?,datetime('now'))",
            (code, "测试基金", 1.0000, 1.0500, 5.0, "2026-07-09 11:30"),
        )
        conn.commit()
        conn.close()

    def test_list_holdings_does_not_trigger_fetch(self):
        # 核心红线:list_holdings 不得调用 refresh_quotes
        self._add_holding()
        self._seed_quote()
        with patch("backend.datasource.fundgz.refresh_quotes") as mock_rq:
            result = app_mod.list_holdings()
        mock_rq.assert_not_called()
        self.assertEqual(len(result["items"]), 1)

    def test_reads_from_cache(self):
        # 缓存里有估值 → list_holdings 应正确返回
        self._add_holding()
        self._seed_quote()
        result = app_mod.list_holdings()
        item = result["items"][0]
        self.assertEqual(item["gsz"], 1.0500)
        self.assertEqual(item["gszzl"], 5.0)
        self.assertEqual(item["today_pl"], 500.0)  # 10000/1.0*(1.05-1.0)

    def test_no_cache_yet_returns_base(self):
        # 刚加持仓、缓存还没有 → 返回基础字段,不报错、不抓取
        self._add_holding()
        with patch("backend.datasource.fundgz.refresh_quotes") as mock_rq:
            result = app_mod.list_holdings()
        mock_rq.assert_not_called()
        item = result["items"][0]
        self.assertEqual(item["fund_code"], "020608")
        self.assertNotIn("gsz", item)


if __name__ == "__main__":
    unittest.main()
