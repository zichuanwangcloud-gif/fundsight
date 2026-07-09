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


def compute_position(code, user_id):
    """由 fund_transaction 全部流水(按 trade_date 排序)加权推导持仓。

    返回 {"shares": float, "cost_amount": float, "avg_cost": float}。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT action, shares, amount FROM fund_transaction "
        "WHERE fund_code=? AND user_id=? ORDER BY trade_date, id",
        (code, user_id),
    ).fetchall()
    conn.close()

    shares = 0.0
    cost = 0.0
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
    }


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
