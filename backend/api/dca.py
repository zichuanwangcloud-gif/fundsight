# -*- coding: utf-8 -*-
"""PRD-04 定投模拟与计划 —— P0 定投模拟器(DCA 回测 + 一次性对比)。

基于 fund_nav_history 只读回测定投收益,读 nav_adj(复权口径,PRD-02 提供;
COALESCE 回落 nav)。不落新表、不画走势曲线——nav_low/nav_high 只给点状
最低/最高点,守"不画走势图"红线。

GET /api/fund/{code}/dca-simulate?start=&end=&amount=&freq=&invest_day=
  freq: monthly(默认)/biweekly/weekly
  invest_day: 每月几号(1-28,monthly)或忽略(biweekly/weekly 从 start 起步长)
  节假日(无净值日)顺延到下一个有净值日。
"""
import bisect

from backend.api._router import Ctx  # noqa: F401
from backend.models.db import get_conn


def _nav_series(conn, code, start, end):
    """区间升序 [(date_str, nav_adj|nav)]。nav 必非空。"""
    return [
        (r["nav_date"], r["nav"])
        for r in conn.execute(
            "SELECT nav_date, COALESCE(nav_adj, nav) AS nav FROM fund_nav_history "
            "WHERE fund_code=? AND nav_date >= ? AND nav_date <= ? AND nav IS NOT NULL "
            "ORDER BY nav_date ASC",
            (code, start, end),
        ).fetchall()
    ]


def _nav_on_or_after(nav_list, d):
    """nav_list 升序,找 >= d 的第一个。返回 (date, nav) 或 None。"""
    dates = [x[0] for x in nav_list]
    i = bisect.bisect_left(dates, d)
    return nav_list[i] if i < len(dates) else None


def _dca_dates(start, end, freq, invest_day):
    """生成定投日期列表(ISO 字符串)。"""
    from datetime import date, timedelta
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    out = []
    if freq == "monthly":
        y, m = s.year, s.month
        while (y, m) <= (e.year, e.month):
            try:
                d = date(y, m, invest_day)
            except ValueError:
                d = None
            if d and s <= d <= e:
                out.append(d.isoformat())
            m += 1
            if m > 12:
                y, m = y + 1, 1
    elif freq == "biweekly":
        d = s
        while d <= e:
            out.append(d.isoformat())
            d = d + timedelta(days=14)
    else:  # weekly
        d = s
        while d <= e:
            out.append(d.isoformat())
            d = d + timedelta(days=7)
    return out


def _empty(code, note):
    return {
        "fund_code": code,
        "total_invested": 0.0, "total_shares": 0.0, "final_value": 0.0,
        "dca_return_pct": None, "annualized_return": None,
        "lump_return_pct": None, "diff": None,
        "nav_low": None, "nav_high": None, "periods": 0, "note": note,
    }


def get_dca_simulate(ctx):
    code = (ctx.params.get("code") or "").strip()
    if not code:
        return (400, {"error": "缺少基金代码"})
    start = ctx.q("start", "").strip()
    end = ctx.q("end", "").strip()
    if not start or not end:
        return (400, {"error": "缺少 start/end"})
    try:
        amount = float(ctx.q("amount", "1000"))
    except ValueError:
        return (400, {"error": "amount 非法"})
    if amount <= 0:
        return (400, {"error": "amount 须为正"})
    freq = ctx.q("freq", "monthly").strip()
    if freq not in ("monthly", "biweekly", "weekly"):
        freq = "monthly"
    try:
        invest_day = int(ctx.q("invest_day", "1"))
    except ValueError:
        invest_day = 1
    invest_day = max(1, min(28, invest_day))

    conn = get_conn()
    try:
        nav_list = _nav_series(conn, code, start, end)
    finally:
        conn.close()
    if not nav_list:
        return _empty(code, "区间无净值数据")

    dca_dates = _dca_dates(start, end, freq, invest_day)
    total_invested = 0.0
    total_shares = 0.0
    for d in dca_dates:
        hit = _nav_on_or_after(nav_list, d)
        if hit is None:
            continue
        total_shares += amount / hit[1]
        total_invested += amount

    start_nav = nav_list[0][1]
    end_nav = nav_list[-1][1]
    final_value = total_shares * end_nav if total_shares else 0.0
    dca_return_pct = (
        round((final_value - total_invested) / total_invested * 100, 2)
        if total_invested else None
    )

    # 年化(按区间天数复利)
    from datetime import date
    annualized = None
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if total_invested and days > 0 and final_value > 0:
        annualized = round(((final_value / total_invested) ** (365.0 / days) - 1) * 100, 2)

    # 一次性:total_invested 在 start_nav 一次性买入
    lump_return_pct = None
    if total_invested and start_nav:
        lump_final = (total_invested / start_nav) * end_nav
        lump_return_pct = round((lump_final - total_invested) / total_invested * 100, 2)
    diff = (
        round(dca_return_pct - lump_return_pct, 2)
        if dca_return_pct is not None and lump_return_pct is not None else None
    )

    low = min(nav_list, key=lambda x: x[1])
    high = max(nav_list, key=lambda x: x[1])

    return {
        "fund_code": code,
        "total_invested": round(total_invested, 2),
        "total_shares": round(total_shares, 4),
        "final_value": round(final_value, 2),
        "dca_return_pct": dca_return_pct,
        "annualized_return": annualized,
        "lump_return_pct": lump_return_pct,
        "diff": diff,
        "nav_low": {"date": low[0], "value": round(low[1], 4)},
        "nav_high": {"date": high[0], "value": round(high[1], 4)},
        "periods": len(dca_dates),
        "note": None,
    }


ROUTES = [
    ("GET", "/api/fund/{code}/dca-simulate", get_dca_simulate),
]
