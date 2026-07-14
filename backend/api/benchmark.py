# -*- coding: utf-8 -*-
"""PRD-06 基准对比 —— 同类百分位 + 排名(P0,数据源就绪);Alpha/Beta 待基准数据源(P2)。

pingzhongdata 已含 Data_rateInSimilarPersent / Data_rateInSimilarType,同类百分位
直接抓存 fund_profile。Alpha/Beta/跟踪误差/信息比率需基准指数净值(沪深300 等),
数据源合规边界待评估,占位 null。

GET /api/fund/{code}/peer-rank  → 返回同类百分位/排名/总数。
"""
from backend.api._router import Ctx  # noqa: F401
from backend.models.db import get_conn


def get_peer_rank(ctx):
    code = (ctx.params.get("code") or "").strip()
    if not code:
        return (400, {"error": "缺少基金代码"})
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT peer_percentile, peer_rank, peer_total FROM fund_profile "
            "WHERE fund_code=?", (code,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return (404, {"error": "无该基金基本面缓存"})
    return {
        "fund_code": code,
        "peer_percentile": row["peer_percentile"],
        "peer_rank": row["peer_rank"],
        "peer_total": row["peer_total"],
        "alpha": None,
        "beta": None,
        "tracking_error": None,
        "information_ratio": None,
        "note": "Alpha/Beta/跟踪误差待基准指数数据源(PRD-06 P2 评估型)",
    }


ROUTES = [
    ("GET", "/api/fund/{code}/peer-rank", get_peer_rank),
]
