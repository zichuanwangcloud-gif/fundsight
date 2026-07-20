# -*- coding: utf-8 -*-
"""基金详情页 API —— 净值走势/涨跌柱 + 基本面(线路 B / M8)。

GET /api/fund/{code}?days=180
  → {"profile": {...} | None, "series": [{"date","nav","equity_return"}, ...]}

业务层只读 fund_profile / fund_nav_history 缓存(延续 M6/M7 只读红线)。
仅当两张缓存表对该基金都完全没有数据时,才触发一次低频按需抓取并落库,
随后照常只读返回 —— 抓取仍收敛在 backend/datasource/,这里只是「首次访问兜底」。
"""
from datetime import datetime

from backend.models.db import get_conn

DEFAULT_DAYS = 180


def _read_profile(conn, code):
    row = conn.execute(
        "SELECT fund_code,name,manager,scale,rate,syl_1n,syl_3y,syl_6y,syl_1y,"
        "asset_alloc_stock,asset_alloc_bond,asset_alloc_cash,"
        "holder_inst,holder_retail,"
        "peer_percentile,peer_rank,peer_total,updated_at "
        "FROM fund_profile WHERE fund_code=?", (code,)
    ).fetchone()
    return dict(row) if row else None


def _read_series(conn, code, days):
    rows = conn.execute(
        "SELECT nav_date, nav, equity_return, nav_adj, equity_return_adj "
        "FROM fund_nav_history WHERE fund_code=? "
        "ORDER BY nav_date DESC LIMIT ?",
        (code, days),
    ).fetchall()
    # 取最近 days 天后翻回升序,便于前端从左到右画图(与 M7 nav_history() 一致)
    return [
        {
            "date": r["nav_date"],
            "nav": r["nav"],
            "equity_return": r["equity_return"],
            "nav_adj": r["nav_adj"],
            "equity_return_adj": r["equity_return_adj"],
        }
        for r in reversed(rows)
    ]


def _ensure_cached(conn, code):
    """缓存完全缺失时,触发一次低频按需抓取并写入(profile + 历史序列),单次不重试。"""
    from backend.datasource.fund_profile import refresh_profile
    from backend.datasource.nav_history import refresh_nav_history

    refresh_profile(conn, [code])
    refresh_nav_history(conn, [code])


def _ensure_intraday_seed(conn, code):
    """今日无该基金盘中时序时,后台触发一次按需采集(不阻塞响应)。

    市场页基金不在持仓里、后台 quote_refresh 尚未采到它 —— 用户点开详情时
    若今日 fund_quote_tick 无记录,触发 trigger_quote_for 补首个点,几秒后
    前端 30s 轮询即见数据,之后后台 60s 周期继续采。表缺失(tick 未建)降级静默。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        row = conn.execute(
            "SELECT 1 FROM fund_quote_tick WHERE fund_code=? AND quote_date=? LIMIT 1",
            (code, today),
        ).fetchone()
    except Exception:  # noqa: BLE001 —— 表缺失等,静默降级
        return
    if row:
        return
    from backend.scheduler import trigger_quote_for
    trigger_quote_for(code)


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
        # 今日盘中时序缺失则后台补采(不阻塞,前端轮询即见)
        _ensure_intraday_seed(conn, code)
        return {"profile": profile, "series": series}
    finally:
        conn.close()


ROUTES = [("GET", "/api/fund/{code}", get_fund_detail)]
