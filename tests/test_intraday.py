# -*- coding: utf-8 -*-
"""backend.api.intraday 单元测试 —— 今日盘中实时涨幅时序 API。

覆盖:
  - get_intraday(): 只读今日 fund_quote_tick(升序)+ fund_quote 最新快照 +
    market_open 状态;空数据降级 ticks=[]
  - 路由注册到 ROUTES
  - 缺 code 返回 400
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from backend.api import intraday
from backend.api._router import Ctx
from backend.models import db as db_mod

_TODAY = date.today().isoformat()


class TestGetIntraday(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _seed_today_ticks(self, code="020608"):
        conn = sqlite3.connect(self.path)
        conn.executemany(
            "INSERT INTO fund_quote_tick"
            "(fund_code,quote_date,quote_time,gsz,gszzl,dwjz,gztime) VALUES (?,?,?,?,?,?,?)",
            [
                (code, _TODAY, "09:31:00", 1.001, 0.10, 1.000, "t1"),
                (code, _TODAY, "10:00:00", 1.015, 1.50, 1.000, "t2"),
                (code, _TODAY, "14:30:00", 0.995, -0.50, 1.000, "t3"),
            ],
        )
        conn.execute(
            "INSERT INTO fund_quote(fund_code,name,dwjz,gsz,gszzl,gztime,updated_at) "
            "VALUES (?,?,?,?,?,?,datetime('now','localtime'))",
            (code, "测试基金", 1.000, 0.995, -0.50, "2026-07-13 14:30"),
        )
        conn.commit()
        conn.close()

    def test_returns_today_ticks_ascending(self):
        self._seed_today_ticks()
        ctx = Ctx(params={"code": "020608"})
        with patch("backend.api.intraday.is_market_open", return_value=False):
            result = intraday.get_intraday(ctx)
        self.assertEqual(result["code"], "020608")
        self.assertEqual(result["date"], _TODAY)
        self.assertFalse(result["market_open"])
        ticks = result["ticks"]
        self.assertEqual(len(ticks), 3)
        # quote_time 升序
        self.assertEqual(ticks[0]["quote_time"], "09:31:00")
        self.assertEqual(ticks[-1]["quote_time"], "14:30:00")
        self.assertEqual(ticks[1]["gszzl"], 1.50)

    def test_latest_from_fund_quote_snapshot(self):
        self._seed_today_ticks()
        ctx = Ctx(params={"code": "020608"})
        with patch("backend.api.intraday.is_market_open", return_value=True):
            result = intraday.get_intraday(ctx)
        latest = result["latest"]
        self.assertIsNotNone(latest)
        self.assertEqual(latest["gszzl"], -0.50)
        self.assertEqual(latest["gsz"], 0.995)

    def test_market_open_field_present(self):
        ctx = Ctx(params={"code": "020608"})
        with patch("backend.api.intraday.is_market_open", return_value=True):
            result = intraday.get_intraday(ctx)
        self.assertTrue(result["market_open"])

    def test_empty_data_degrades_gracefully(self):
        # 今日无 tick + 无 fund_quote 行
        ctx = Ctx(params={"code": "020608"})
        with patch("backend.api.intraday.is_market_open", return_value=False):
            result = intraday.get_intraday(ctx)
        self.assertEqual(result["ticks"], [])
        self.assertIsNone(result["latest"])

    def test_missing_code_returns_400(self):
        ctx = Ctx(params={})
        code, obj = intraday.get_intraday(ctx)
        self.assertEqual(code, 400)
        self.assertEqual(obj, {"error": "缺少基金代码"})

    def test_registered_in_routes(self):
        self.assertEqual(len(intraday.ROUTES), 1)
        method, pattern, handler = intraday.ROUTES[0]
        self.assertEqual(method, "GET")
        self.assertEqual(pattern, "/api/fund/{code}/intraday")
        self.assertIs(handler, intraday.get_intraday)


if __name__ == "__main__":
    unittest.main()
