# -*- coding: utf-8 -*-
"""backend.datasource.fundgz 单元测试。

不发起真实网络请求：用离线样本 JSONP 报文 mock urllib.request.urlopen。
"""
import sqlite3
import unittest
from datetime import datetime, time
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
            CREATE TABLE fund_quote_tick (
                fund_code   TEXT NOT NULL,
                quote_date  TEXT NOT NULL,
                quote_time  TEXT NOT NULL,
                gsz         REAL,
                gszzl       REAL,
                dwjz        REAL,
                gztime      TEXT,
                PRIMARY KEY (fund_code, quote_date, quote_time)
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
    def test_refresh_quotes_writes_tick_timeseries(self, mock_fetch):
        # refresh_quotes 除写 fund_quote 快照外,应追加 fund_quote_tick 时序点
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
        tick = self.conn.execute(
            "SELECT * FROM fund_quote_tick WHERE fund_code=?", ("020608",)
        ).fetchone()
        self.assertIsNotNone(tick)
        self.assertEqual(tick["gszzl"], 1.26)
        self.assertTrue(tick["quote_date"])  # YYYY-MM-DD
        self.assertTrue(tick["quote_time"])  # HH:MM:SS

    @patch("backend.datasource.fundgz.fetch_estimate")
    def test_refresh_quotes_tick_dedup_same_second(self, mock_fetch):
        # 同一秒内重复采样应 INSERT OR IGNORE 去重,不产生重复行
        mock_fetch.return_value = {
            "fund_code": "020608", "name": "X", "dwjz": 1.0,
            "gsz": 1.01, "gszzl": 1.0, "gztime": "t",
        }
        fundgz.refresh_quotes(self.conn, ["020608"])
        fundgz.refresh_quotes(self.conn, ["020608"])
        n = self.conn.execute(
            "SELECT COUNT(*) FROM fund_quote_tick WHERE fund_code=?", ("020608",)
        ).fetchone()[0]
        self.assertEqual(n, 1)

    @patch("backend.datasource.fundgz.fetch_estimate")
    def test_refresh_quotes_skips_failed_fetches(self, mock_fetch):
        mock_fetch.return_value = None

        ok = fundgz.refresh_quotes(self.conn, ["999999"])

        self.assertEqual(ok, 0)
        row = self.conn.execute(
            "SELECT * FROM fund_quote WHERE fund_code=?", ("999999",)
        ).fetchone()
        self.assertIsNone(row)


class TestIsMarketOpen(unittest.TestCase):
    """交易时段判断:A 股工作日 09:30–15:00,其余均闭市。"""

    def test_weekday_morning_before_open(self):
        # 周一 09:00 未开盘
        dt = datetime(2026, 7, 13, 9, 0)  # 2026-07-13 是周一
        self.assertFalse(fundgz.is_market_open(dt))

    def test_weekday_trading_hours(self):
        dt = datetime(2026, 7, 13, 10, 30)
        self.assertTrue(fundgz.is_market_open(dt))

    def test_weekday_after_close(self):
        dt = datetime(2026, 7, 13, 15, 1)
        self.assertFalse(fundgz.is_market_open(dt))

    def test_saturday_closed(self):
        dt = datetime(2026, 7, 18, 10, 30)  # 周六
        self.assertFalse(fundgz.is_market_open(dt))

    def test_sunday_closed(self):
        dt = datetime(2026, 7, 19, 14, 0)  # 周日
        self.assertFalse(fundgz.is_market_open(dt))

    def test_open_at_0930_boundary(self):
        dt = datetime(2026, 7, 13, 9, 30)
        self.assertTrue(fundgz.is_market_open(dt))

    def test_close_at_1500_boundary(self):
        dt = datetime(2026, 7, 13, 15, 0)
        self.assertTrue(fundgz.is_market_open(dt))


if __name__ == "__main__":
    unittest.main()
