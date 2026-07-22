# -*- coding: utf-8 -*-
"""同类对比走势(P3)—— datasource/fund_compare.py + GET /api/fund/{code}/compare 单元测试。

覆盖:
- parse_compare: 离线 pingzhongdata 片段解析(不发网络);3 序列归一 self/peer/hs300、
  时间戳→日期、值映射、无变量→[]。
- refresh_compare: 注入离线 fetch 写表、先删后插、失败保留旧序列。
- get_fund_compare: 只读分组排序、空态(有 profile 时不触发按需抓取)。

用临时库隔离。
"""
import os
import re
import sqlite3
import tempfile
import unittest

from backend.api import fund_detail
from backend.api._router import Ctx
from backend.datasource import fund_compare
from backend.models import db as db_mod


# 离线样本:仿 pingzhongdata 的 Data_grandTotal(3 序列,各 2 点)
_SAMPLE = (
    'var Data_grandTotal = ['
    '{"name":"易方达消费行业股票","data":[[1737388800000,0],[1737475200000,1.5]]},'
    '{"name":"同类平均","data":[[1737388800000,0],[1737475200000,0.3]]},'
    '{"name":"沪深300","data":[[1737388800000,0],[1737475200000,0.4]]}'
    '];'
)


class ParseCompareTest(unittest.TestCase):
    def test_parse_three_series_keyed(self):
        series = fund_compare.parse_compare(_SAMPLE)
        by_key = {s["key"]: s for s in series}
        self.assertEqual(set(by_key), {"self", "peer", "hs300"})
        self.assertEqual(by_key["self"]["name"], "本基金")
        self.assertEqual(by_key["peer"]["name"], "同类平均")
        self.assertEqual(by_key["hs300"]["name"], "沪深300")
        self.assertEqual(len(by_key["self"]["points"]), 2)
        self.assertAlmostEqual(by_key["self"]["points"][1]["value"], 1.5)
        self.assertAlmostEqual(by_key["peer"]["points"][1]["value"], 0.3)

    def test_parse_ts_to_date(self):
        series = fund_compare.parse_compare(_SAMPLE)
        d = series[0]["points"][0]["date"]
        self.assertRegex(d, r"^\d{4}-\d{2}-\d{2}$")

    def test_parse_no_grandtotal_returns_empty(self):
        self.assertEqual(fund_compare.parse_compare("var x = 1;"), [])
        self.assertEqual(fund_compare.parse_compare(""), [])


class CompareDbTestBase(unittest.TestCase):
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


class RefreshCompareTest(CompareDbTestBase):
    def test_refresh_writes_series(self):
        conn = self._conn()
        n = fund_compare.refresh_compare(
            conn, ["110022"], fetch=lambda c: fund_compare.parse_compare(_SAMPLE))
        conn.close()
        self.assertEqual(n, 1)
        conn = self._conn()
        keys = {r["series_key"] for r in conn.execute(
            "SELECT DISTINCT series_key FROM fund_compare_trend WHERE fund_code='110022'")}
        cnt = conn.execute("SELECT COUNT(*) FROM fund_compare_trend WHERE fund_code='110022'").fetchone()[0]
        conn.close()
        self.assertEqual(keys, {"self", "peer", "hs300"})
        self.assertEqual(cnt, 6)  # 3 序列 × 2 点

    def test_refresh_delete_then_insert(self):
        conn = self._conn()
        fund_compare.refresh_compare(conn, ["110022"], fetch=lambda c: fund_compare.parse_compare(_SAMPLE))
        one = ('var Data_grandTotal = ['
               '{"name":"本基金","data":[[1737388800000,9.9]]}];')
        fund_compare.refresh_compare(conn, ["110022"], fetch=lambda c: fund_compare.parse_compare(one))
        conn.close()
        conn = self._conn()
        cnt = conn.execute("SELECT COUNT(*) FROM fund_compare_trend WHERE fund_code='110022'").fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 1)  # 旧 6 行被替换为 1 行

    def test_refresh_empty_keeps_old(self):
        conn = self._conn()
        fund_compare.refresh_compare(conn, ["110022"], fetch=lambda c: fund_compare.parse_compare(_SAMPLE))
        n = fund_compare.refresh_compare(conn, ["110022"], fetch=lambda c: [])
        conn.close()
        self.assertEqual(n, 0)
        conn = self._conn()
        cnt = conn.execute("SELECT COUNT(*) FROM fund_compare_trend").fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 6)


class CompareHandlerTest(CompareDbTestBase):
    def test_handler_grouped_ordered(self):
        conn = self._conn()
        fund_compare.refresh_compare(conn, ["110022"], fetch=lambda c: fund_compare.parse_compare(_SAMPLE))
        conn.close()
        result = fund_detail.get_fund_compare(Ctx(params={"code": "110022"}))
        keys = [s["key"] for s in result["series"]]
        self.assertEqual(keys, ["self", "peer", "hs300"])  # 固定展示序
        self.assertEqual(len(result["series"][0]["points"]), 2)

    def test_handler_empty_with_profile_no_fetch(self):
        conn = self._conn()
        conn.execute("INSERT INTO fund_profile(fund_code,name,updated_at) "
                     "VALUES('999999','某基金',datetime('now','localtime'))")
        conn.commit()
        conn.close()
        result = fund_detail.get_fund_compare(Ctx(params={"code": "999999"}))
        self.assertEqual(result["series"], [])

    def test_handler_missing_code(self):
        code, _ = fund_detail.get_fund_compare(Ctx(params={}))
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
