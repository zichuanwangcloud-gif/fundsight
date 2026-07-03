# -*- coding: utf-8 -*-
"""backend.datasource.fundgz 单元测试。

不发起真实网络请求：用离线样本 JSONP 报文 mock urllib.request.urlopen。
"""
import sqlite3
import unittest
from unittest.mock import patch, MagicMock

from backend.datasource import fundgz

# 离线样本报文，格式与天天基金 fundgz 接口一致
SAMPLE_JSONP = (
    'jsonpgz({"fundcode":"020608","name":"南方中证机器人ETF发起联接C",'
    '"jzrq":"2026-07-02","dwjz":"1.2345","gsz":"1.2500","gszzl":"1.26",'
    '"gztime":"2026-07-03 15:00"});'
)


def _mock_response(text):
    resp = MagicMock()
    resp.read.return_value = text.encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


class TestF(unittest.TestCase):
    """测试 _f() 数字转换的边界情况。"""

    def test_f_normal_number_string(self):
        self.assertEqual(fundgz._f("1.2345"), 1.2345)

    def test_f_normal_int_string(self):
        self.assertEqual(fundgz._f("5"), 5.0)

    def test_f_none_returns_none(self):
        self.assertIsNone(fundgz._f(None))

    def test_f_empty_string_returns_none(self):
        self.assertIsNone(fundgz._f(""))

    def test_f_non_numeric_string_returns_none(self):
        self.assertIsNone(fundgz._f("abc"))

    def test_f_already_float(self):
        self.assertEqual(fundgz._f(3.14), 3.14)


class TestFetchEstimate(unittest.TestCase):
    """用离线样本报文 mock jsonpgz(...) 响应，验证解析逻辑，不打真实网络。"""

    @patch("backend.datasource.fundgz.urllib.request.urlopen")
    def test_fetch_estimate_parses_sample_jsonp(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(SAMPLE_JSONP)

        result = fundgz.fetch_estimate("020608")

        self.assertIsNotNone(result)
        self.assertEqual(result["fund_code"], "020608")
        self.assertEqual(result["name"], "南方中证机器人ETF发起联接C")
        self.assertEqual(result["dwjz"], 1.2345)
        self.assertEqual(result["gsz"], 1.2500)
        self.assertEqual(result["gszzl"], 1.26)
        self.assertEqual(result["gztime"], "2026-07-03 15:00")

    @patch("backend.datasource.fundgz.urllib.request.urlopen")
    def test_fetch_estimate_returns_none_when_pattern_not_found(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response("not a jsonp payload at all")

        result = fundgz.fetch_estimate("999999")

        self.assertIsNone(result)

    @patch("backend.datasource.fundgz.urllib.request.urlopen")
    def test_fetch_estimate_returns_none_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("network unreachable")

        result = fundgz.fetch_estimate("020608")

        self.assertIsNone(result)

    @patch("backend.datasource.fundgz.urllib.request.urlopen")
    def test_fetch_estimate_handles_malformed_numeric_fields(self, mock_urlopen):
        malformed = (
            'jsonpgz({"fundcode":"020608","name":"测试基金",'
            '"jzrq":"2026-07-02","dwjz":"","gsz":null,"gszzl":"abc",'
            '"gztime":"2026-07-03 15:00"});'
        )
        mock_urlopen.return_value = _mock_response(malformed)

        result = fundgz.fetch_estimate("020608")

        self.assertIsNotNone(result)
        self.assertIsNone(result["dwjz"])
        self.assertIsNone(result["gsz"])
        self.assertIsNone(result["gszzl"])


class TestRefreshQuotes(unittest.TestCase):
    """refresh_quotes 写入内存 SQLite，不涉及网络（mock fetch_estimate）。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE fund_quote (
                fund_code   TEXT PRIMARY KEY,
                name        TEXT,
                dwjz        REAL,
                gsz         REAL,
                gszzl       REAL,
                gztime      TEXT,
                nav_date    TEXT,
                updated_at  TEXT
            );
            """
        )

    def tearDown(self):
        self.conn.close()

    @patch("backend.datasource.fundgz.fetch_estimate")
    def test_refresh_quotes_writes_successful_fetches(self, mock_fetch):
        mock_fetch.return_value = {
            "fund_code": "020608",
            "name": "南方中证机器人ETF发起联接C",
            "dwjz": 1.2345,
            "gsz": 1.25,
            "gszzl": 1.26,
            "gztime": "2026-07-03 15:00",
        }

        ok = fundgz.refresh_quotes(self.conn, ["020608"])

        self.assertEqual(ok, 1)
        row = self.conn.execute(
            "SELECT * FROM fund_quote WHERE fund_code=?", ("020608",)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["dwjz"], 1.2345)

    @patch("backend.datasource.fundgz.fetch_estimate")
    def test_refresh_quotes_skips_failed_fetches(self, mock_fetch):
        mock_fetch.return_value = None

        ok = fundgz.refresh_quotes(self.conn, ["999999"])

        self.assertEqual(ok, 0)
        row = self.conn.execute(
            "SELECT * FROM fund_quote WHERE fund_code=?", ("999999",)
        ).fetchone()
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
