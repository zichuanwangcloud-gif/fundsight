# -*- coding: utf-8 -*-
"""线路 A —— 基金市场列表 + 分类 tab。

只读 fund_list 缓存,绝不触发外部抓取(抓取层是唯一对外接口,详见 CLAUDE.md)。

接口:
  GET /api/categories        8 大类(混合/指数/债券/股票/货币/FOF/QDII/Reits)+ 每类基金数量
  GET /api/market?cat=&q=&page=&size=   分页列表,cat 大类过滤 + q 名称/代码/拼音过滤

fund_type 是数据源给出的 33 个细类字符串(如「混合型」「指数型-股票」「QDII-指数」等),
用关键字 LIKE 前缀/包含聚合到 8 大类。聚合优先级(避免一个细类同时命中多个关键字时
被重复计数或分到错误大类,如「QDII-指数」应归 QDII 而非「指数」、「债券指数」应归
「债券」而非「指数」):
  QDII > FOF > Reits > 货币 > 债券 > 指数(含 ETF) > 股票 > 混合
不命中任何关键字的归为「其他」,不计入 8 大类展示,但仍可被 /api/market 检索到
(cat 为空时正常列出)。
"""
from backend.api._router import Ctx  # noqa: F401  (类型提示用途,保持路由约定一致)
from backend.models.db import get_conn

# 分类展示顺序(前端 Tab 顺序)
DISPLAY_ORDER = ["混合", "指数", "债券", "股票", "货币", "FOF", "QDII", "Reits"]

# 归类优先级(自上而下第一个命中的关键字组决定归属;详见模块 docstring)
CLASSIFY_PRIORITY = [
    ("QDII", ["QDII"]),
    ("FOF", ["FOF"]),
    ("Reits", ["REIT"]),
    ("货币", ["货币"]),
    ("债券", ["债券"]),
    ("指数", ["指数", "ETF"]),
    ("股票", ["股票"]),
    ("混合", ["混合"]),
]

DEFAULT_SIZE = 20


def _category_case_sql():
    """构造 SQL CASE 表达式(按 CLASSIFY_PRIORITY 顺序),复用于聚合与过滤。

    返回 (sql_fragment, params) —— params 按 sql 中 ? 出现顺序排列。
    """
    whens = []
    params = []
    for cat, keywords in CLASSIFY_PRIORITY:
        conds = " OR ".join("fund_type LIKE ?" for _ in keywords)
        whens.append(f"WHEN {conds} THEN ?")
        params.extend(f"%{kw}%" for kw in keywords)
        params.append(cat)
    sql = "CASE " + " ".join(whens) + " ELSE '其他' END"
    return sql, params


def _int_arg(raw, default, minimum=1):
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= minimum else default


def categories_handler(ctx):
    """GET /api/categories —— 8 大类 + 每类基金数量。只读缓存。"""
    case_sql, params = _category_case_sql()
    conn = get_conn()
    rows = conn.execute(
        f"SELECT {case_sql} AS cat, COUNT(*) AS cnt FROM fund_list GROUP BY cat",
        params,
    ).fetchall()
    conn.close()
    counts = {r["cat"]: r["cnt"] for r in rows}
    return [{"cat": c, "count": counts.get(c, 0)} for c in DISPLAY_ORDER]


def market_handler(ctx):
    """GET /api/market?cat=&q=&page=&size= —— 分页列表。只读缓存,绝不抓取。"""
    cat = ctx.q("cat", "").strip()
    q = ctx.q("q", "").strip()
    page = _int_arg(ctx.q("page", ""), 1)
    size = _int_arg(ctx.q("size", ""), DEFAULT_SIZE)

    where = []
    params = []
    if cat:
        case_sql, case_params = _category_case_sql()
        where.append(f"({case_sql}) = ?")
        params.extend(case_params)
        params.append(cat)
    if q:
        like = f"%{q}%"
        where.append("(fund_code LIKE ? OR name LIKE ? OR pinyin LIKE ?)")
        params.extend([like, like, like])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_conn()
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM fund_list {where_sql}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT fund_code,name,fund_type FROM fund_list {where_sql} "
        "ORDER BY fund_code LIMIT ? OFFSET ?",
        params + [size, (page - 1) * size],
    ).fetchall()
    conn.close()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "size": size,
    }


ROUTES = [
    ("GET", "/api/categories", categories_handler),
    ("GET", "/api/market", market_handler),
]
