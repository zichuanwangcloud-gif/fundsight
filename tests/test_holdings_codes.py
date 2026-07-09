# -*- coding: utf-8 -*-
"""GET /api/holdings/codes —— 当前登录用户自选/持仓代码集,供市场页打"已自选"标。

TDD:先写用例再实现 backend/api/holdings_ext.py。

策略:
- 未登录(ctx.user_id is None)→ 401 {"error": "unauthorized"},与 app.py 现有
  _require_auth 风格一致,不额外发明协议。
- 已登录 → {"codes": [...]},按 user_id 隔离,fund_code 去重(不保证顺序,
  用例用 set 比较)。
- 通过 ALL_ROUTES 注册验证(backend/api/__init__.py 汇总),同时直接单测 handler。
"""
import os
import sqlite3
import tempfile
import unittest

from backend.api._router import Ctx, dispatch
from backend.api import ALL_ROUTES
from backend.models import db as db_mod


class TestHoldingsCodes(unittest.TestCase):
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

    def _add_holding(self, user_id, code):
        conn = self._conn()
        conn.execute(
            "INSERT INTO holding(user_id,fund_code,created_at) VALUES (?,?,datetime('now'))",
            (user_id, code),
        )
        conn.commit()
        conn.close()

    def test_unauthenticated_returns_401(self):
        result = dispatch(ALL_ROUTES, "GET", "/api/holdings/codes", user_id=None)
        self.assertEqual(result, (401, {"error": "unauthorized"}))

    def test_returns_current_user_codes(self):
        self._add_holding(1, "020608")
        self._add_holding(1, "005827")
        code, obj = dispatch(ALL_ROUTES, "GET", "/api/holdings/codes", user_id=1)
        self.assertEqual(code, 200)
        self.assertEqual(set(obj["codes"]), {"020608", "005827"})

    def test_isolated_by_user_id(self):
        self._add_holding(1, "020608")
        self._add_holding(2, "161725")
        code, obj = dispatch(ALL_ROUTES, "GET", "/api/holdings/codes", user_id=1)
        self.assertEqual(code, 200)
        self.assertEqual(obj["codes"], ["020608"])
        code2, obj2 = dispatch(ALL_ROUTES, "GET", "/api/holdings/codes", user_id=2)
        self.assertEqual(obj2["codes"], ["161725"])

    def test_dedupes_codes(self):
        # 同一用户同一基金既在自选也重复录入持仓,codes 去重
        self._add_holding(1, "020608")
        self._add_holding(1, "020608")
        code, obj = dispatch(ALL_ROUTES, "GET", "/api/holdings/codes", user_id=1)
        self.assertEqual(obj["codes"], ["020608"])

    def test_no_holdings_returns_empty_list(self):
        code, obj = dispatch(ALL_ROUTES, "GET", "/api/holdings/codes", user_id=1)
        self.assertEqual(code, 200)
        self.assertEqual(obj["codes"], [])

    def test_registered_in_all_routes(self):
        # 守注册纪律:确保通过 __init__.py 锚点追加行汇总进 ALL_ROUTES
        patterns = [(m, p) for m, p, _ in ALL_ROUTES]
        self.assertIn(("GET", "/api/holdings/codes"), patterns)


if __name__ == "__main__":
    unittest.main()
