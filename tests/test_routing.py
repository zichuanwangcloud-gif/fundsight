# -*- coding: utf-8 -*-
"""backend.api._router 扩展路由基础设施单元测试。"""
import unittest

from backend.api._router import Ctx, match, dispatch


class TestMatch(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(match("/api/market", "/api/market"), {})

    def test_param_capture(self):
        self.assertEqual(match("/api/fund/{code}", "/api/fund/020608"), {"code": "020608"})

    def test_no_match_diff_segments(self):
        self.assertIsNone(match("/api/market", "/api/market/x"))

    def test_no_match_diff_literal(self):
        self.assertIsNone(match("/api/foo", "/api/bar"))


class TestCtx(unittest.TestCase):
    def test_q_single(self):
        c = Ctx(query={"cat": ["指数型"], "page": ["2"]})
        self.assertEqual(c.q("cat"), "指数型")
        self.assertEqual(c.q("page"), "2")
        self.assertEqual(c.q("missing", "def"), "def")

    def test_user_id_carried(self):
        c = Ctx(user_id=7)
        self.assertEqual(c.user_id, 7)
        self.assertIsNone(Ctx().user_id)


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.routes = [
            ("GET", "/api/ping", lambda ctx: {"pong": True}),
            ("GET", "/api/fund/{code}", lambda ctx: {"code": ctx.params["code"], "uid": ctx.user_id}),
            ("POST", "/api/echo", lambda ctx: (201, {"got": ctx.body})),
        ]

    def test_get_hit(self):
        self.assertEqual(dispatch(self.routes, "GET", "/api/ping"), (200, {"pong": True}))

    def test_param_and_user_id(self):
        self.assertEqual(
            dispatch(self.routes, "GET", "/api/fund/020608", user_id=42),
            (200, {"code": "020608", "uid": 42}),
        )

    def test_tuple_return_preserves_code(self):
        self.assertEqual(
            dispatch(self.routes, "POST", "/api/echo", body={"a": 1}),
            (201, {"got": {"a": 1}}),
        )

    def test_method_mismatch_returns_none(self):
        self.assertIsNone(dispatch(self.routes, "POST", "/api/ping"))

    def test_unknown_path_returns_none(self):
        self.assertIsNone(dispatch(self.routes, "GET", "/api/nope"))


if __name__ == "__main__":
    unittest.main()
