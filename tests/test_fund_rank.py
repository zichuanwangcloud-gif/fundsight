# -*- coding: utf-8 -*-
"""基金排行榜 —— datasource/fund_rank.py + GET /api/rank* 单元测试。

覆盖:
- parse_rank: 离线真实报文解析(不发网络);字段下标映射、空字段→None、坏报文→[]。
- refresh_rank: 注入离线 fetch 写 fund_rank;先删后插(重刷替换)。
- rank_handler: 只读、cat/period 过滤与非法回退、按 rank 排序、空态。
- rank_meta_handler: 类目/区间清单。

用临时库隔离(与 test_market_index.py 同构)。
"""
import os
import sqlite3
import tempfile
import unittest

from backend.api import rank
from backend.api._router import Ctx
from backend.datasource import fund_rank
from backend.models import db as db_mod


# 离线样本:2026-07-21 实测捕获的 gp/近1年 榜单报文(3 只)
_SAMPLE = (
    'var rankData = {datas:['
    '"006502,财通集成电路产业股票A,CTJCDLCYGPA,2026-07-21,7.047,7.047,9.45,-17.06,'
    '-31.11,25.98,74.41,212.6,352.4,344.91,78.15,604.7,2018-11-29,1,604.7,1.50%,0.15%,1,0.15%,1,207.37",'
    '"005825,申万菱信智能驱动股票A,SWLXZNQDGPA,2026-07-21,9.2903,9.7327,13.78,-11.59,'
    '-5.72,54.85,64.3,212.54,290.28,251.29,89.36,983.52,2018-06-13,1,983.519123,1.50%,0.15%,1,0.15%,1,209.92",'
    '"015159,申万菱信智能驱动股票C,SWLXZNQDGPC,2026-07-21,9.0774,9.5339,13.78,-11.6,'
    '-5.76,54.69,63.98,211.32,287.13,247.08,88.95,178.28,2022-02-25,1,178.284103,,0.00%,,,,"'
    '],allRecords:1076,pageIndex:1,pageNum:3,allPages:359,allNum:20022,'
    'zs_count:4428,gp_count:1076,hh_count:8466,zq_count:4837,qdii_count:221,fof_count:994};'
)


class ParseRankTest(unittest.TestCase):
    def test_parse_basic_and_field_mapping(self):
        rows = fund_rank.parse_rank(_SAMPLE)
        self.assertEqual(len(rows), 3)
        top = rows[0]
        self.assertEqual(top["fund_code"], "006502")
        self.assertEqual(top["name"], "财通集成电路产业股票A")
        self.assertEqual(top["nav_date"], "2026-07-21")
        self.assertAlmostEqual(top["nav"], 7.047)
        self.assertAlmostEqual(top["r_1m"], -31.11)
        self.assertAlmostEqual(top["r_3m"], 25.98)
        self.assertAlmostEqual(top["r_6m"], 74.41)
        self.assertAlmostEqual(top["r_1y"], 212.6)
        self.assertAlmostEqual(top["r_ytd"], 78.15)

    def test_parse_empty_field_becomes_none(self):
        raw = ('var rankData = {datas:['
               '"000001,某基金,MJJ,2026-07-21,1.0,1.0,0,0,,3.3,,5.5,,,7.7,,2020-01-01"'
               '],allRecords:1};')
        rows = fund_rank.parse_rank(raw)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIsNone(r["r_1m"])   # 第 8 段空
        self.assertAlmostEqual(r["r_3m"], 3.3)
        self.assertIsNone(r["r_6m"])   # 第 10 段空
        self.assertAlmostEqual(r["r_1y"], 5.5)
        self.assertAlmostEqual(r["r_ytd"], 7.7)

    def test_parse_bad_payload_returns_empty(self):
        self.assertEqual(fund_rank.parse_rank("garbage"), [])
        self.assertEqual(fund_rank.parse_rank("var rankData = {};"), [])

    def test_parse_skips_short_rows(self):
        raw = 'var rankData = {datas:["001,只有几段,x"],allRecords:1};'
        self.assertEqual(fund_rank.parse_rank(raw), [])


