# -*- coding: utf-8 -*-
"""PRD-03 组合层汇总 —— 资产配置占比 + 持仓集中度(线路 P0)。

app.py 的 summarize 已提供总市值/总盈亏/总收益率,本模块补 app.py 缺失的两块:
  - 资产配置分布:持仓市值按 fund_type 归 8 大类(market.CLASSIFY_PRIORITY 口径)
  - 持仓集中度:TOP1 占比 + CR3,单基金 > 40% 预警

为避免循环依赖(api 模块不 import app.py),市值在模块内重算轻量版,
口径与 app.enrich_holding 对齐:份额=hold_amount/dwjz,市值优先 nav(收盘官方),
回落 gsz(盘中估值)。

GET /api/portfolio/summary  → 需登录,按 user_id 隔离。
"""
from backend.api._router import Ctx  # noqa: F401
from backend.models.db import get_conn

# 单基金集中度预警阈值(私享自用写死;>40% 视为集中度偏高)
_CONCENTRATION_WARN_THRESHOLD = 0.40


def _classify(fund_type):
    """复用 market.CLASSIFY_PRIORITY 把 fund_type 归 8 大类,不命中归 '其他'。"""
    from backend.api.market import CLASSIFY_PRIORITY
    if not fund_type:
        return "其他"
    for cat, keywords in CLASSIFY_PRIORITY:
        if any(kw in fund_type for kw in keywords):
            return cat
    return "其他"


def _market_value(hold_amount, dwjz, gsz, nav):
    """单只持仓市值:份额=hold_amount/dwjz,市值优先 nav(收盘),回落 gsz(盘中)。

    hold_amount 为按昨日净值 dwjz 的持仓金额;缺 dwjz 或 hold_amount 时无法反推份额,
    返回 None。与 app.enrich_holding 的 real_value / est_value 口径一致。
    """
    if not hold_amount or not dwjz:
        return None
    shares = hold_amount / dwjz
    if nav is not None:
        return shares * nav
    if gsz:
        return shares * gsz
    return None


def _compute_summary(conn, user_id):
    rows = conn.execute(
        "SELECT h.fund_code, h.hold_amount, h.cost_amount, "
        "q.gsz, q.dwjz, q.nav, fl.fund_type "
        "FROM holding h "
        "LEFT JOIN fund_quote q ON q.fund_code = h.fund_code "
        "LEFT JOIN fund_list fl ON fl.fund_code = h.fund_code "
        "WHERE h.user_id = ?",
        (user_id,),
    ).fetchall()

    total_value = 0.0
    total_cost = 0.0
    alloc_amount = {}     # cat -> 市值金额
    per_fund = []         # [(fund_code, value)] 用于集中度
    for r in rows:
        value = _market_value(r["hold_amount"], r["dwjz"], r["gsz"], r["nav"])
        if value is None:
            continue
        value = round(value, 2)
        total_value += value
        per_fund.append((r["fund_code"], value))
        cat = _classify(r["fund_type"])
        alloc_amount[cat] = round(alloc_amount.get(cat, 0.0) + value, 2)
        cost = r["cost_amount"]
        if cost:
            total_cost += cost

    total_cost = round(total_cost, 2)
    total_value = round(total_value, 2)
    total_pnl = round(total_value - total_cost, 2) if total_cost else None
    total_return_pct = (
        round((total_value - total_cost) / total_cost * 100, 2) if total_cost else None
    )

    # 资产配置:按 market.DISPLAY_ORDER 8 大类 + 末尾"其他"
    from backend.api.market import DISPLAY_ORDER
    allocation = []
    for c in DISPLAY_ORDER:
        amt = alloc_amount.get(c, 0.0)
        allocation.append({
            "cat": c, "amount": round(amt, 2),
            "ratio": round(amt / total_value, 3) if total_value else 0.0,
        })
    if "其他" in alloc_amount:
        amt = alloc_amount["其他"]
        allocation.append({
            "cat": "其他", "amount": round(amt, 2),
            "ratio": round(amt / total_value, 3) if total_value else 0.0,
        })

    # 持仓集中度
    per_fund.sort(key=lambda x: -x[1])
    top1 = per_fund[0] if per_fund else None
    top1_ratio = round(top1[1] / total_value, 3) if (top1 and total_value) else 0.0
    cr3 = (
        round(sum(v for _, v in per_fund[:3]) / total_value, 3)
        if total_value else 0.0
    )

    return {
        "total_market_value": total_value,
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "total_return_pct": total_return_pct,
        "holdings_count": len(per_fund),
        "allocation": allocation,
        "concentration": {
            "top1_fund_code": top1[0] if top1 else None,
            "top1_ratio": top1_ratio,
            "warn": top1_ratio > _CONCENTRATION_WARN_THRESHOLD,
            "cr3": cr3,
        },
        "note": None,
    }


def get_portfolio_summary(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    conn = get_conn()
    try:
        summary = _compute_summary(conn, ctx.user_id)
    finally:
        conn.close()
    return summary


ROUTES = [
    ("GET", "/api/portfolio/summary", get_portfolio_summary),
]
