# 盈见 FundSight —— AI 分析 + MCP 设计（P1）

> 状态：P1 已实现（本文件随首个 PR 落地）。P2（个股/赛道实时抓取）见文末。

## 1. 目标

在现有基金看板上集成 AI 能力：
- 一个**全局悬浮聊天窗**，可常规对话、可对某只基金做分析；
- 基金详情页一个**「🤖 AI 分析」按钮**，一键出「近况 + 展望」分析卡；
- AI 能读基金**近期涨幅、重仓股、同类/大盘对比**等关键数据，汇总判断近况与预期；
- 所有分析**强制附「仅供参考」免责**。

技术上采用 **MCP（Model Context Protocol）**：fundsight 自身作为 MCP server，把工具标准化，
既供 Web 聊天窗（进程内直调）使用，也供外部 MCP 客户端（Claude Desktop / Cursor 等）复用。

## 2. 架构：一套工具，两个消费口，零依赖

```
  Web 聊天窗 / 一键分析 ──►  后端 Messages API tool-loop (urllib，纯标准库)
                              │ 进程内直调工具（不经 JSON-RPC 到自己）
   外部 MCP 客户端      ──►  POST /mcp  JSON-RPC 2.0 over Streamable HTTP
                              │
                              ▼
                    backend/mcp/tools.py  —— 工具唯一真源，只读 SQLite 缓存
                              │ 缓存全缺时首访低频抓一次
                              ▼
                    backend/datasource/   —— 唯一对外接口（低频抓取 + 缓存）
```

**为什么工具进程内直调、而非让 Web 后端 JSON-RPC 调自己的 MCP server**：那等于序列化
JSON-RPC 跟自己说话，白加延迟与复杂度。MCP 的价值在跨进程/跨厂商边界，故 MCP server
定位为「对外复用出口」，Web 聊天窗直接调同一批函数。工具定义（`TOOLS`）只写一份。

## 3. 组件与落点

| 文件 | 职责 |
|---|---|
| `backend/mcp/tools.py` | 工具定义（JSON-Schema）+ 执行，**唯一真源**；只读 SQLite，首访兜底抓一次 |
| `backend/mcp/server.py` | 纯标准库 MCP server：`initialize`/`tools/list`/`tools/call`/通知/`ping` |
| `backend/datasource/ai_engine.py` | provider 抽象 + 手搓 Messages API tool-loop（anthropic / openai 兼容） |
| `backend/api/ai.py` | 路由：`/api/ai/status`、`/api/ai/chat`、`/api/ai/analyze/{code}`、`/mcp` |
| `frontend/ai.js` `ai.css` | 悬浮聊天窗 + 免责页脚 + 无 key 降级 |
| `frontend/detail.js` | 详情页「🤖 AI 分析」按钮 |

## 4. 工具集（P1）

全部只读本地缓存表，零新增抓取：

| 工具 | 来源表 |
|---|---|
| `get_fund_overview(code)` | `fund_profile` + `fund_quote` + 近1/3月/今年/成立累计涨幅 |
| `get_fund_holdings(code)` | `fund_holding_stock`（Top10 重仓股 + 权重 + 报告期） |
| `get_fund_nav_trend(code,days)` | `fund_nav_history`（采样点 + 区间累计涨幅 + 最大回撤） |
| `get_fund_peer_compare(code)` | `fund_compare_trend`（本基金 vs 同类平均 vs 沪深300） |

## 5. 模型接入（provider 可切换，默认不定死）

沿用 `vision_ocr` 的抽象，纯 `urllib` 调用，无 SDK：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `FUNDSIGHT_AI_API_KEY` | 回退 `ANTHROPIC_API_KEY` | 密钥；未设则 AI 优雅降级 |
| `FUNDSIGHT_AI_PROVIDER` | `anthropic` | `anthropic` \| `openai`（含国产 OpenAI 兼容端点） |
| `FUNDSIGHT_AI_ENDPOINT` | 按 provider | 自定义端点 |
| `FUNDSIGHT_AI_MODEL` | `claude-sonnet-5` / `gpt-4o-mini` | 模型名 |

tool-loop 硬上限 **6 轮**，钉死外部请求次数与 token 成本；叠加已有 60 次/分限流。

## 6. 合规红线与安全

- **抓取收敛**：工具只读 SQLite，绝不在工具内直连外网；首访缓存全缺时才低频抓一次
  （复用 `datasource/` 现有 refresh），与 `fund_detail._ensure_cached` 同款。
- **`/mcp` 默认关闭**：仅当设置 `FUNDSIGHT_MCP_TOKEN` 才开放，且请求须带匹配 token
  （`?token=` 或已登录会话），不把工具端点裸奔公网，守住「仅自用私享」。生产建议再绑回环/内网。
- **无 key 降级**：无密钥时聊天窗按钮置灰提示，主功能不受影响。

## 7. 「仅供参考」双保险

1. **system prompt 硬约束**：结尾必须输出固定免责声明；「未来展望」只作情景/风险提示，
   禁确定性买卖点与收益承诺。
2. **前端无条件页脚**：不依赖模型自觉，聊天面板始终渲染
   `本分析由 AI 基于公开数据自动生成，仅供参考，不构成投资建议；市场有风险，投资需谨慎。`

## 8. 测试

`tests/test_ai_mcp.py`（19 项，全离线）：工具读缓存正确性 + 缺参/未知工具兜底、
MCP server 各方法、tool-loop（mock urlopen 模拟 anthropic 两轮）、路由鉴权与 token 门控。

## 9. P2（后续，合规敏感单独隔离评审）

- 新增 `backend/datasource/stock_quote.py`、`sector_info.py`：个股行情/赛道信息**真实抓取**，
  写入新表 `stock_quote`、`sector_info`，低频懒刷新（个股盘中 TTL ~10 分钟、赛道按天）。
- 新增工具 `get_stock_info`、`get_sector_info` 接入 tool-loop 与 MCP。
- 触及「抓取层是唯一对外接口 / 仅自用低频」红线，按 CLAUDE.md 需重新评估合规，单独 PR。
