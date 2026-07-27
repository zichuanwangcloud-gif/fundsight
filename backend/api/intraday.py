# -*- coding: utf-8 -*-
"""今日盘中实时涨幅时序 API —— 详情页「今日实时涨幅」+「今日预估盈亏(元)」折线用。

GET /api/fund/{code}/intraday
  → {"code","date","market_open","latest":{gszzl,gsz,dwjz,gztime,updated_at}|None,
     "ticks":[{"quote_time","gsz","gszzl"}, ...],
     # 以下仅当登录用户持有该基金且录了金额时有值,否则 None/空:
     "hold_amount", "pl_baseline"(=dwjz), "latest_pl", "pl_ticks":[{quote_time,pl}]}

业务层只读 fund_quote_tick(今日)+ fund_quote(最新快照),绝不触发抓取
(抓取由 scheduler quote_refresh 后台 + fund_detail._ensure_intraday_seed 按需兜底)。
公共行情数据,无需登录校验(与 /api/fund/{code} 一致),仍受 app.py 限流保护。
预估盈亏口径与 app.enrich_holding 的 today_pl 严格一致:份额=金额/dwjz,pl=份额*(gsz-dwjz)。
"""
from datetime import datetime

from backend.datasource.fundgz import is_market_open
from backend.models.db import get_conn


def get_intraday(ctx):
    code = (ctx.params.get("code") or "").strip()
    if not code:
        return (400, {"error": "缺少基金代码"})

    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        try:
            ticks = [
                {"quote_time": r["quote_time"], "gsz": r["gsz"], "gszzl": r["gszzl"]}
                for r in conn.execute(
                    "SELECT quote_time, gsz, gszzl FROM fund_quote_tick "
                    "WHERE fund_code=? AND quote_date=? ORDER BY quote_time",
                    (code, today),
                ).fetchall()
            ]
        except Exception:  # noqa: BLE001 —— 表缺失等,静默降级为空时序
            ticks = []
        latest = conn.execute(
            "SELECT gszzl, gsz, dwjz, gztime, updated_at FROM fund_quote WHERE fund_code=?",
            (code,),
        ).fetchone()
        # 今日预估盈亏(元)时序:仅登录用户 + 持有该基金 + 录了金额 时计算。
        hold_amount = None
        if ctx.user_id is not None:
            hrow = conn.execute(
                "SELECT hold_amount FROM holding WHERE user_id=? AND fund_code=?",
                (ctx.user_id, code),
            ).fetchone()
            if hrow:
                hold_amount = hrow["hold_amount"]
    finally:
        conn.close()

    latest_d = dict(latest) if latest else None
    # baseline 用最新快照 dwjz(=最近收盘,估算基准),与 enrich_holding 口径一致。
    baseline = latest_d["dwjz"] if latest_d else None
    pl_ticks = []
    latest_pl = None
    if hold_amount and baseline:
        shares = hold_amount / baseline
        pl_ticks = [
            {"quote_time": t["quote_time"], "pl": round(shares * (t["gsz"] - baseline), 2)}
            for t in ticks if t.get("gsz") is not None
        ]
        if latest_d and latest_d.get("gsz") is not None:
            latest_pl = round(shares * (latest_d["gsz"] - baseline), 2)

    return {
        "code": code,
        "date": today,
        "market_open": is_market_open(),
        "latest": latest_d,
        "ticks": ticks,
        "hold_amount": hold_amount,
        "pl_baseline": baseline if hold_amount else None,
        "latest_pl": latest_pl,
        "pl_ticks": pl_ticks,
    }


ROUTES = [("GET", "/api/fund/{code}/intraday", get_intraday)]
