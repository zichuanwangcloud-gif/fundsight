# -*- coding: utf-8 -*-
"""基金重仓股(F10)—— datasource/fund_holdings.py + GET /api/fund/{code}/holdings 单元测试。

覆盖:
- parse_holdings: 离线 F10 报文解析(不发网络);代码/名称/占比/报告期映射、无 tbody→[]。
- refresh_holdings: 注入离线 fetch 写表、先删后插;抓取失败保留旧持仓。
- get_fund_holdings: 只读排序、空态(有 profile 时不触发按需抓取)。

用临时库隔离(与 test_market_index.py 同构)。
"""
import os
import sqlite3
import tempfile
import unittest

from backend.api import fund_detail
from backend.api._router import Ctx
from backend.datasource import fund_holdings
from backend.models import db as db_mod


# 离线样本:仿 F10 jjcc 报文(2 行,含动态 span 占位列 + 报告期)
_SAMPLE = (
    'var apidata={ content:"<div class=\'box\'><h4 class=\'t\'>'
    '易方达消费行业股票&nbsp;&nbsp;2026年2季度股票投资明细</h4>'
    '<table><tbody>'
    "<tr><td>1</td><td><a href='//quote.eastmoney.com/unify/r/1.600519'>600519</a></td>"
    "<td class='tol'><a href='x'>贵州茅台</a></td>"
    "<td class='tor'><span data-id='dq600519'></span></td>"
    "<td class='tor'><span data-id='zd600519'></span></td>"
    "<td class='xglj'><a href='y'>变动详情</a></td>"
    "<td class='tor'>9.77%</td><td class='tor'>80.00</td><td class='tor'>94,838.96</td></tr>"
    "<tr><td>2</td><td><a href='//quote.eastmoney.com/unify/r/0.000333'>000333</a></td>"
    "<td class='tol'><a href='x'>美的集团</a></td>"
    "<td class='tor'><span data-id='dq000333'></span></td>"
    "<td class='tor'><span data-id='zd000333'></span></td>"
    "<td class='xglj'><a href='y'>变动详情</a></td>"
    "<td class='tor'>9.31%</td><td class='tor'>1,196.52</td><td class='tor'>90,372.87</td></tr>"
    '</tbody></table></div>",arryear:[2026],curyear:2026};'
)


class ParseHoldingsTest(unittest.TestCase):
    def test_parse_basic_and_mapping(self):
        rows = fund_holdings.parse_holdings(_SAMPLE)
        self.assertEqual(len(rows), 2)
        top = rows[0]
        self.assertEqual(top["rank"], 1)
        self.assertEqual(top["stock_code"], "600519")
        self.assertEqual(top["stock_name"], "贵州茅台")
        self.assertAlmostEqual(top["weight"], 9.77)
        self.assertEqual(top["report_period"], "2026年2季度")
        self.assertEqual(rows[1]["stock_code"], "000333")
        self.assertAlmostEqual(rows[1]["weight"], 9.31)

    def test_parse_no_tbody_returns_empty(self):
        self.assertEqual(fund_holdings.parse_holdings("var apidata={content:\"no table\"};"), [])
        self.assertEqual(fund_holdings.parse_holdings(""), [])

    def test_parse_skips_row_without_stock(self):
        raw = 'x<tbody><tr><td>1</td><td>无链接</td><td>无名</td></tr></tbody>y'
        self.assertEqual(fund_holdings.parse_holdings(raw), [])


class HoldingsDbTestBase(unittest.TestCase):
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


class RefreshHoldingsTest(HoldingsDbTestBase):
    def test_refresh_writes_rows(self):
        conn = self._conn()
        n = fund_holdings.refresh_holdings(
            conn, ["110022"], fetch=lambda code: fund_holdings.parse_holdings(_SAMPLE))
        conn.close()
        self.assertEqual(n, 1)
        conn = self._conn()
        rows = conn.execute(
            "SELECT rank,stock_code,weight FROM fund_holding_stock "
            "WHERE fund_code='110022' ORDER BY rank").fetchall()
        conn.close()
        self.assertEqual([r["stock_code"] for r in rows], ["600519", "000333"])
        self.assertAlmostEqual(rows[0]["weight"], 9.77)

    def test_refresh_delete_then_insert(self):
        conn = self._conn()
        fund_holdings.refresh_holdings(conn, ["110022"], fetch=lambda c: fund_holdings.parse_holdings(_SAMPLE))
        one = ('x<tbody>'
               "<tr><td>1</td><td><a href='//quote.eastmoney.com/unify/r/1.601318'>601318</a></td>"
               "<td class='tol'><a>中国平安</a></td><td class='tor'><span></span></td>"
               "<td class='tor'><span></span></td><td class='xglj'></td>"
               "<td class='tor'>5.00%</td></tr></tbody>y")
        fund_holdings.refresh_holdings(conn, ["110022"], fetch=lambda c: fund_holdings.parse_holdings(one))
        conn.close()
        conn = self._conn()
        rows = conn.execute("SELECT stock_code FROM fund_holding_stock WHERE fund_code='110022'").fetchall()
        conn.close()
        self.assertEqual([r["stock_code"] for r in rows], ["601318"])  # 旧 2 只被替换

    def test_refresh_empty_keeps_old(self):
        conn = self._conn()
        fund_holdings.refresh_holdings(conn, ["110022"], fetch=lambda c: fund_holdings.parse_holdings(_SAMPLE))
        n = fund_holdings.refresh_holdings(conn, ["110022"], fetch=lambda c: [])
        conn.close()
        self.assertEqual(n, 0)
        conn = self._conn()
        cnt = conn.execute("SELECT COUNT(*) FROM fund_holding_stock").fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 2)


class HoldingsHandlerTest(HoldingsDbTestBase):
    def _seed_holdings(self):
        conn = self._conn()
        fund_holdings.refresh_holdings(conn, ["110022"], fetch=lambda c: fund_holdings.parse_holdings(_SAMPLE))
        conn.close()

    def _seed_profile_only(self, code):
        # 写一条 profile,使 handler 空持仓时不触发按需网络抓取
        conn = self._conn()
        conn.execute(
            "INSERT INTO fund_profile(fund_code,name,updated_at) VALUES(?,?,datetime('now','localtime'))",
            (code, "某基金"))
        conn.commit()
        conn.close()

    def test_handler_reads_ordered(self):
        self._seed_holdings()
        result = fund_detail.get_fund_holdings(Ctx(params={"code": "110022"}))
        self.assertEqual(result["code"], "110022")
        self.assertEqual(result["period"], "2026年2季度")
        codes = [h["stock_code"] for h in result["holdings"]]
        self.assertEqual(codes, ["600519", "000333"])

    def test_handler_empty_with_profile_no_fetch(self):
        self._seed_profile_only("999999")  # 有 profile、无持仓 → 不触发按需抓取
        result = fund_detail.get_fund_holdings(Ctx(params={"code": "999999"}))
        self.assertEqual(result["holdings"], [])
        self.assertIsNone(result["period"])

    def test_handler_missing_code(self):
        code, _ = fund_detail.get_fund_holdings(Ctx(params={}))
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
