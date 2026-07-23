# -*- coding: utf-8 -*-
"""MCP server —— Model Context Protocol，JSON-RPC 2.0 over Streamable HTTP。

纯标准库实现（无 mcp / anthropic 第三方包）。把 tools.py 里那份工具定义暴露给
任意 MCP 客户端（Claude Desktop / Cursor 等），与 Web 聊天窗进程内 tool-loop
共用同一套工具——定义只写一份。

传输：由 backend/api/ai.py 的 POST /mcp 承载（复用现有 http.server），本模块只负责
JSON-RPC 请求对象 → 响应对象的纯函数处理，便于单测、与 HTTP 层解耦。

实现的方法：initialize / notifications/initialized / tools/list / tools/call / ping。
"""
from backend.mcp import tools

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "fundsight-mcp", "version": "0.1.0"}

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _mcp_tools():
    """TOOLS(input_schema) → MCP tools/list 形态(inputSchema camelCase)。"""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": t["input_schema"],
        }
        for t in tools.TOOLS
    ]


def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _handle_one(msg):
    """处理单条 JSON-RPC 消息。通知(无 id)返回 None，否则返回响应对象。"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, INVALID_REQUEST, "无效的 JSON-RPC 2.0 请求")
    method = msg.get("method")
    req_id = msg.get("id")
    is_notification = "id" not in msg
    params = msg.get("params") or {}

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
        return None if is_notification else _ok(req_id, result)

    if method in ("notifications/initialized", "initialized"):
        return None  # 通知，无需响应

    if method == "ping":
        return None if is_notification else _ok(req_id, {})

    if method == "tools/list":
        return None if is_notification else _ok(req_id, {"tools": _mcp_tools()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in tools.TOOL_NAMES:
            return None if is_notification else _err(
                req_id, INVALID_PARAMS, f"未知工具: {name}")
        payload = tools.call_tool(name, arguments)
        is_error = isinstance(payload, dict) and "error" in payload
        result = {
            "content": [{"type": "text", "text": tools.call_tool_text(name, arguments)}],
            "isError": is_error,
        }
        return None if is_notification else _ok(req_id, result)

    if is_notification:
        return None
    return _err(req_id, METHOD_NOT_FOUND, f"未知方法: {method}")


def handle(payload):
    """处理一个 JSON-RPC 请求体（单对象或批量数组）。

    返回：响应对象 / 响应数组 / None（纯通知无响应，HTTP 层回 202 空体）。
    """
    if isinstance(payload, list):
        if not payload:
            return _err(None, INVALID_REQUEST, "空批量请求")
        responses = [r for r in (_handle_one(m) for m in payload) if r is not None]
        return responses or None
    return _handle_one(payload)
