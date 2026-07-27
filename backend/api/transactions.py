# -*- coding: utf-8 -*-
"""线路 C —— 交易流水记录:买卖流水的增删查 + 加权成本推导持仓。

接口:
  GET    /api/transactions?code=      某基金(或全部,不传 code)的流水列表,
                                       按 user_id 过滤;若传 code 则附带
                                       该基金由流水推导出的持仓(position)。
  POST   /api/transactions            新增一笔流水(需登录)。
  DELETE /api/transactions/{id}       删一笔(校验 user_id 归属)。

compute_position(code, user_id) 是本线路的核心纯函数:按 trade_date 顺序回放
该基金全部流水,加权推导剩余份额与持仓成本 —— buy 累加 shares 与成本(amount);
sell 按当前加权平均成本冲减:冲减金额 = avg_cost * 实际卖出份额,不改变剩余
份额的单位成本。边界策略:
  - 卖出份额超过当前持有量 → 按实际持有量全部卖出(不做空、不报错)。
  - 尚未买入就卖出(脏数据) → 该笔流水忽略,不产生负份额/负成本。
  - 全部卖出后份额与成本归零。
"""
from backend.models.db import get_conn

VALID_ACTIONS = ("buy", "sell")


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def compute_position(code, user_id, conn=None):
    """由 fund_transaction 全部流水(按 trade_date 排序)加权推导持仓。

    返回 {"shares", "cost_amount", "avg_cost", "realized_pnl", "has_tx"}。
      realized_pnl —— 累计已实现盈亏(落袋):卖出时 卖出金额 − 均摊成本×卖出份额,
                      正为落袋盈利、负为割肉亏损。超卖按实际卖出份额等比例折算卖出金额。
      has_tx       —— 是否存在任一流水(用于 resolve_position 判断账本来源)。

    conn 可选:传入则复用(不关闭),不传则自开自关。
    """
    own = conn is None
    if own:
        conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT action, shares, amount FROM fund_transaction "
            "WHERE fund_code=? AND user_id=? ORDER BY trade_date, id",
            (code, user_id),
        ).fetchall()
    finally:
        if own:
            conn.close()

    shares = 0.0
    cost = 0.0
    realized = 0.0
    for r in rows:
        s = r["shares"] or 0.0
        amt = r["amount"] or 0.0
        if r["action"] == "buy":
            shares += s
            cost += amt
        elif r["action"] == "sell":
            if shares <= 0:
                continue  # 脏数据(未持有先卖):忽略,不产生负份额
            avg_cost = cost / shares
            sell_shares = min(s, shares)  # 超卖按实际持有量清仓,不做空
            # 超卖时按实际卖出份额等比例折算本笔卖出金额(避免把未成交的钱算进落袋)
            sell_amount = amt * (sell_shares / s) if s > 0 else 0.0
            realized += sell_amount - avg_cost * sell_shares
            cost -= avg_cost * sell_shares
            shares -= sell_shares
            if shares <= 1e-9:
                shares = 0.0
                cost = 0.0

    avg_cost = cost / shares if shares else 0.0
    return {
        "shares": round(shares, 6),
        "cost_amount": round(cost, 6),
        "avg_cost": round(avg_cost, 6),
        "realized_pnl": round(realized, 6),
        "has_tx": bool(rows),
    }


def resolve_position(conn, code, user_id):
    """账本统一入口(流水优先 + 手填兼容):某用户某基金的权威持仓。

    有流水记录 → 用 compute_position 推导(份额/成本/已实现盈亏均可对账);
    无流水     → 回退 holding 手填(hold_amount/cost_amount),realized_pnl=0。

    返回统一结构:
      source        "transaction" | "holding" | "empty"
      shares        流水口径的份额;holding 口径为 None(市值按 hold_amount/dwjz 反推)
      cost_amount   持仓成本(可 None)
      realized_pnl  累计已实现盈亏(holding 口径恒 0.0)
      hold_amount   holding 手填金额(流水口径为 None)
    """
    tx = compute_position(code, user_id, conn=conn)
    if tx["has_tx"]:
        return {
            "source": "transaction",
            "shares": tx["shares"],
            "cost_amount": tx["cost_amount"],
            "realized_pnl": tx["realized_pnl"],
            "hold_amount": None,
        }
    h = conn.execute(
        "SELECT hold_amount, cost_amount FROM holding WHERE fund_code=? AND user_id=?",
        (code, user_id),
    ).fetchone()
    if h is None:
        return {"source": "empty", "shares": None, "cost_amount": None,
                "realized_pnl": 0.0, "hold_amount": None}
    return {
        "source": "holding",
        "shares": None,
        "cost_amount": h["cost_amount"],
        "realized_pnl": 0.0,
        "hold_amount": h["hold_amount"],
    }


def position_market_value(pos, dwjz, gsz, nav):
    """统一市值计算,兼容两种账本口径。市值优先 nav(收盘)回落 gsz(盘中)。

    - 流水口径:市值 = shares × 现价。
    - 手填口径:份额 = hold_amount / dwjz,市值 = 份额 × 现价(与旧 _market_value 一致)。
    缺关键数据返回 None。
    """
    price = nav if nav is not None else (gsz if gsz else None)
    if price is None:
        return None
    if pos.get("shares") is not None:
        return pos["shares"] * price
    if pos.get("hold_amount") and dwjz:
        return (pos["hold_amount"] / dwjz) * price
    return None


def add_transaction(data, user_id):
    """新增一笔流水 → 返回新记录 id;数据非法返回 None。"""
    fund_code = (data.get("fund_code") or "").strip()
    action = (data.get("action") or "").strip().lower()
    if not fund_code or action not in VALID_ACTIONS:
        return None

    shares = _num(data.get("shares"))
    price = _num(data.get("price"))
    amount = _num(data.get("amount"))
    if amount is None and shares is not None and price is not None:
        amount = shares * price
    if shares is None or amount is None:
        return None

    trade_date = (data.get("trade_date") or "").strip()

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO fund_transaction(user_id,fund_code,action,shares,price,amount,"
        "trade_date,created_at) VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))",
        (user_id, fund_code, action, shares, price, amount, trade_date),
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def list_transactions(user_id, code=None):
    """某用户的流水列表,可选按 fund_code 过滤,按交易日期倒序。"""
    conn = get_conn()
    if code:
        rows = conn.execute(
            "SELECT * FROM fund_transaction WHERE user_id=? AND fund_code=? "
            "ORDER BY trade_date DESC, id DESC",
            (user_id, code),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM fund_transaction WHERE user_id=? ORDER BY trade_date DESC, id DESC",
            (user_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_transaction(tid, user_id):
    """删一笔流水,校验 user_id 归属(越权删除不生效)。"""
    conn = get_conn()
    conn.execute("DELETE FROM fund_transaction WHERE id=? AND user_id=?", (tid, user_id))
    conn.commit()
    conn.close()


# ---- 路由 handler ----

def _h_list(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    code = ctx.q("code", "").strip()
    items = list_transactions(ctx.user_id, code or None)
    position = compute_position(code, ctx.user_id) if code else None
    return {"items": items, "position": position}


def _h_add(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    tid = add_transaction(ctx.body, ctx.user_id)
    if tid is None:
        return (400, {"error": "invalid transaction"})
    return {"ok": True, "id": tid}


def _h_delete(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    delete_transaction(ctx.params.get("id"), ctx.user_id)
    return {"ok": True}


ROUTES = [
    ("GET", "/api/transactions", _h_list),
    ("POST", "/api/transactions", _h_add),
    ("DELETE", "/api/transactions/{id}", _h_delete),
]
