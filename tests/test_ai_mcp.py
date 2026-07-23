# -*- coding: utf-8 -*-
"""AI 分析 + MCP 单元测试。

覆盖：
  - backend.mcp.tools: 4 个工具读本地缓存的正确性 + 未知工具/缺参兜底
  - backend.mcp.server: JSON-RPC initialize / tools/list / tools/call / 通知 / 未知方法
  - backend.datasource.ai_engine: 无 key 降级 + mock urlopen 的 anthropic tool-loop
  - backend.api.ai: status / chat 需登录 / analyze / /mcp token 门控

全程离线：不发真实网络请求，DB 用临时文件，工具只读已 seed 的缓存表（不触发抓取兜底）。
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from backend.api import ai as ai_api
from backend.api._router import Ctx
from backend.datasource import ai_engine
from backend.mcp import server as mcp_server
from backend.mcp import tools as mcp_tools
from backend.models import db as db_mod

CODE = "020608"


def _seed(conn):
    conn.execute(
        "INSERT INTO fund_list(fund_code,name,pinyin,fund_type,synced_at) "
        "VALUES (?,?,?,?,datetime('now'))",
        (CODE, "南方中证机器人ETF发起联接C", "x", "指数"),
    )
    conn.execute(
        "INSERT INTO fund_profile(fund_code,name,manager,scale,rate,"
        "syl_1n,syl_3y,syl_6y,syl_1y,peer_percentile,peer_rank,peer_total,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        (CODE, "南方中证机器人ETF", "张三", 12.3, "0.15%", 31.8, 15.6, 40.1, 2.1, 88.5, 120, 1000),
    )
    conn.execute(
        "INSERT INTO fund_quote(fund_code,name,dwjz,gsz,gszzl,gztime,nav,nav_date,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
        (CODE, "南方中证机器人ETF", 1.20, 1.23, 2.5, "2026-07-23 15:00", 1.20, "2026-07-22"),
    )
    navs = [
        ("2026-04-24", 1.00), ("2026-05-24", 1.05),
        ("2026-06-24", 0.98), ("2026-07-22", 1.20),
    ]
    for d, v in navs:
        conn.execute(
            "INSERT INTO fund_nav_history(fund_code,nav_date,nav,equity_return,nav_adj) "
            "VALUES (?,?,?,?,?)", (CODE, d, v, 0.0, v))
    holds = [
        (1, "300750", "宁德时代", 9.8, "2026年2季度"),
        (2, "002594", "比亚迪", 7.2, "2026年2季度"),
    ]
    for rank, sc, sn, w, rp in holds:
        conn.execute(
            "INSERT INTO fund_holding_stock(fund_code,rank,stock_code,stock_name,weight,"
            "report_period,updated_at) VALUES (?,?,?,?,?,?,datetime('now'))",
            (CODE, rank, sc, sn, w, rp))
    cmp_rows = [
        ("self", "2026-07-22", 20.0), ("peer", "2026-07-22", 12.0),
        ("hs300", "2026-07-22", 8.0),
    ]
    for k, d, v in cmp_rows:
        conn.execute(
            "INSERT INTO fund_compare_trend(fund_code,series_key,trade_date,value,updated_at) "
            "VALUES (?,?,?,?,datetime('now'))", (CODE, k, d, v))
    conn.commit()


class _DbCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)
        conn = db_mod.get_conn()
        _seed(conn)
        conn.close()

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)


class TestTools(_DbCase):
    def test_overview(self):
        out = mcp_tools.call_tool("get_fund_overview", {"code": CODE})
        self.assertEqual(out["code"], CODE)
        self.assertEqual(out["intraday"]["gszzl_pct"], 2.5)
        self.assertEqual(out["profile"]["manager"], "张三")
        self.assertEqual(out["profile"]["peer_percentile"], 88.5)
        self.assertIn("periods", out)
        # 近30日：从 1.05(约30天前无点，取 <= target 最近) 到 1.20；覆盖率算出非空
        self.assertIsNotNone(out["periods"]["r_since"])

    def test_holdings(self):
        out = mcp_tools.call_tool("get_fund_holdings", {"code": CODE})
        self.assertEqual(len(out["holdings"]), 2)
        self.assertEqual(out["holdings"][0]["stock_name"], "宁德时代")
        self.assertEqual(out["report_period"], "2026年2季度")

    def test_nav_trend(self):
        out = mcp_tools.call_tool("get_fund_nav_trend", {"code": CODE, "days": 365})
        self.assertEqual(out["start"]["nav"], 1.00)
        self.assertEqual(out["end"]["nav"], 1.20)
        self.assertEqual(out["cumulative_return_pct"], 20.0)
        self.assertLess(out["max_drawdown_pct"], 0)   # 1.05→0.98 有回撤

    def test_peer_compare(self):
        out = mcp_tools.call_tool("get_fund_peer_compare", {"code": CODE})
        self.assertEqual(out["self_vs_peer_pct"], 8.0)
        self.assertEqual(out["self_vs_hs300_pct"], 12.0)

    def test_unknown_tool_and_missing_code(self):
        self.assertIn("error", mcp_tools.call_tool("nope", {"code": CODE}))
        self.assertIn("error", mcp_tools.call_tool("get_fund_overview", {}))

    def test_call_tool_text_is_json(self):
        s = mcp_tools.call_tool_text("get_fund_overview", {"code": CODE})
        self.assertEqual(json.loads(s)["code"], CODE)


class TestMcpServer(_DbCase):
    def test_initialize(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "fundsight-mcp")
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_tools_list_shape(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, mcp_tools.TOOL_NAMES)
        self.assertIn("inputSchema", resp["result"]["tools"][0])  # MCP camelCase

    def test_tools_call(self):
        resp = mcp_server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_fund_overview", "arguments": {"code": CODE}},
        })
        self.assertFalse(resp["result"]["isError"])
        text = resp["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text)["code"], CODE)

    def test_notification_returns_none(self):
        self.assertIsNone(mcp_server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "bogus"})
        self.assertEqual(resp["error"]["code"], mcp_server.METHOD_NOT_FOUND)


def _anthropic_resp(text):
    m = MagicMock()
    m.read.return_value = text.encode("utf-8")
    m.__enter__ = lambda s: s
    m.__exit__ = lambda s, *a: False
    return m


class TestEngine(_DbCase):
    def test_not_configured(self):
        with patch.dict(os.environ, {"FUNDSIGHT_AI_API_KEY": "", "ANTHROPIC_API_KEY": ""}, clear=False):
            self.assertFalse(ai_engine.is_configured())
            r = ai_engine.run_chat([{"role": "user", "content": "hi"}])
            self.assertFalse(r["ok"])

    def test_tool_loop(self):
        # 第一轮：模型请求调 get_fund_overview；第二轮：出最终文本
        round1 = json.dumps({
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "让我查一下"},
                {"type": "tool_use", "id": "tu_1", "name": "get_fund_overview",
                 "input": {"code": CODE}},
            ],
        })
        round2 = json.dumps({
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "近况小结：表现不错。" + ai_engine.DISCLAIMER}],
        })
        with patch.dict(os.environ, {"FUNDSIGHT_AI_API_KEY": "k", "FUNDSIGHT_AI_PROVIDER": "anthropic"}):
            with patch("backend.datasource.ai_engine.urllib.request.urlopen",
                       side_effect=[_anthropic_resp(round1), _anthropic_resp(round2)]):
                r = ai_engine.run_chat([{"role": "user", "content": f"分析 {CODE}"}])
        self.assertTrue(r["ok"])
        self.assertIn("get_fund_overview", r["tool_calls"])
        self.assertIn("小结", r["reply"])
        self.assertEqual(r["disclaimer"], ai_engine.DISCLAIMER)


class TestApiRoutes(_DbCase):
    def test_status(self):
        code, obj = 200, ai_api.get_status(Ctx())
        self.assertIn("configured", obj)
        self.assertIn("provider", obj)

    def test_chat_requires_login(self):
        code, obj = ai_api.post_chat(Ctx(body={"message": "hi"}, user_id=None))
        self.assertEqual(code, 401)

    def test_analyze_missing_code(self):
        code, obj = ai_api.post_analyze(Ctx(params={}, user_id=1))
        self.assertEqual(code, 400)

    def test_mcp_disabled_without_token(self):
        with patch.dict(os.environ, {"FUNDSIGHT_MCP_TOKEN": ""}, clear=False):
            os.environ.pop("FUNDSIGHT_MCP_TOKEN", None)
            code, obj = ai_api.post_mcp(Ctx(
                body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, user_id=None))
        self.assertEqual(code, 403)

    def test_mcp_with_token(self):
        with patch.dict(os.environ, {"FUNDSIGHT_MCP_TOKEN": "secret"}):
            ctx = Ctx(query={"token": ["secret"]},
                      body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, user_id=None)
            code, obj = ai_api.post_mcp(ctx)
        self.assertEqual(code, 200)
        self.assertEqual({t["name"] for t in obj["result"]["tools"]}, mcp_tools.TOOL_NAMES)

    def test_mcp_notification_202(self):
        with patch.dict(os.environ, {"FUNDSIGHT_MCP_TOKEN": "secret"}):
            ctx = Ctx(query={"token": ["secret"]},
                      body={"jsonrpc": "2.0", "method": "notifications/initialized"}, user_id=None)
            code, obj = ai_api.post_mcp(ctx)
        self.assertEqual(code, 202)


if __name__ == "__main__":
    unittest.main()
