# -*- coding: utf-8 -*-
"""大盘指数条 —— datasource/market_index.py + GET /api/market/indices 单元测试。

覆盖:
- parse_indices: 离线报文解析(不发真实网络);null data / 缺 diff / "-" 字段兜底。
- refresh_indices: 注入离线 fetch 写 market_index;空态返回 0 不动快照。
- indices_handler: 只读缓存、按 DISPLAY_CODES 展示序返回、空态降级。

用临时库隔离,不污染其他测试用到的数据库(与 test_market.py 同构)。
"""
import json
import os
import sqlite3
import tempfile
import unittest

from backend.api import market
from backend.api._router import Ctx
from backend.datasource import market_index
from backend.models import db as db_mod


# 离线样本:仿 push2 ulist 报文(含一个 "-" 停牌/盘前字段的指数)
_SAMPLE = json.dumps({
    "rc": 0,
    "data": {"diff": [
        {"f2": 3050.12, "f3": 1.23, "f4": 37.10, "f12": "000001", "f13": 1, "f14": "上证指数"},
        {"f2": 9800.50, "f3": -0.45, "f4": -44.20, "f12": "399001", "f13": 0, "f14": "深证成指"},
        {"f2": 1950.30, "f3": 0.00, "f4": 0.00, "f12": "399006", "f13": 0, "f14": "创业板指"},
        {"f2": "-", "f3": "-", "f4": "-", "f12": "000300", "f13": 1, "f14": "沪深300"},
    ]},
})


class ParseIndicesTest(unittest.TestCase):
    def test_parse_basic(self):
        rows = market_index.parse_indices(_SAMPLE)
        self.assertEqual(len(rows), 4)
        sh = rows[0]
        self.assertEqual(sh["code"], "000001")
        self.assertEqual(sh["name"], "上证指数")
        self.assertAlmostEqual(sh["price"], 3050.12)
        self.assertAlmostEqual(sh["change"], 37.10)
        self.assertAlmostEqual(sh["change_pct"], 1.23)

    def test_parse_dash_fields_become_none(self):
        rows = market_index.parse_indices(_SAMPLE)
        hs300 = next(r for r in rows if r["code"] == "000300")
        self.assertIsNone(hs300["price"])
        self.assertIsNone(hs300["change"])
        self.assertIsNone(hs300["change_pct"])
        self.assertEqual(hs300["name"], "沪深300")  # 名称仍在

    def test_parse_bad_payload_returns_empty(self):
        self.assertEqual(market_index.parse_indices("not json"), [])
        self.assertEqual(market_index.parse_indices(json.dumps({"data": None})), [])
        self.assertEqual(market_index.parse_indices(json.dumps({})), [])

    def test_parse_skips_rows_without_code(self):
        raw = json.dumps({"data": {"diff": [{"f14": "无代码", "f2": 1.0}]}})
        self.assertEqual(market_index.parse_indices(raw), [])


class IndexDbTestBase(unittest.TestCase):
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


class RefreshIndicesTest(IndexDbTestBase):
    def test_refresh_writes_rows(self):
        conn = self._conn()
        n = market_index.refresh_indices(
            conn, fetch=lambda: market_index.parse_indices(_SAMPLE))
        conn.close()
        self.assertEqual(n, 4)
        conn = self._conn()
        rows = conn.execute(
            "SELECT code,name,price,change_pct FROM market_index ORDER BY code").fetchall()
        conn.close()
        codes = {r["code"] for r in rows}
        self.assertEqual(codes, {"000001", "399001", "399006", "000300"})
        sh = next(r for r in rows if r["code"] == "000001")
        self.assertAlmostEqual(sh["price"], 3050.12)

    def test_refresh_upserts_on_conflict(self):
        conn = self._conn()
        market_index.refresh_indices(conn, fetch=lambda: market_index.parse_indices(_SAMPLE))
        # 第二次喂更新值,应覆盖而非重复
        updated = json.dumps({"data": {"diff": [
            {"f2": 3100.00, "f3": 2.0, "f4": 60.0, "f12": "000001", "f14": "上证指数"}]}})
        market_index.refresh_indices(conn, fetch=lambda: market_index.parse_indices(updated))
        conn.close()
        conn = self._conn()
        cnt = conn.execute("SELECT COUNT(*) FROM market_index WHERE code='000001'").fetchone()[0]
        price = conn.execute("SELECT price FROM market_index WHERE code='000001'").fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 1)
        self.assertAlmostEqual(price, 3100.00)

    def test_refresh_empty_fetch_returns_zero(self):
        conn = self._conn()
        n = market_index.refresh_indices(conn, fetch=lambda: [])
        conn.close()
        self.assertEqual(n, 0)
        conn = self._conn()
        cnt = conn.execute("SELECT COUNT(*) FROM market_index").fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 0)


class IndicesHandlerTest(IndexDbTestBase):
    def test_handler_reads_and_orders(self):
        conn = self._conn()
        market_index.refresh_indices(conn, fetch=lambda: market_index.parse_indices(_SAMPLE))
        conn.close()
        result = market.indices_handler(Ctx())
        codes = [it["code"] for it in result["indices"]]
        # 按 DISPLAY_CODES 展示序:上证/深证/创业板/沪深300
        self.assertEqual(codes, ["000001", "399001", "399006", "000300"])
        self.assertIsNotNone(result["updated_at"])

    def test_handler_empty_cache_degrades(self):
        result = market.indices_handler(Ctx())
        self.assertEqual(result["indices"], [])
        self.assertIsNone(result["updated_at"])


if __name__ == "__main__":
    unittest.main()
