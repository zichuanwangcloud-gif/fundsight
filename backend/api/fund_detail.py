# -*- coding: utf-8 -*-
"""基金详情页 API —— 净值走势/涨跌柱 + 基本面(线路 B / M8)。

GET /api/fund/{code}?days=180
  → {"profile": {...} | None, "series": [{"date","nav","equity_return"}, ...]}

业务层只读 fund_profile / fund_nav_history 缓存(延续 M6/M7 只读红线)。
仅当两张缓存表对该基金都完全没有数据时,才触发一次低频按需抓取并落库,
随后照常只读返回 —— 抓取仍收敛在 backend/datasource/,这里只是「首次访问兜底」。
"""
from backend.models.db import get_conn

DEFAULT_DAYS = 180


def _read_profile(conn, code):
    row = conn.execute(
        "SELECT fund_code,name,manager,scale,rate,syl_1n,syl_3y,syl_6y,syl_1y,updated_at "
        "FROM fund_profile WHERE fund_code=?", (code,)
    ).fetchone()
    return dict(row) if row else None


def _read_series(conn, code, days):
    rows = conn.execute(
        "SELECT nav_date, nav, equity_return FROM fund_nav_history WHERE fund_code=? "
        "ORDER BY nav_date DESC LIMIT ?",
        (code, days),
    ).fetchall()
    # 取最近 days 天后翻回升序,便于前端从左到右画图(与 M7 nav_history() 一致)
    return [
        {"date": r["nav_date"], "nav": r["nav"], "equity_return": r["equity_return"]}
        for r in reversed(rows)
    ]


def _ensure_cached(conn, code):
    """缓存完全缺失时,触发一次低频按需抓取并写入(profile + 历史序列),单次不重试。"""
    from backend.datasource.fund_profile import refresh_profile
    from backend.datasource.nav_history import refresh_nav_history

    refresh_profile(conn, [code])
    refresh_nav_history(conn, [code])


def get_fund_detail(ctx):
    code = (ctx.params.get("code") or "").strip()
    if not code:
        return (400, {"error": "缺少基金代码"})
    try:
        days = int(ctx.q("days", str(DEFAULT_DAYS)))
    except ValueError:
        days = DEFAULT_DAYS

    conn = get_conn()
    try:
        profile = _read_profile(conn, code)
        series = _read_series(conn, code, days)
        if profile is None and not series:
            # 缓存完全没有该基金的数据(首次访问)→ 低频抓一次入库再读
            _ensure_cached(conn, code)
            profile = _read_profile(conn, code)
            series = _read_series(conn, code, days)
        return {"profile": profile, "series": series}
    finally:
        conn.close()


ROUTES = [("GET", "/api/fund/{code}", get_fund_detail)]
