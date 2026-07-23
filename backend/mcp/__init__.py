# -*- coding: utf-8 -*-
"""AI 分析工具层 + MCP server（Model Context Protocol）。

- tools.py   基金/个股/赛道工具的【唯一真源】：定义 + 执行，只读 SQLite 缓存。
- server.py  纯标准库实现的 MCP server（JSON-RPC 2.0 over Streamable HTTP）。

Web 聊天窗后端在进程内直接调 tools.call_tool（不经 JSON-RPC 到自己）；
外部 MCP 客户端（Claude Desktop / Cursor 等）经 /mcp 走 JSON-RPC 调同一套工具。
"""
