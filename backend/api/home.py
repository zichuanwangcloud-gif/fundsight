# -*- coding: utf-8 -*-
"""首页概览「今天」—— 薄聚合层(方向 A)。

把已有能力收口到第一屏,不做新抓取、不加新表:
  - 组合汇总:复用 portfolio._compute_summary(流水优先 + 手填兼容)
  - 今日盈亏 / 各基金涨跌:读 fund_quote(gszzl 或 gsz vs dwjz)
  - 离目标进度:目标收益率进度 / 距止盈 / 浮亏回本进度(点状,不画走势曲线)
  - 今天要看:止盈止损/移动止盈触发 + 定投到点 + 未读通知(复用已有口径)
  - AI 早晚报:单独端点、点开才生成 + 当日进程内缓存(不盘中烧 token)

接口:
  GET /api/home/overview   一次性拿齐首页所有非 AI 数据(需登录)
  GET /api/home/briefing   AI 一句话早晚报(需登录;无 key 优雅降级;当日缓存)

红线:纯聚合只读缓存;进度用百分比/进度条呈现,不画连续走势曲线;AI 仅按需触发。
"""
from datetime import date

from backend.api._router import Ctx  # noqa: F401
from backend.api import portfolio
from backend.api.transactions import resolve_position, position_market_value
from backend.models.db import get_conn


def _today_change_pct(q):
    """今日涨跌幅 %:优先 gszzl(盘中估算涨跌幅),回落 (gsz-dwjz)/dwjz。"""
    if q is None:
        return None
    if q["gszzl"] is not None:
        return round(q["gszzl"], 2)
    if q["gsz"] and q["dwjz"]:
        return round((q["gsz"] - q["dwjz"]) / q["dwjz"] * 100, 2)
    return None


def _cur_price(q):
    """现价:优先 nav(收盘官方)回落 gsz(盘中估值)。"""
    if q is None:
        return None
    if q["nav"] is not None:
        return q["nav"]
    return q["gsz"] if q["gsz"] else None


def _shares_of(pos, q):
    """账本份额:流水口径直接给份额;手填口径按 hold_amount/dwjz 反推。"""
    if pos.get("shares") is not None:
        return pos["shares"]
    if pos.get("hold_amount") and q and q["dwjz"]:
        return pos["hold_amount"] / q["dwjz"]
    return None


