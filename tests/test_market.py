# -*- coding: utf-8 -*-
"""市场列表 + 分类 tab —— backend/api/market.py 单元测试(TDD,先写测试)。

覆盖:
- GET /api/categories: fund_type 33 个细类正确归纳到 8 大类,计数准确。
- GET /api/market: 分页正确、q 叠加名称/代码/拼音过滤、cat 大类过滤。
- 只读红线: 绝不触发任何外部抓取(datasource 层零调用 / 零网络)。

用临时库隔离,不污染其他测试用到的数据库。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.api import market
from backend.api._router import Ctx
from backend.models import db as db_mod


class MarketTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)
        self._seed()

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def _insert(self, rows):
        conn = self._conn()
        conn.executemany(
            "INSERT INTO fund_list(fund_code,name,pinyin,fund_type,synced_at) "
            "VALUES (?,?,?,?,datetime('now','localtime'))",
            rows,
        )
        conn.commit()
        conn.close()

    def _seed(self):
        # 覆盖 8 大类 + 若干细分子类型 + 一个未知类型(归入「其他」,不计入 8 大类)
        self._insert([
            ("000001", "华夏成长混合", "hxczhh", "混合型"),
            ("000002", "偏股混合基金", "pghhjj", "混合型-偏股"),
            ("110022", "易方达消费行业股票", "yfdxfhygp", "股票型"),
            ("161725", "招商中证白酒指数", "zszzbjzs", "指数型-股票"),
            ("008888", "国证半导体芯片ETF联接", "gzbdtxpETFljC", "ETF联接"),
            ("003001", "纯债债券基金", "cvzqjj", "债券型-长债"),
            ("003002", "债券指数基金", "zqzsjj", "债券指数"),
            ("000003", "货币市场基金", "hbscjj", "货币市场型"),
            ("012348", "天弘恒生科技指数C", "thhskjzsC", "QDII-指数"),
            ("270042", "广发纳斯达克100指数", "gfnsdk100zs", "QDII"),
            ("501000", "FOF稳健配置一年持有", "FOFwjpz1n", "FOF-偏债"),
            ("180101", "华夏中国交建REITs", "hxzgjjREITs", "REITs"),
            ("999999", "神秘另类基金", "smllfjj", "其他另类"),
        ])


class TestCategories(MarketTestBase):
    def test_eight_categories_returned_in_order(self):
        result = market.categories_handler(Ctx())
        cats = [c["cat"] for c in result]
        self.assertEqual(cats, ["混合", "指数", "债券", "股票", "货币", "FOF", "QDII", "Reits"])

    def test_counts_aggregate_by_keyword(self):
        result = market.categories_handler(Ctx())
        counts = {c["cat"]: c["count"] for c in result}
        # 混合型 + 混合型-偏股 = 2
        self.assertEqual(counts["混合"], 2)
        # 股票型 + 指数型-股票 命中"指数"关键字优先于"股票"? 指数型-股票同时含两词,
        # 归入"指数"(股票只统计不含"指数"字样的股票型)
        self.assertEqual(counts["股票"], 1)  # 仅"股票型"
        # 指数型-股票 + ETF联接 + 债券指数(先归债券,不算入指数) + QDII-指数(先归QDII)
        self.assertEqual(counts["指数"], 2)  # 指数型-股票, ETF联接
        # 纯债债券基金 + 债券指数 = 2
        self.assertEqual(counts["债券"], 2)
        self.assertEqual(counts["货币"], 1)
        self.assertEqual(counts["QDII"], 2)  # QDII-指数, QDII
        self.assertEqual(counts["FOF"], 1)
        self.assertEqual(counts["Reits"], 1)

    def test_total_counted_matches_known_buckets_plus_other(self):
        result = market.categories_handler(Ctx())
        total_known = sum(c["count"] for c in result)
        # 13 条种子里有 1 条"其他另类"归入「其他」,不计入 8 大类
        self.assertEqual(total_known, 12)


class TestMarketList(MarketTestBase):
    def test_default_pagination_size_20(self):
        result = market.market_handler(Ctx())
        self.assertEqual(result["size"], 20)
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["total"], 13)
        self.assertEqual(len(result["items"]), 13)  # 13 条不足一页,全部返回

    def test_pagination_across_pages(self):
        # 追加到 25 条,验证跨页
        self._insert([(f"90{i:04d}", f"批量基金{i}", f"plfj{i}", "混合型") for i in range(15)])
        page1 = market.market_handler(Ctx(query={"page": ["1"], "size": ["20"]}))
        page2 = market.market_handler(Ctx(query={"page": ["2"], "size": ["20"]}))
        self.assertEqual(page1["total"], 28)
        self.assertEqual(len(page1["items"]), 20)
        self.assertEqual(len(page2["items"]), 8)

    def test_cat_filter(self):
        result = market.market_handler(Ctx(query={"cat": ["QDII"]}))
        self.assertEqual(result["total"], 2)
        codes = {it["fund_code"] for it in result["items"]}
        self.assertEqual(codes, {"012348", "270042"})

    def test_q_filter_by_name(self):
        result = market.market_handler(Ctx(query={"q": ["消费"]}))
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["fund_code"], "110022")

    def test_q_filter_by_code(self):
        result = market.market_handler(Ctx(query={"q": ["161725"]}))
        self.assertEqual(result["total"], 1)

    def test_q_filter_by_pinyin(self):
        result = market.market_handler(Ctx(query={"q": ["hxczhh"]}))
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["fund_code"], "000001")

    def test_cat_and_q_combined(self):
        result = market.market_handler(Ctx(query={"cat": ["指数"], "q": ["ETF"]}))
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["fund_code"], "008888")

    def test_unknown_cat_returns_empty(self):
        result = market.market_handler(Ctx(query={"cat": ["不存在的大类"]}))
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["items"], [])

    def test_item_shape(self):
        result = market.market_handler(Ctx(query={"q": ["华夏成长"]}))
        item = result["items"][0]
        self.assertEqual(set(item.keys()), {"fund_code", "name", "fund_type"})


class TestReadOnlyGuard(MarketTestBase):
    """市场列表是唯一只读缓存出口,绝不该触发外部抓取。"""

    def test_categories_never_calls_external_fetch(self):
        with patch("backend.datasource.fund_list_sync.fetch_all_funds") as mock_fetch:
            market.categories_handler(Ctx())
        mock_fetch.assert_not_called()

    def test_market_list_never_calls_external_fetch(self):
        with patch("backend.datasource.fund_list_sync.fetch_all_funds") as mock_fetch:
            market.market_handler(Ctx(query={"q": ["混合"]}))
        mock_fetch.assert_not_called()

    def test_market_list_never_opens_network(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            market.categories_handler(Ctx())
            market.market_handler(Ctx())
        mock_urlopen.assert_not_called()


class TestRoutesRegistered(unittest.TestCase):
    def test_routes_exported(self):
        patterns = {(m, p) for m, p, _ in market.ROUTES}
        self.assertIn(("GET", "/api/categories"), patterns)
        self.assertIn(("GET", "/api/market"), patterns)


if __name__ == "__main__":
    unittest.main()