class RankDbTestBase(unittest.TestCase):
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


class RefreshRankTest(RankDbTestBase):
    def test_refresh_writes_ranked_rows(self):
        conn = self._conn()
        n = fund_rank.refresh_rank(
            conn,
            fetch=lambda ft, sc, top: fund_rank.parse_rank(_SAMPLE),
            categories=[("gp", "股票")],
            periods=[("1y", "近1年", "1nzf")],
        )
        conn.close()
        self.assertEqual(n, 3)
        conn = self._conn()
        rows = conn.execute(
            "SELECT rank,fund_code FROM fund_rank WHERE period='1y' AND category='gp' ORDER BY rank"
        ).fetchall()
        conn.close()
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[0]["fund_code"], "006502")

    def test_refresh_delete_then_insert_replaces(self):
        conn = self._conn()
        args = dict(categories=[("gp", "股票")], periods=[("1y", "近1年", "1nzf")])
        fund_rank.refresh_rank(conn, fetch=lambda ft, sc, top: fund_rank.parse_rank(_SAMPLE), **args)
        # 二次刷新:仅 1 只,应替换掉此前 3 只(先删后插)
        one = ('var rankData = {datas:['
               '"999999,新基金,XJJ,2026-07-22,2.0,2.0,0,0,1,2,3,4,5,6,7,8,2021-01-01"'
               '],allRecords:1};')
        fund_rank.refresh_rank(conn, fetch=lambda ft, sc, top: fund_rank.parse_rank(one), **args)
        conn.close()
        conn = self._conn()
        rows = conn.execute(
            "SELECT rank,fund_code FROM fund_rank WHERE period='1y' AND category='gp'").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fund_code"], "999999")

    def test_refresh_empty_fetch_keeps_old(self):
        conn = self._conn()
        args = dict(categories=[("gp", "股票")], periods=[("1y", "近1年", "1nzf")])
        fund_rank.refresh_rank(conn, fetch=lambda ft, sc, top: fund_rank.parse_rank(_SAMPLE), **args)
        # 抓取失败(空)不应清空已有榜单
        n = fund_rank.refresh_rank(conn, fetch=lambda ft, sc, top: [], **args)
        conn.close()
        self.assertEqual(n, 0)
        conn = self._conn()
        cnt = conn.execute("SELECT COUNT(*) FROM fund_rank").fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 3)


class RankHandlerTest(RankDbTestBase):
    def _seed(self):
        conn = self._conn()
        fund_rank.refresh_rank(
            conn,
            fetch=lambda ft, sc, top: fund_rank.parse_rank(_SAMPLE),
            categories=[("gp", "股票")],
            periods=[("1y", "近1年", "1nzf")],
        )
        conn.close()

    def test_handler_reads_ordered(self):
        self._seed()
        result = rank.rank_handler(Ctx(query={"cat": ["gp"], "period": ["1y"]}))
        self.assertEqual(result["cat"], "gp")
        self.assertEqual(result["period"], "1y")
        codes = [it["fund_code"] for it in result["items"]]
        self.assertEqual(codes, ["006502", "005825", "015159"])
        self.assertIsNotNone(result["updated_at"])

    def test_handler_invalid_params_fallback(self):
        self._seed()
        result = rank.rank_handler(Ctx(query={"cat": ["nonsense"], "period": ["zzz"]}))
        self.assertEqual(result["cat"], "all")     # 非法回退缺省
        self.assertEqual(result["period"], "1y")
        self.assertEqual(result["items"], [])      # all/1y 无数据

    def test_handler_empty_cache(self):
        result = rank.rank_handler(Ctx())
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["updated_at"])

    def test_meta_lists_categories_and_periods(self):
        meta = rank.rank_meta_handler(Ctx())
        cat_keys = [c["key"] for c in meta["categories"]]
        per_keys = [p["key"] for p in meta["periods"]]
        self.assertIn("all", cat_keys)
        self.assertIn("qdii", cat_keys)
        self.assertEqual(per_keys, ["1m", "3m", "6m", "1y", "ytd"])
        self.assertEqual(meta["default_cat"], "all")


if __name__ == "__main__":
    unittest.main()
