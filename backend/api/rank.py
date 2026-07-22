# -*- coding: utf-8 -*-
"""基金排行榜 API（P1b）—— 只读 fund_rank 缓存,绝不触发抓取。

接口:
  GET /api/rank/meta            类目 + 区间清单(前端渲染 Tab 用)
  GET /api/rank?cat=&period=    某(大类,区间)榜单,按 rank 升序返回 topN

cat 缺省 all,period 缺省 1y;非法值回退到缺省。榜单由 scheduler 后台日更写入,
缓存为空时返回空列表(前端优雅降级,提示"榜单生成中")。
"""
from backend.datasource.fund_rank import CATEGORIES, PERIODS
from backend.models.db import get_conn

_VALID_CATS = {k for k, _ in CATEGORIES}
_VALID_PERIODS = {k for k, _, _ in PERIODS}
_DEFAULT_CAT = "all"
_DEFAULT_PERIOD = "1y"


def rank_meta_handler(ctx):
    """GET /api/rank/meta —— 类目 + 区间清单。"""
    return {
        "categories": [{"key": k, "label": l} for k, l in CATEGORIES],
        "periods": [{"key": k, "label": l} for k, l, _ in PERIODS],
        "default_cat": _DEFAULT_CAT,
        "default_period": _DEFAULT_PERIOD,
    }


def rank_handler(ctx):
    """GET /api/rank?cat=&period= —— 榜单只读。非法参数回退缺省。"""
    cat = (ctx.q("cat", _DEFAULT_CAT) or _DEFAULT_CAT).strip()
    period = (ctx.q("period", _DEFAULT_PERIOD) or _DEFAULT_PERIOD).strip()
    if cat not in _VALID_CATS:
        cat = _DEFAULT_CAT
    if period not in _VALID_PERIODS:
        period = _DEFAULT_PERIOD
    conn = get_conn()
    rows = conn.execute(
        "SELECT rank,fund_code,name,nav_date,nav,r_1m,r_3m,r_6m,r_1y,r_ytd "
        "FROM fund_rank WHERE period=? AND category=? ORDER BY rank",
        (period, cat),
    ).fetchall()
    updated = conn.execute(
        "SELECT MAX(updated_at) FROM fund_rank WHERE period=? AND category=?",
        (period, cat),
    ).fetchone()[0]
    conn.close()
    return {
        "cat": cat,
        "period": period,
        "items": [dict(r) for r in rows],
        "updated_at": updated,
    }


ROUTES = [
    ("GET", "/api/rank/meta", rank_meta_handler),
    ("GET", "/api/rank", rank_handler),
]
