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


# --------------------------------------------------------------------------- #
# PRD-04 P1 定投计划 CRUD —— GET/POST/PUT/DELETE /api/dca/plans
#
# 用户设定定投频率与金额,scheduler 日更巡检到点推 dca_due 站内通知(去重),
# next_date 滚动到下一期。按 user_id 隔离。站内角标,非手机/Web Push(红线)。
# --------------------------------------------------------------------------- #
_VALID_FREQS = ("monthly", "biweekly", "weekly")


def _roll_next_date(freq, invest_day, from_date):
    """从 from_date 起算下一个定投日(不含当日),返回 date。"""
    from datetime import timedelta
    if freq == "monthly":
        y, m = from_date.year, from_date.month
        m += 1
        if m > 12:
            y, m = y + 1, 1
        try:
            from datetime import date
            return date(y, m, invest_day)
        except ValueError:
            from datetime import date
            return date(y, m, 28)
    if freq == "biweekly":
        return from_date + timedelta(days=14)
    return from_date + timedelta(days=7)


def list_dca_plans(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,fund_code,per_amount,freq,invest_day,next_date,active,created_at "
            "FROM dca_plan WHERE user_id=? ORDER BY id", (ctx.user_id,)
        ).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


def add_dca_plan(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    d = ctx.body or {}
    fund_code = (d.get("fund_code") or "").strip()
    freq = (d.get("freq") or "monthly").strip()
    if not fund_code or freq not in _VALID_FREQS:
        return (400, {"error": "fund_code/freq 非法"})
    try:
        per_amount = float(d.get("per_amount"))
        invest_day = int(d.get("invest_day", 1))
    except (TypeError, ValueError):
        return (400, {"error": "per_amount/invest_day 非法"})
    if per_amount <= 0:
        return (400, {"error": "per_amount 须为正"})
    invest_day = max(1, min(28, invest_day))
    from datetime import date
    next_date = _roll_next_date(freq, invest_day, date.today()).isoformat()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO dca_plan(user_id,fund_code,per_amount,freq,invest_day,next_date,"
            "active,created_at) VALUES(?,?,?,?,?,?,1,datetime('now','localtime'))",
            (ctx.user_id, fund_code, per_amount, freq, invest_day, next_date))
        conn.commit()
        pid = cur.lastrowid
    finally:
        conn.close()
    return {"ok": True, "id": pid, "next_date": next_date}


def update_dca_plan(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    d = ctx.body or {}
    fields, vals = [], []
    for k in ("per_amount", "invest_day", "active", "next_date"):
        if k in d:
            fields.append(f"{k}=?")
            vals.append(d[k])
    if not fields:
        return (400, {"error": "无可更新字段"})
    vals.append(ctx.params.get("id"))
    vals.append(ctx.user_id)
    conn = get_conn()
    try:
        conn.execute(f"UPDATE dca_plan SET {','.join(fields)} WHERE id=? AND user_id=?", vals)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def delete_dca_plan(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    conn = get_conn()
    try:
        conn.execute("DELETE FROM dca_plan WHERE id=? AND user_id=?",
                     (ctx.params.get("id"), ctx.user_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


ROUTES = [
    ("GET", "/api/fund/{code}/dca-simulate", get_dca_simulate),
    ("GET", "/api/dca/plans", list_dca_plans),
    ("POST", "/api/dca/plans", add_dca_plan),
    ("PUT", "/api/dca/plans/{id}", update_dca_plan),
    ("DELETE", "/api/dca/plans/{id}", delete_dca_plan),
]