def _build_overview(conn, user_id):
    summary = portfolio._compute_summary(conn, user_id)

    # 各持仓明细:名称 / 今日涨跌 / 今日盈亏 / 当前收益率 / 目标进度 / 止盈止损触发
    # 遍历「持仓 ∪ 流水」并集(与 summary 口径一致);目标/止盈线只存在于 holding,
    # 流水-only 基金的这些字段为 NULL(仍展示今日盈亏与收益率,只是无目标线)。
    rows = conn.execute(
        "SELECT x.fund_code, "
        "h.target_rate, h.target_price, h.stop_profit, h.stop_loss, "
        "h.trailing_stop_pct, h.peak_nav, "
        "q.dwjz, q.gsz, q.gszzl, q.nav, fl.name AS name "
        "FROM (SELECT fund_code FROM holding WHERE user_id=? "
        "      UNION SELECT fund_code FROM fund_transaction WHERE user_id=?) x "
        "LEFT JOIN holding h ON h.fund_code=x.fund_code AND h.user_id=? "
        "LEFT JOIN fund_quote q ON q.fund_code=x.fund_code "
        "LEFT JOIN fund_list fl ON fl.fund_code=x.fund_code",
        (user_id, user_id, user_id),
    ).fetchall()

    holdings = []
    goals = []
    alerts = []
    total_today_pl = 0.0
    has_today_pl = False

    for r in rows:
        code = r["fund_code"]
        name = r["name"] or code
        q = r  # 同一行里已含 quote 字段
        pos = resolve_position(conn, code, user_id)
        shares = _shares_of(pos, q)
        cur_price = _cur_price(q)
        cost = pos["cost_amount"]
        cur_value = position_market_value(
            pos, q["dwjz"], q["gsz"], q["nav"]
        )
        today_rate = _today_change_pct(q)

        # 今日盈亏:份额 × (gsz - dwjz)
        today_pl = None
        if shares is not None and q["gsz"] and q["dwjz"]:
            today_pl = round(shares * (q["gsz"] - q["dwjz"]), 2)
            total_today_pl += today_pl
            has_today_pl = True

        cur_return = None
        if cur_value is not None and cost:
            cur_return = round((cur_value / cost - 1) * 100, 2)

        holdings.append({
            "fund_code": code, "name": name,
            "today_rate": today_rate, "today_pl": today_pl,
            "current_return_pct": cur_return,
            "market_value": round(cur_value, 2) if cur_value is not None else None,
        })

        # ---- 离目标进度 ----
        goal = {
            "fund_code": code, "name": name,
            "current_return_pct": cur_return,
            "target_rate": r["target_rate"],
            "target_progress_pct": None,   # 距目标收益率的完成度
            "dist_to_stop_profit": None,   # 距止盈还差多少百分点
            "recovery_progress_pct": None, # 浮亏时的回本进度(市值/成本)
            "in_loss": bool(cur_return is not None and cur_return < 0),
        }
        if cur_return is not None:
            if r["target_rate"] and r["target_rate"] > 0:
                goal["target_progress_pct"] = round(
                    max(0.0, min(cur_return / r["target_rate"], 1.0)) * 100, 1
                )
            if r["stop_profit"] is not None:
                goal["dist_to_stop_profit"] = round(r["stop_profit"] - cur_return, 2)
            if cur_return < 0 and cur_value is not None and cost:
                goal["recovery_progress_pct"] = round(min(cur_value / cost, 1.0) * 100, 1)
        if any(goal[k] is not None for k in
               ("target_progress_pct", "dist_to_stop_profit", "recovery_progress_pct")):
            goals.append(goal)

        # ---- 今天要看:止盈/止损/移动止盈触发 ----
        if cur_return is not None:
            if r["stop_profit"] is not None and cur_return >= r["stop_profit"]:
                alerts.append({
                    "kind": "stop_profit", "fund_code": code, "name": name,
                    "severity": "good",
                    "message": "触及止盈线 %.2f%%(当前 %.2f%%)" % (r["stop_profit"], cur_return),
                })
            if r["stop_loss"] is not None and cur_return <= r["stop_loss"]:
                alerts.append({
                    "kind": "stop_loss", "fund_code": code, "name": name,
                    "severity": "bad",
                    "message": "跌破止损线 %.2f%%(当前 %.2f%%)" % (r["stop_loss"], cur_return),
                })
        # 移动止盈:现价从峰值回撤超阈值
        if (r["trailing_stop_pct"] is not None and r["peak_nav"] and cur_price is not None
                and r["peak_nav"] > 0 and cur_price <= r["peak_nav"] * (1 - r["trailing_stop_pct"] / 100)):
            alerts.append({
                "kind": "trailing_stop", "fund_code": code, "name": name,
                "severity": "bad",
                "message": "移动止盈触发:较峰值回撤超 %.1f%%" % r["trailing_stop_pct"],
            })

    # ---- 今天要看:定投到点(next_date <= 今天且 active) ----
    today = date.today().isoformat()
    dca_rows = conn.execute(
        "SELECT d.fund_code, d.per_amount, d.next_date, fl.name AS name "
        "FROM dca_plan d LEFT JOIN fund_list fl ON fl.fund_code=d.fund_code "
        "WHERE d.user_id=? AND d.active=1 AND d.next_date<=?",
        (user_id, today),
    ).fetchall()
    for d in dca_rows:
        alerts.append({
            "kind": "dca_due", "fund_code": d["fund_code"],
            "name": d["name"] or d["fund_code"], "severity": "info",
            "message": "定投扣款日:计划投入 ¥%.2f" % d["per_amount"],
        })

    # ---- 未读通知数(复用 notification 表) ----
    unread = conn.execute(
        "SELECT COUNT(*) AS c FROM notification WHERE user_id=? AND read_at IS NULL",
        (user_id,),
    ).fetchone()["c"]

    # 今日涨跌 mini 条:按今日涨跌排序(拖累/领涨一眼看)
    spark = sorted(
        [h for h in holdings if h["today_rate"] is not None],
        key=lambda x: x["today_rate"],
    )

    today_pl_pct = None
    base = summary.get("total_market_value") or 0
    if has_today_pl and base:
        # 以昨日市值近似 = 今市值 - 今日盈亏
        prev = base - total_today_pl
        if prev:
            today_pl_pct = round(total_today_pl / prev * 100, 2)

    return {
        "summary": summary,
        "today_pl": round(total_today_pl, 2) if has_today_pl else None,
        "today_pl_pct": today_pl_pct,
        "holdings": holdings,
        "spark": spark,
        "goals": goals,
        "alerts": alerts,
        "unread_notifications": unread,
        "as_of": today,
    }


