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


# --------------------------------------------------------------------------- #
# M10A 收益分析深化 —— 分批买入成本曲线 + 阶段收益归因
#
# 仍在「收益走势历史图(K 线/连续曲线)」红线之外:只做点状/分批。
# 只读 fund_transaction(基金维度,不按 user_id 隔离——私享级自用) +
# fund_nav_history,不落新表。新端点读私有交易数据,故校验登录态。
# --------------------------------------------------------------------------- #
def _buy_rows(conn, code):
    """该基金全部买入流水,按 trade_date 升序(基金维度,不按用户隔离)。"""
    return conn.execute(
        "SELECT shares, price, amount, trade_date FROM fund_transaction "
        "WHERE fund_code=? AND action='buy' AND shares IS NOT NULL AND shares>0 "
        "ORDER BY trade_date, id",
        (code,),
    ).fetchall()


def _batch_cost_price(row):
    """单批次成本单价:优先取 price,缺失则 amount/shares,再缺则 None。"""
    price = row["price"]
    if price not in (None, ""):
        try:
            return float(price)
        except (TypeError, ValueError):
            pass
    shares = row["shares"] or 0.0
    amount = row["amount"]
    if amount not in (None, "") and shares > 0:
        try:
            return float(amount) / shares
        except (TypeError, ValueError):
            return None
    return None


def _latest_nav(conn, code):
    row = conn.execute(
        "SELECT nav FROM fund_nav_history WHERE fund_code=? AND nav IS NOT NULL "
        "ORDER BY nav_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    return row["nav"] if row else None


def get_cost_curve(ctx):
    """分批买入加权成本点列 —— 每次买入后的累计成本与加权单价(点状,不连曲线)。

    GET /api/fund/{code}/cost-curve
      → {"fund_code": code, "points": [{date, shares, cost_basis, weighted_price}]}
    无买入记录时 points 为空列表(前端不报错)。需登录。
    """
    if ctx.user_id is None:
        return (401, {"error": "需登录"})
    code = (ctx.params.get("code") or "").strip()
    if not code:
        return (400, {"error": "缺少基金代码"})
    conn = get_conn()
    try:
        rows = _buy_rows(conn, code)
    finally:
        conn.close()
    points = []
    cum_shares = 0.0
    cum_amount = 0.0
    for r in rows:
        shares = float(r["shares"] or 0.0)
        amount = r["amount"]
        try:
            amount = float(amount) if amount not in (None, "") else 0.0
        except (TypeError, ValueError):
            amount = 0.0
        cum_shares += shares
        cum_amount += amount
        wp = round(cum_amount / cum_shares, 4) if cum_shares > 0 else None
        points.append({
            "date": r["trade_date"],
            "shares": round(cum_shares, 4),
            "cost_basis": round(cum_amount, 2),
            "weighted_price": wp,
        })
    return {"fund_code": code, "points": points}


def _attribution_for_period(conn, code, start_date, is_max):
    """单阶段归因。is_max 时含全部批次且只依赖期末净值;否则按起点 nav 过滤批次。

    返回 {"batches":[...], "total": x} 或 None(数据不足)。
    批次贡献 = 批次份额 × (期末净值 − 批次成本)(spec §4 公式)。
    """
    end_nav = _latest_nav(conn, code)
    if not end_nav:
        return None
    if not is_max:
        start_nav = _nav_on_or_before(conn, code, start_date)
        if not start_nav:
            return None
    rows = _buy_rows(conn, code)
    if not is_max and start_date:
        rows = [r for r in rows if (r["trade_date"] or "") <= start_date]
    batches = []
    total = 0.0
    for r in rows:
        shares = float(r["shares"] or 0.0)
        cost = _batch_cost_price(r)
        if cost is None:
            continue
        contrib = round(shares * (end_nav - cost), 2)
        total += contrib
        batches.append({
            "date": r["trade_date"],
            "shares": round(shares, 4),
            "cost_price": round(cost, 4),
            "contribution": contrib,
        })
    # 占比(防御除零)
    for b in batches:
        b["ratio"] = round(b["contribution"] / total, 4) if total else 0.0
    return {"batches": batches, "total": round(total, 2)}


def get_returns_attribution(ctx):
    """阶段收益按批次归因 —— m1/m3/ytd 仅计该阶段起点前已持有的批次,
    max 含全部批次。数据不足(期末净值缺失,或 m1/m3/ytd 起点净值缺失)→ null。

    GET /api/fund/{code}/returns-attribution
      → {"fund_code": code, "periods": {m1: {batches,total}|null, ...}}
    需登录;数据按基金维度不隔离用户(私享自用)。
    """
    if ctx.user_id is None:
        return (401, {"error": "需登录"})
    code = (ctx.params.get("code") or "").strip()
    if not code:
        return (400, {"error": "缺少基金代码"})
    from datetime import date, timedelta
    today = date.today()
    d_m1 = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    d_m3 = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    d_ytd = f"{today.year}-01-01"
    conn = get_conn()
    try:
        periods = {
            "m1": _attribution_for_period(conn, code, d_m1, False),
            "m3": _attribution_for_period(conn, code, d_m3, False),
            "ytd": _attribution_for_period(conn, code, d_ytd, False),
            "max": _attribution_for_period(conn, code, None, True),
        }
    finally:
        conn.close()
    return {"fund_code": code, "periods": periods}


ROUTES = [
    ("GET", "/api/fund/{code}/returns", get_returns),
    ("GET", "/api/fund/{code}/cost-curve", get_cost_curve),
    ("GET", "/api/fund/{code}/returns-attribution", get_returns_attribution),
]
