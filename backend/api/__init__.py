# -*- coding: utf-8 -*-
"""后续线路的 API 路由扩展点。

核心端点(搜索/持仓/鉴权)由 app.py 直接处理。市场/详情/交易流水等新模块
在各自文件导出 ROUTES = [(method, pattern, handler), ...],在下方锚点追加一行
汇总到 ALL_ROUTES;app.py 会在核心分发之外尝试匹配它们。各线路只加自己一行,
互不冲突。
"""
ALL_ROUTES = []

# === 并行线路注册锚点(各线路只在此追加自己一行) ===
# 线路 A 市场:    from backend.api import market;       ALL_ROUTES += market.ROUTES
# 线路 B 详情:    from backend.api import fund_detail;  ALL_ROUTES += fund_detail.ROUTES
# 线路 C 交易流水: from backend.api import transactions; ALL_ROUTES += transactions.ROUTES
from backend.api import market; ALL_ROUTES += market.ROUTES  # noqa: E401,E402
from backend.api import fund_detail; ALL_ROUTES += fund_detail.ROUTES
from backend.api import intraday; ALL_ROUTES += intraday.ROUTES
from backend.api import transactions; ALL_ROUTES += transactions.ROUTES
from backend.api import holdings_ext; ALL_ROUTES += holdings_ext.ROUTES
from backend.api import sync_status; ALL_ROUTES += sync_status.ROUTES
from backend.api import notifications; ALL_ROUTES += notifications.ROUTES
from backend.api import returns; ALL_ROUTES += returns.ROUTES
# M10B 线路 B 鉴权加固:登录审计只读接口(按 user_id 隔离,越权 404)
from backend.api import login_audit; ALL_ROUTES += login_audit.ROUTES  # noqa: E401,E402
