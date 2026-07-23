# -*- coding: utf-8 -*-
"""AI 分析工具层 —— 唯一真源（进程内 tool-loop 与 MCP server 共用一份定义）。

设计约束（延续本项目铁律）：
- 零第三方依赖：只用标准库 + 现有 backend.models.db。
- 业务层只读 SQLite 缓存：工具只 SELECT 已有表；仅在某基金完全无缓存时
  触发一次低频按需抓取（与 fund_detail._ensure_cached 同款兜底），随后照常只读。
  绝不高频轮询、绝不在工具里直连外网行情接口。
- 优雅降级：任何异常都被兜底为结构化 {"error": ...}，绝不抛出拖垮服务/循环。

P1 落地 4 个「读本地数据」工具（get_fund_overview / get_fund_holdings /
get_fund_nav_trend / get_fund_peer_compare）。个股/赛道实时抓取（get_stock_info /
get_sector_info）属 P2，触及合规红线，单独隔离实现。
"""
import json

from backend.models.db import get_conn

# --------------------------------------------------------------------------- #
# 工具定义（JSON-Schema）—— tool-loop 与 MCP tools/list 都用这份
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        "name": "get_fund_overview",
        "description": (
            "获取一只基金的概况：名称、基金经理、规模、费率、当日盘中估算涨幅、"
            "近1月/近3月/今年来/成立以来累计收益率、同类百分位排名。"
            "分析基金近况时应首先调用此工具。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6 位基金代码，如 020608"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_fund_holdings",
        "description": (
            "获取一只基金最新一期的前十大重仓股（股票代码、名称、占净值比例、报告期）。"
            "分析基金投向、行业集中度、赛道暴露时调用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6 位基金代码"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_fund_nav_trend",
        "description": (
            "获取一只基金近一段时间的净值走势采样点与区间累计涨幅、区间最大回撤。"
            "分析近期表现、波动、回撤时调用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6 位基金代码"},
                "days": {
                    "type": "integer",
                    "description": "回看天数，默认 90，最大 1825",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_fund_peer_compare",
        "description": (
            "获取一只基金相对同类平均、沪深300 的累计收益率对比（最新值与区间差值）。"
            "判断基金跑赢/跑输同类与大盘时调用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6 位基金代码"},
            },
            "required": ["code"],
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}

MAX_TREND_POINTS = 16   # 走势采样点上限，控制回给模型的 token 量
MAX_TREND_DAYS = 1825


# --------------------------------------------------------------------------- #
# 首访兜底：某基金完全无缓存时低频抓一次（与 fund_detail._ensure_cached 同款）
# --------------------------------------------------------------------------- #
def _ensure_cached(conn, code):
    """profile / 历史 / 重仓股 / 同类对比 缓存全缺时，触发一次低频按需抓取。单次不重试。"""
    try:
        from backend.datasource.fund_profile import refresh_profile
        from backend.datasource.nav_history import refresh_nav_history
        from backend.datasource.fund_holdings import refresh_holdings
        from backend.datasource.fund_compare import refresh_compare
        refresh_profile(conn, [code])
        refresh_nav_history(conn, [code])
        refresh_holdings(conn, [code])
        refresh_compare(conn, [code])
    except Exception:  # noqa: BLE001 —— 抓取失败静默降级，工具照常只读返回已有数据
        pass


def _profile_row(conn, code):
    row = conn.execute(
        "SELECT fund_code,name,manager,scale,rate,syl_1n,syl_3y,syl_6y,syl_1y,"
        "asset_alloc_stock,asset_alloc_bond,asset_alloc_cash,"
        "peer_percentile,peer_rank,peer_total,updated_at "
        "FROM fund_profile WHERE fund_code=?",
        (code,),
    ).fetchone()
    return dict(row) if row else None


def _quote_row(conn, code):
    row = conn.execute(
        "SELECT name,dwjz,gsz,gszzl,gztime,nav,nav_date FROM fund_quote WHERE fund_code=?",
        (code,),
    ).fetchone()
    return dict(row) if row else None


def _fund_name(conn, code):
    row = conn.execute(
        "SELECT name FROM fund_list WHERE fund_code=?", (code,)
    ).fetchone()
    return row["name"] if row else None


def _nav_on_or_before(conn, code, target_date):
    row = conn.execute(
        "SELECT COALESCE(nav_adj, nav) AS nav FROM fund_nav_history WHERE fund_code=? "
        "AND nav_date <= ? AND nav IS NOT NULL ORDER BY nav_date DESC LIMIT 1",
        (code, target_date),
    ).fetchone()
    return row["nav"] if row else None


def _periods(conn, code):
    """近1月/近3月/今年来/成立以来累计涨幅 %（复权口径优先）。"""
    from datetime import date, timedelta
    latest = conn.execute(
        "SELECT COALESCE(nav_adj, nav) AS nav, nav_date FROM fund_nav_history "
        "WHERE fund_code=? AND nav IS NOT NULL ORDER BY nav_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if not latest:
        return None
    latest_nav = latest["nav"]
    today = date.today()

    def rate(target):
        start = _nav_on_or_before(conn, code, target)
        if not start:
            return None
        return round((latest_nav - start) / start * 100, 2)

    earliest = conn.execute(
        "SELECT COALESCE(nav_adj, nav) AS nav FROM fund_nav_history "
        "WHERE fund_code=? AND nav IS NOT NULL ORDER BY nav_date ASC LIMIT 1",
        (code,),
    ).fetchone()
    max_ret = None
    if earliest and earliest["nav"]:
        max_ret = round((latest_nav - earliest["nav"]) / earliest["nav"] * 100, 2)
    return {
        "latest_nav_date": latest["nav_date"],
        "r_1m": rate((today - timedelta(days=30)).strftime("%Y-%m-%d")),
        "r_3m": rate((today - timedelta(days=90)).strftime("%Y-%m-%d")),
        "r_ytd": rate(f"{today.year}-01-01"),
        "r_since": max_ret,
    }


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #
def _tool_fund_overview(conn, code):
    profile = _profile_row(conn, code)
    quote = _quote_row(conn, code)
    if profile is None and quote is None:
        _ensure_cached(conn, code)
        profile = _profile_row(conn, code)
        quote = _quote_row(conn, code)
    name = (profile or {}).get("name") or (quote or {}).get("name") or _fund_name(conn, code)
    if profile is None and quote is None:
        return {"code": code, "note": "本地暂无该基金缓存数据"}
    out = {"code": code, "name": name}
    if quote:
        out["intraday"] = {
            "gszzl_pct": quote.get("gszzl"),   # 当日盘中估算涨跌幅 %
            "gsz": quote.get("gsz"),
            "gztime": quote.get("gztime"),
            "dwjz": quote.get("dwjz"),
            "nav": quote.get("nav"),
            "nav_date": quote.get("nav_date"),
        }
    if profile:
        out["profile"] = {
            "manager": profile.get("manager"),
            "scale_yi": profile.get("scale"),
            "rate": profile.get("rate"),
            "return_1m_pct": profile.get("syl_1y"),   # pingzhongdata 字段：近1月
            "return_1y_pct": profile.get("syl_1n"),   # 近1年
            "return_3y_pct": profile.get("syl_3y"),
            "return_since_pct": profile.get("syl_6y"),
            "asset_stock_pct": profile.get("asset_alloc_stock"),
            "asset_bond_pct": profile.get("asset_alloc_bond"),
            "asset_cash_pct": profile.get("asset_alloc_cash"),
            "peer_percentile": profile.get("peer_percentile"),
            "peer_rank": profile.get("peer_rank"),
            "peer_total": profile.get("peer_total"),
        }
    periods = _periods(conn, code)
    if periods:
        out["periods"] = periods
    return out


def _tool_fund_holdings(conn, code):
    rows = conn.execute(
        "SELECT rank,stock_code,stock_name,weight,report_period "
        "FROM fund_holding_stock WHERE fund_code=? ORDER BY rank",
        (code,),
    ).fetchall()
    if not rows and _profile_row(conn, code) is None:
        _ensure_cached(conn, code)
        rows = conn.execute(
            "SELECT rank,stock_code,stock_name,weight,report_period "
            "FROM fund_holding_stock WHERE fund_code=? ORDER BY rank",
            (code,),
        ).fetchall()
    holdings = [
        {
            "rank": r["rank"],
            "stock_code": r["stock_code"],
            "stock_name": r["stock_name"],
            "weight_pct": r["weight"],
        }
        for r in rows
    ]
    period = rows[0]["report_period"] if rows else None
    return {"code": code, "report_period": period, "holdings": holdings}


def _tool_fund_nav_trend(conn, code, days):
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 90
    days = max(7, min(days, MAX_TREND_DAYS))
    rows = conn.execute(
        "SELECT nav_date, COALESCE(nav_adj, nav) AS nav FROM fund_nav_history "
        "WHERE fund_code=? AND nav IS NOT NULL ORDER BY nav_date DESC LIMIT ?",
        (code, days),
    ).fetchall()
    if not rows and _profile_row(conn, code) is None:
        _ensure_cached(conn, code)
        rows = conn.execute(
            "SELECT nav_date, COALESCE(nav_adj, nav) AS nav FROM fund_nav_history "
            "WHERE fund_code=? AND nav IS NOT NULL ORDER BY nav_date DESC LIMIT ?",
            (code, days),
        ).fetchall()
    series = list(reversed([{"date": r["nav_date"], "nav": r["nav"]} for r in rows]))
    if not series:
        return {"code": code, "days": days, "note": "本地暂无该基金历史净值"}
    start_nav = series[0]["nav"]
    end_nav = series[-1]["nav"]
    cum = round((end_nav - start_nav) / start_nav * 100, 2) if start_nav else None
    # 区间最大回撤 %
    peak = series[0]["nav"]
    max_dd = 0.0
    for p in series:
        if p["nav"] > peak:
            peak = p["nav"]
        if peak:
            dd = (p["nav"] - peak) / peak * 100
            if dd < max_dd:
                max_dd = dd
    # 均匀降采样，控制点数
    if len(series) > MAX_TREND_POINTS:
        step = len(series) / MAX_TREND_POINTS
        sampled = [series[int(i * step)] for i in range(MAX_TREND_POINTS)]
        if sampled[-1] != series[-1]:
            sampled.append(series[-1])
    else:
        sampled = series
    return {
        "code": code,
        "days": days,
        "start": {"date": series[0]["date"], "nav": start_nav},
        "end": {"date": series[-1]["date"], "nav": end_nav},
        "cumulative_return_pct": cum,
        "max_drawdown_pct": round(max_dd, 2),
        "sample_points": sampled,
    }


def _tool_fund_peer_compare(conn, code):
    rows = conn.execute(
        "SELECT series_key, trade_date, value FROM fund_compare_trend "
        "WHERE fund_code=? ORDER BY series_key, trade_date",
        (code,),
    ).fetchall()
    if not rows and _profile_row(conn, code) is None:
        _ensure_cached(conn, code)
        rows = conn.execute(
            "SELECT series_key, trade_date, value FROM fund_compare_trend "
            "WHERE fund_code=? ORDER BY series_key, trade_date",
            (code,),
        ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["series_key"], []).append(r["value"])
    labels = {"self": "本基金", "peer": "同类平均", "hs300": "沪深300"}
    latest = {}
    for key, vals in grouped.items():
        if vals:
            latest[key] = round(vals[-1], 2)
    if not latest:
        return {"code": code, "note": "本地暂无同类对比数据"}
    out = {
        "code": code,
        "unit": "累计收益率 %",
        "latest": {labels.get(k, k): v for k, v in latest.items()},
    }
    if "self" in latest and "peer" in latest:
        out["self_vs_peer_pct"] = round(latest["self"] - latest["peer"], 2)
    if "self" in latest and "hs300" in latest:
        out["self_vs_hs300_pct"] = round(latest["self"] - latest["hs300"], 2)
    return out


_DISPATCH = {
    "get_fund_overview": lambda conn, a: _tool_fund_overview(conn, a.get("code", "")),
    "get_fund_holdings": lambda conn, a: _tool_fund_holdings(conn, a.get("code", "")),
    "get_fund_nav_trend": lambda conn, a: _tool_fund_nav_trend(
        conn, a.get("code", ""), a.get("days", 90)),
    "get_fund_peer_compare": lambda conn, a: _tool_fund_peer_compare(conn, a.get("code", "")),
}


def call_tool(name, arguments):
    """执行一个工具，返回 JSON 可序列化 dict。未知工具/异常均结构化兜底，绝不抛出。"""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"未知工具: {name}"}
    args = arguments if isinstance(arguments, dict) else {}
    code = str(args.get("code", "")).strip()
    if not code:  # P1 全部工具都以 6 位基金代码为必填参数
        return {"error": "缺少必填参数 code（6 位基金代码）"}
    conn = get_conn()
    try:
        return fn(conn, {**args, "code": code})
    except Exception as e:  # noqa: BLE001 —— 优雅降级，绝不让单个工具失败拖垮循环
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


def call_tool_text(name, arguments):
    """call_tool 的文本封装：返回紧凑 JSON 字符串，供回填给模型 / MCP text content。"""
    return json.dumps(call_tool(name, arguments), ensure_ascii=False)
