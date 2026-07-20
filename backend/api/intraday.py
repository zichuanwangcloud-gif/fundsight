# -*- coding: utf-8 -*-
"""今日盘中实时涨幅时序 API —— 详情页「今日实时涨幅」折线用。

GET /api/fund/{code}/intraday
  → {"code","date","market_open","latest":{gszzl,gsz,dwjz,gztime,updated_at}|None,
     "ticks":[{"quote_time","gsz","gszzl"}, ...]}

业务层只读 fund_quote_tick(今日)+ fund_quote(最新快照),绝不触发抓取
(抓取由 scheduler quote_refresh 后台 + fund_detail._ensure_intraday_seed 按需兜底)。
公共行情数据,无需登录校验(与 /api/fund/{code} 一致),仍受 app.py 限流保护。
"""
from datetime import datetime

from backend.datasource.fundgz import is_market_open
from backend.models.db import get_conn


def get_intraday(ctx):
    code = (ctx.params.get("code") or "").strip()
    if not code:
        return (400, {"error": "缺少基金代码"})

    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        try:
            ticks = [
                {"quote_time": r["quote_time"], "gsz": r["gsz"], "gszzl": r["gszzl"]}
                for r in conn.execute(
                    "SELECT quote_time, gsz, gszzl FROM fund_quote_tick "
                    "WHERE fund_code=? AND quote_date=? ORDER BY quote_time",
                    (code, today),
                ).fetchall()
            ]
        except Exception:  # noqa: BLE001 —— 表缺失等,静默降级为空时序
            ticks = []
        latest = conn.execute(
            "SELECT gszzl, gsz, dwjz, gztime, updated_at FROM fund_quote WHERE fund_code=?",
            (code,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "code": code,
        "date": today,
        "market_open": is_market_open(),
        "latest": dict(latest) if latest else None,
        "ticks": ticks,
    }


ROUTES = [("GET", "/api/fund/{code}/intraday", get_intraday)]
