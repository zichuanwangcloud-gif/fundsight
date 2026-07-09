# -*- coding: utf-8 -*-
"""backend.datasource.nav_history 单元测试。

用离线样本 pingzhongdata 报文 mock urlopen，验证历史序列解析与写库幂等，
不发起真实网络请求。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from backend.datasource import nav_history
from backend.models import db as db_mod

# 精简的 pingzhongdata 样本：含 Data_netWorthTrend 三个净值点
# x 为毫秒时间戳(2024-03-12 / 03-13 / 03-14 UTC),y 为单位净值
SAMPLE_JS = (
    'var fS_name = "测试基金";'
    'var Data_netWorthTrend = ['
    '{"x":1710201600000,"y":1.0000,"equityReturn":0},'
    '{"x":1710288000000,"y":1.0200,"equityReturn":2.0},'
    '{"x":1710374400000,"y":1.0100,"equityReturn":-0.98}'
    '];'
    'var Data_ACWorthTrend = [];'
)


def _mock_response(text):
    resp = MagicMock()
    resp.read.return_value = text.encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


class TestFetchNavHistory(unittest.TestCase):
    @patch("backend.datasource.nav_history.urllib.request.urlopen")
    def test_parses_full_series(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(SAMPLE_JS)
        series = nav_history.fetch_nav_history("020608")
        self.assertIsNotNone(series)
        self.assertEqual(len(series), 3)
        # 每项 (date_str, nav)
        self.assertEqual(series[0][1], 1.0000)
        self.assertEqual(series[-1][1], 1.0100)
        # 日期格式 YYYY-MM-DD
        self.assertRegex(series[0][0], r"^\d{4}-\d{2}-\d{2}$")

    @patch("backend.datasource.nav_history.urllib.request.urlopen")
    def test_returns_none_on_error(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("network unreachable")
        self.assertIsNone(nav_history.fetch_nav_history("020608"))

    @patch("backend.datasource.nav_history.urllib.request.urlopen")
    def test_returns_none_when_no_trend(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response('var fS_name = "x";')
        self.assertIsNone(nav_history.fetch_nav_history("020608"))


class TestRefreshNavHistory(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _count(self, code="020608"):
        conn = sqlite3.connect(self.path)
        n = conn.execute(
            "SELECT COUNT(*) FROM fund_nav_history WHERE fund_code=?", (code,)
        ).fetchone()[0]
        conn.close()
        return n

    @patch("backend.datasource.nav_history.fetch_nav_history")
    def test_writes_series(self, mock_fetch):
        mock_fetch.return_value = [("2024-03-12", 1.0), ("2024-03-13", 1.02)]
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        n = nav_history.refresh_nav_history(conn, ["020608"])
        conn.close()
        self.assertEqual(n, 1)
        self.assertEqual(self._count(), 2)

    @patch("backend.datasource.nav_history.fetch_nav_history")
    def test_idempotent_no_duplicate(self, mock_fetch):
        mock_fetch.return_value = [("2024-03-12", 1.0), ("2024-03-13", 1.02)]
        conn = sqlite3.connect(self.path)
        nav_history.refresh_nav_history(conn, ["020608"])
        nav_history.refresh_nav_history(conn, ["020608"])  # 重复写
        conn.close()
        self.assertEqual(self._count(), 2)  # 主键去重,不翻倍

    @patch("backend.datasource.nav_history.fetch_nav_history")
    def test_skip_when_fetch_none(self, mock_fetch):
        mock_fetch.return_value = None
        conn = sqlite3.connect(self.path)
        n = nav_history.refresh_nav_history(conn, ["020608"])
        conn.close()
        self.assertEqual(n, 0)
        self.assertEqual(self._count(), 0)


if __name__ == "__main__":
    unittest.main()
