# -*- coding: utf-8 -*-
"""AI 分析 + MCP 路由（线路：AI）。

端点：
  GET  /api/ai/status            —— AI 是否就绪（前端据此决定按钮置灰/降级提示）
  POST /api/ai/chat              —— 常规多轮对话 + tool-loop（登录态）
  POST /api/ai/analyze/{code}    —— 详情页「一键分析」（登录态）
  POST /mcp                      —— MCP server（JSON-RPC 2.0，token 门控，供外部 MCP 客户端）

合规与安全：
- chat/analyze 需登录，且沿用 app.py 的 60 次/分限流（按端点路径计）。
- /mcp 默认关闭：仅当设置环境变量 FUNDSIGHT_MCP_TOKEN 后开放，且请求须带匹配 token
  （?token=... 或已登录会话），避免把工具端点裸奔公网，守住「仅自用私享」红线。
- 返回体始终附 disclaimer，前端另有无条件免责页脚，双保险。
"""
import os

from backend.datasource import ai_engine
from backend.mcp import server as mcp_server
from backend.mcp import tools as mcp_tools
from backend.models.db import get_conn


def get_status(ctx):
    return ai_engine.status()


def _messages_from_body(body):
    """兼容两种入参：{"messages": [{role,content}...]} 或 {"message": "..."}。"""
    if isinstance(body.get("messages"), list):
        out = []
        for m in body["messages"]:
            if not isinstance(m, dict):
                continue
            role = "assistant" if m.get("role") == "assistant" else "user"
            content = str(m.get("content", "")).strip()
            if content:
                out.append({"role": role, "content": content})
        return out[-20:]  # 只保留最近 20 轮，控制上下文与成本
    msg = str(body.get("message", "")).strip()
    return [{"role": "user", "content": msg}] if msg else []


def post_chat(ctx):
    if ctx.user_id is None:
        return (401, {"error": "请先登录"})
    messages = _messages_from_body(ctx.body or {})
    if not messages:
        return (400, {"error": "缺少对话内容"})
    result = ai_engine.run_chat(messages)
    return (200, result) if result.get("ok") else (200, result)


def post_analyze(ctx):
    if ctx.user_id is None:
        return (401, {"error": "请先登录"})
    code = (ctx.params.get("code") or "").strip()
    if not code:
        return (400, {"error": "缺少基金代码"})
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT name FROM fund_list WHERE fund_code=?", (code,)
        ).fetchone()
        name = row["name"] if row else None
    finally:
        conn.close()
    return (200, ai_engine.analyze_fund(code, name))


# --------------------------------------------------------------------------- #
# MCP over Streamable HTTP
# --------------------------------------------------------------------------- #
def _mcp_authorized(ctx):
    token = os.environ.get("FUNDSIGHT_MCP_TOKEN", "")
    if not token:
        return False  # 未配置 token → 默认关闭 /mcp
    if ctx.user_id is not None:
        return True   # 已登录的同源会话放行
    return ctx.q("token") == token


def post_mcp(ctx):
    if not _mcp_authorized(ctx):
        return (403, {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32001, "message": "MCP 未开放或 token 无效"},
        })
    payload = ctx.body
    if payload in (None, {}, ""):
        return (400, {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": "空请求体"},
        })
    response = mcp_server.handle(payload)
    if response is None:
        return (202, {})   # 纯通知：无响应体
    return (200, response)


ROUTES = [
    ("GET", "/api/ai/status", get_status),
    ("POST", "/api/ai/chat", post_chat),
    ("POST", "/api/ai/analyze/{code}", post_analyze),
    ("POST", "/mcp", post_mcp),
]

# 供外部引用：工具清单（便于文档/调试）
TOOL_LIST = [t["name"] for t in mcp_tools.TOOLS]
