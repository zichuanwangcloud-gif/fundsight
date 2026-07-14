# -*- coding: utf-8 -*-
"""PRD-02 分红复权净值单元测试。

构造一份含分红日的离线 pingzhongdata 样本(单位净值在分红日断崖跳跌、
累计净值平滑不跳),验证:
  - fetch_nav_history 解析 Data_ACWorthTrend,返回四元组含累计净值
  - _compute_adj_return 基于累计净值算涨跌幅,消除分红日假大跌
  - refresh_nav_history 写入 nav_adj / equity_return_adj
  - returns._compute_periods 跨分红期收益用复权口径,不被分红拉低
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from backend.api import returns
from backend.datasource import nav_history
from backend.models import db as db_mod

# 分红日样本：D3 分红,单位净值 1.02→0.95(假大跌 -6.86%),累计净值 1.020→1.025(平滑)
#   D1 2024-03-12, D2 2024-03-13, D3 2024-03-14(分红日), D4 2024-03-15
SAMPLE_JS_DIVIDEND = (
    'var fS_name = "分红样本";'
    'var Data_netWorthTrend = ['
    '{"x":1710201600000,"y":1.00,"equityReturn":0},'
    '{"x":1710288000000,"y":1.02,"equityReturn":2.0},'
    '{"x":1710374400000,"y":0.95,"equityReturn":-6.86},'
    '{"x":1710460800000,"y":0.96,"equityReturn":1.05}'
    '];'
    'var Data_ACWorthTrend = ['
    '{"x":1710201600000,"y":1.000},'
    '{"x":1710288000000,"y":1.020},'
    '{"x":1710374400000,"y":1.025},'
    '{"x":1710460800000,"y":1.035}'
    '];'
)


def _mock_response(text):
    resp = MagicMock()
    resp.read.return_value = text.encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


class TestFetchParsesAccNav(unittest.TestCase):
    @patch("backend.datasource.nav_history.urllib.request.urlopen")
    def test_returns_four_tuple_with_acc_nav(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(SAMPLE_JS_DIVIDEND)
        series = nav_history.fetch_nav_history("020608")
        self.assertIsNotNone(series)
        self.assertEqual(len(series), 4)
        # 第 2 项单位净值,分红日 D3 跳跌到 0.95
        self.assertEqual(series[2][1], 0.95)
        # 第 3 项累计净值,分红日 D3 平滑不跳
        self.assertEqual(series[2][2], 1.025)
        # 第 4 项 equityReturn 单位口径(分红日假大跌)
        self.assertEqual(series[2][3], -6.86)


class TestComputeAdjReturn(unittest.TestCase):
    def test_dividend_day_no_fake_crash(self):
        """复权口径下,分红日涨跌幅由累计净值算,不再是大跌。"""
        series = [
            ("2024-03-12", 1.00, 1.000, 0.0),
            ("2024-03-13", 1.02, 1.020, 2.0),
            ("2024-03-14", 0.95, 1.025, -6.86),   # 分红日
            ("2024-03-15", 0.96, 1.035, 1.05),
        ]
        adj = nav_history._compute_adj_return(series)
        # 分红日 D3:1.025/1.020-1 = 0.4902 → 0.49(不是 -6.86)
        self.assertAlmostEqual(adj[2], round((1.025 / 1.020 - 1) * 100, 4), places=4)
        self.assertNotAlmostEqual(adj[2], -6.86, places=2)
        # D4:1.035/1.025-1 = 0.9756 → 0.98
        self.assertAlmostEqual(adj[3], round((1.035 / 1.025 - 1) * 100, 4), places=4)
        # D1 无前日累计净值 → 回落 equityReturn(0.0)
        self.assertEqual(adj[0], 0.0)

    def test_missing_acc_falls_back_to_unit_return(self):
        """累计净值缺失时,复权涨跌幅回落到报文 equityReturn(单位口径)。"""
        series = [("2024-03-12", 1.0, None, 1.5), ("2024-03-13", 1.01, None, 0.5)]
        adj = nav_history._compute_adj_return(series)
        self.assertEqual(adj, [1.5, 0.5])


class TestRefreshWritesAdjColumns(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    @patch("backend.datasource.nav_history.fetch_nav_history")
    def test_writes_nav_adj_and_adj_return(self, mock_fetch):
        mock_fetch.return_value = [
            ("2024-03-12", 1.00, 1.000, 0.0),
            ("2024-03-13", 1.02, 1.020, 2.0),
            ("2024-03-14", 0.95, 1.025, -6.86),
            ("2024-03-15", 0.96, 1.035, 1.05),
        ]
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        nav_history.refresh_nav_history(conn, ["020608"])
        rows = conn.execute(
            "SELECT nav_date, nav, nav_adj, equity_return, equity_return_adj "
            "FROM fund_nav_history WHERE fund_code='020608' ORDER BY nav_date"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 4)
        # 分红日:单位净值跳跌、累计净值平滑
        d3 = rows[2]
        self.assertEqual(d3["nav"], 0.95)
        self.assertEqual(d3["nav_adj"], 1.025)
        # equity_return(单位口径)= -6.86 假大跌;equity_return_adj 复权口径平滑
        self.assertEqual(d3["equity_return"], -6.86)
        self.assertAlmostEqual(d3["equity_return_adj"], round((1.025 / 1.020 - 1) * 100, 4), places=4)


class TestReturnsUsesAdjNav(unittest.TestCase):
    """跨分红期收益用复权口径,不被分红日假大跌拉低。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _seed_dividend_series(self):
        conn = sqlite3.connect(self.path)
        rows = [
            ("020608", "2024-03-12", 1.00, 0.0, 1.000, 0.0),
            ("020608", "2024-03-13", 1.02, 2.0, 1.020, 2.0),
            ("020608", "2024-03-14", 0.95, -6.86, 1.025, 0.49),   # 分红日
            ("020608", "2024-03-15", 0.96, 1.05, 1.035, 0.98),
        ]
        conn.executemany(
            "INSERT INTO fund_nav_history("
            "fund_code,nav_date,nav,equity_return,nav_adj,equity_return_adj) "
            "VALUES (?,?,?,?,?,?)", rows,
        )
        conn.commit()
        conn.close()

    def test_max_return_uses_adj_not_unit(self):
        self._seed_dividend_series()
        from backend.models.db import get_conn
        c = get_conn()
        p = returns._compute_periods(c, "020608")
        c.close()
        # 复权口径:1.035/1.000-1 = 3.5%
        self.assertEqual(p["max"], 3.5)
        # 若误用单位口径会是 0.96/1.00-1 = -4.0,被分红错误拉低
        self.assertNotEqual(p["max"], -4.0)


if __name__ == "__main__":
    unittest.main()
