# -*- coding: utf-8 -*-
"""登录审计只读接口(M10B-B4)。

供登录用户查自己的登录记录(成功/失败),按 user_id 隔离。越权带
?user_id=他人 时返回 404,绝不泄露他人记录。

接口:
  GET /api/admin/login-audit              当前用户最近登录记录(?limit 覆盖,上限 100)
"""
from backend.api._router import Ctx  # noqa: F401  (保持路由约定一致)
from backend.models.db import get_conn

LIST_LIMIT = 50
MAX_LIMIT = 100


def handle_list(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    # 越权防护:?user_id 指定他人 → 404。仅允许查自己。
    asked = ctx.q("user_id", "").strip()
    if asked and asked != str(ctx.user_id):
        return (404, {"error": "not found"})
    try:
        limit = int(ctx.q("limit", str(LIST_LIMIT)))
    except ValueError:
        limit = LIST_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,user_id,ip,ua,ok,created_at FROM login_audit "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (ctx.user_id, limit),
        ).fetchall()
        return {"records": [dict(r) for r in rows]}
    finally:
        conn.close()


ROUTES = [
    ("GET", "api/admin/login-audit", handle_list),
]
