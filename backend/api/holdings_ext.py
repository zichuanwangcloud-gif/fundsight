# -*- coding: utf-8 -*-
"""线路 D —— 市场↔自选联动扩展端点。

  GET /api/holdings/codes   当前登录用户自选/持仓 fund_code 去重集合
                            → {"codes": [...]}，供市场页/详情页打"已自选"标。

零依赖:仅标准库 + backend.models.db.get_conn,直接查 holding 表(只读)。
鉴权:未登录(ctx.user_id is None)返回 401,风格与 app.py 现有端点一致
(app.py 的 _require_auth 也是 401 {"error": "unauthorized"})。
"""
from backend.models.db import get_conn


def get_holding_codes(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT fund_code FROM holding WHERE user_id=? ORDER BY fund_code",
        (ctx.user_id,),
    ).fetchall()
    conn.close()
    return {"codes": [r["fund_code"] for r in rows]}


ROUTES = [
    ("GET", "/api/holdings/codes", get_holding_codes),
]
