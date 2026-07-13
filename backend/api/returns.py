# -*- coding: utf-8 -*-
"""基金阶段收益率(线路 F / M9-F)。

基于 fund_nav_history 只读计算各阶段收益率(%),不落新数据、不画走势图
——「收益走势历史图」为立项 Out of Scope,这里只做点状统计(近1月/近3月/
今年以来/成立以来),复用 M7 已落库的历史净值序列。

GET /api/fund/{code}/returns
  → {"fund_code": code, "periods": {"m1": x, "m3": y, "ytd": z, "max": w}}
  任一阶段数据不足(nav_history 缺对应起点)时该值为 null。
"""
from backend.api._router import Ctx  # noqa: F401
from backend.models.db import get_conn


def _nav_on_or_before(conn, code, target_date):
    """取 <= target_date 的最近一条 nav(阶段起点)。返回 nav 或 None。"""
    row = conn.execute(
        "SELECT nav FROM fund_nav_history WHERE fund_code=? "
        "AND nav_date <= ? AND nav IS NOT NULL ORDER BY nav_date DESC LIMIT 1",
        (code, target_date),
    ).fetchone()
    return row["nav"] if row else None


def _compute_periods(conn, code):
    from datetime import date, timedelta
    today = date.today()
    latest = conn.execute(
        "SELECT nav FROM fund_nav_history WHERE fund_code=? AND nav IS NOT NULL "
        "ORDER BY nav_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if not latest:
        return {"m1": None, "m3": None, "ytd": None, "max": None}
    latest_nav = latest["nav"]

    def rate(target_date):
        start = _nav_on_or_before(conn, code, target_date)
        if not start:
            return None
        return round((latest_nav - start) / start * 100, 2)

    d_m1 = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    d_m3 = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    d_ytd = f"{today.year}-01-01"
    earliest = conn.execute(
        "SELECT nav FROM fund_nav_history WHERE fund_code=? AND nav IS NOT NULL "
        "ORDER BY nav_date ASC LIMIT 1",
        (code,),
    ).fetchone()
    max_ret = None
    if earliest and earliest["nav"]:
        max_ret = round((latest_nav - earliest["nav"]) / earliest["nav"] * 100, 2)
    return {
        "m1": rate(d_m1),
        "m3": rate(d_m3),
        "ytd": rate(d_ytd),
        "max": max_ret,
    }


def get_returns(ctx):
    code = (ctx.params.get("code") or "").strip()
    if not code:
        return (400, {"error": "缺少基金代码"})
    conn = get_conn()
    try:
        periods = _compute_periods(conn, code)
    finally:
        conn.close()
    return {"fund_code": code, "periods": periods}


ROUTES = [("GET", "/api/fund/{code}/returns", get_returns)]