def get_overview(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    conn = get_conn()
    try:
        return _build_overview(conn, ctx.user_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# AI 早晚报:点开才生成 + 当日进程内缓存(不盘中高频烧 token)
# 缓存 key = (user_id, 日期);同一天重复点只生成一次。进程重启即失效(可接受)。
# --------------------------------------------------------------------------- #
_BRIEFING_CACHE = {}  # (user_id, date_str) -> {"text","disclaimer","cached"}


def _briefing_prompt(ov):
    s = ov["summary"]
    lines = ["这是我的基金组合今日快照,请用一句话(40字内)点评今日表现与最该关注的一点,"
             "口吻平实、不做买卖建议:"]
    lines.append("总市值 %.2f 元,累计收益率 %s%%,今日盈亏 %s 元(%s%%)。" % (
        s.get("total_market_value") or 0,
        s.get("total_return_pct"), ov.get("today_pl"), ov.get("today_pl_pct"),
    ))
    spark = ov.get("spark") or []
    if spark:
        worst = spark[0]
        best = spark[-1]
        lines.append("今日领涨:%s %s%%;今日拖累:%s %s%%。" % (
            best["name"], best["today_rate"], worst["name"], worst["today_rate"]))
    if ov.get("alerts"):
        kinds = {a["kind"] for a in ov["alerts"]}
        lines.append("今日提醒类型:" + ",".join(sorted(kinds)) + "。")
    return "\n".join(lines)


def get_briefing(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    from backend.datasource import ai_engine
    if not ai_engine.is_configured():
        return (200, {"ok": False, "configured": False,
                      "text": "未配置 AI 密钥,早晚报暂不可用(不影响其他功能)。"})

    today = date.today().isoformat()
    key = (ctx.user_id, today)
    force = ctx.q("refresh") in ("1", "true", "yes")
    if not force and key in _BRIEFING_CACHE:
        out = dict(_BRIEFING_CACHE[key])
        out["cached"] = True
        return (200, out)

    conn = get_conn()
    try:
        ov = _build_overview(conn, ctx.user_id)
    finally:
        conn.close()
    if not ov.get("holdings"):
        return (200, {"ok": False, "configured": True,
                      "text": "还没有持仓,录入后即可生成组合早晚报。"})

    result = ai_engine.run_chat([{"role": "user", "content": _briefing_prompt(ov)}])
    out = {
        "ok": bool(result.get("ok")),
        "configured": True,
        "text": result.get("reply") or result.get("error") or "生成失败,请稍后重试。",
        "disclaimer": result.get("disclaimer"),
        "cached": False,
    }
    if out["ok"]:
        _BRIEFING_CACHE[key] = out
    return (200, out)


ROUTES = [
    ("GET", "/api/home/overview", get_overview),
    ("GET", "/api/home/briefing", get_briefing),
]
