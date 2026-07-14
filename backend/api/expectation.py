# -*- coding: utf-8 -*-
"""PRD-07 预期深化 —— 年化目标 / 回本所需涨幅 / 达成时间推算(P0+P1 纯计算)。

延伸产品魂「现状 vs 预期」:把静态目标线升级为年化可比 + 回本清醒剂 + 达成时间轴。
全纯计算,不落新表、不加 holding 列(移动止盈需加列+scheduler,留后续切片)。

GET /api/holdings/expectations  → 需登录,按 user_id 隔离。
返回当前用户各持仓的预期派生:
  target_annual       目标收益率年化(跨持仓可比)
  current_return_pct  当前收益率(成本口径)
  recovery_pct        浮亏时回本所需涨幅 %(反直觉:亏20%要涨25%)
  days_to_target_est  按近3月增速推算达成天数(仅供参考,非承诺)
"""
import math
from datetime import date, datetime, timedelta

from backend.api._router import Ctx  # noqa: F401
from backend.models.db import get_conn


def _parse_created(s):
    """holding.created_at 兼容 'YYYY-MM-DD HH:MM:SS' 与 'YYYY-MM-DD'。

    注意 fmt 字符串长度(%Y 占 2 字符)不等于日期字符串长度,故按真实字符串长度截取。
    """
    if not s:
        return None
    s = s.strip().split(".")[0]  # 去可能的毫秒
    for fmt, slen in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(s[:slen], fmt).date()
        except ValueError:
            continue
    return None


def _current_value(h, q):
    """持仓当前市值:份额=hold_amount/dwjz,优先 nav(收盘)回落 gsz(盘中)。

    h: holding 行;q: fund_quote 行或 None。缺数据返回 None。
    与 app.enrich_holding 的 real_value/est_value 口径一致。
    """
    if not q or not h["hold_amount"] or not q["dwjz"]:
        return None
    shares = h["hold_amount"] / q["dwjz"]
    if q["nav"] is not None:
        return shares * q["nav"]
    if q["gsz"]:
        return shares * q["gsz"]
    return None


def _r_proj_annualized(conn, code):
    """近3月复权净值收益年化 r_proj(小数)。数据不足返回 None。"""
    d3 = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    start = conn.execute(
        "SELECT COALESCE(nav_adj, nav) AS nav FROM fund_nav_history "
        "WHERE fund_code=? AND nav_date <= ? AND nav IS NOT NULL "
        "ORDER BY nav_date DESC LIMIT 1", (code, d3),
    ).fetchone()
    latest = conn.execute(
        "SELECT COALESCE(nav_adj, nav) AS nav FROM fund_nav_history "
        "WHERE fund_code=? AND nav IS NOT NULL ORDER BY nav_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if not start or not latest or not start["nav"] or start["nav"] <= 0:
        return None
    r_3m = latest["nav"] / start["nav"] - 1
    if r_3m <= 0:
        return None
    return (1 + r_3m) ** (365.0 / 90) - 1


def _compute_for_holding(conn, h, q):
    """单条 holding + quote → 预期派生 dict。"""
    code = h["fund_code"]
    target_rate = h["target_rate"]
    cost = h["cost_amount"]

    # 目标年化
    target_annual = None
    if target_rate is not None:
        cd = _parse_created(h["created_at"])
        if cd:
            days = (date.today() - cd).days
            if days > 0:
                target_annual = round(
                    ((1 + target_rate / 100) ** (365.0 / days) - 1) * 100, 2
                )

    # 当前市值 / 收益率 / 回本所需涨幅
    cur_value = _current_value(h, q)
    current_return_pct = None
    recovery_pct = None
    if cur_value and cost:
        current_return_pct = round((cur_value / cost - 1) * 100, 2)
        if cur_value < cost:  # 浮亏才有回本所需涨幅
            recovery_pct = round((cost / cur_value - 1) * 100, 2)

    # 达成时间推算(按近3月增速年化)
    days_to_target_est = None
    note = None
    if target_rate is not None and current_return_pct is not None:
        gap_pct = target_rate - current_return_pct
        if gap_pct <= 0:
            note = "已达目标"
        else:
            r_proj = _r_proj_annualized(conn, code)
            if r_proj is not None and r_proj > 0:
                days_to_target_est = round(
                    math.log(1 + gap_pct / 100) / math.log(1 + r_proj) * 365
                )
            else:
                note = "当前趋势下短期无法达成"

    return {
        "fund_code": code,
        "target_rate": target_rate,
        "target_annual": target_annual,
        "current_return_pct": current_return_pct,
        "recovery_pct": recovery_pct,
        "days_to_target_est": days_to_target_est,
        "note": note,
    }


def get_expectations(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    conn = get_conn()
    try:
        holdings = conn.execute(
            "SELECT fund_code, target_rate, cost_amount, hold_amount, created_at "
            "FROM holding WHERE user_id=?", (ctx.user_id,)
        ).fetchall()
        items = []
        for h in holdings:
            q = conn.execute(
                "SELECT dwjz, gsz, nav FROM fund_quote WHERE fund_code=?",
                (h["fund_code"],),
            ).fetchone()
            items.append(_compute_for_holding(conn, h, q))
    finally:
        conn.close()
    return {"items": items}


ROUTES = [
    ("GET", "/api/holdings/expectations", get_expectations),
]
