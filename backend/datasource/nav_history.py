# -*- coding: utf-8 -*-
"""历史净值序列抓取 —— 数据源: 天天基金 pingzhongdata 接口。

从 https://fund.eastmoney.com/pingzhongdata/{code}.js 解析:
  - `Data_netWorthTrend`  单位净值走势(分红日会断崖跳跌) + equityReturn 当日涨跌幅 %
  - `Data_ACWorthTrend`   累计净值走势(后复权,分红日不跳变)——PRD-02 复权口径

写入 fund_nav_history 四列:
  nav / equity_return         单位净值口径(原值,分红日假大跌)
  nav_adj / equity_return_adj 累计净值口径(复权,消除分红假大跌)

收益/回撤/波动率等计算一律读 nav_adj(缺失回落 nav),保证跨分红期不失真。
抓取失败一律返回 None,业务层容忍缺历史。

用法: python3 backend/datasource/nav_history.py
"""
import datetime
import json
import re
import ssl
import sys
import urllib.request

sys.path.insert(0, __file__.rsplit("/backend/", 1)[0])
from backend.models.db import get_conn  # noqa: E402

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/",
    "Accept": "*/*",
}

_PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_acc_map(raw):
    """解析 Data_ACWorthTrend 累计净值序列 → {date_str: acc_nav}。报文无该字段返回 {}。"""
    m = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", raw, re.S)
    if not m:
        return {}
    try:
        acc = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    if not isinstance(acc, list):
        return {}
    out = {}
    for pt in acc:
        # 天天基金 pingzhongdata 里 Data_ACWorthTrend 是 [[ts,y],...] 二元数组,
        # 而 Data_netWorthTrend 才是 [{x,y,equityReturn},...] 字典数组。
        # 这里两种形态都兼容,避免对 list 调 .get() 报 'list' object has no attribute 'get'。
        if isinstance(pt, dict):
            ts_ms, y = pt.get("x"), pt.get("y")
        elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
            ts_ms, y = pt[0], pt[1]
        else:
            continue
        if ts_ms is None or y is None:
            continue
        d = datetime.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
        out[d] = _f(y)
    return out


def fetch_nav_history(code):
    """拉取单只基金的完整历史净值序列。

    返回 [(date_str 'YYYY-MM-DD', unit_nav, acc_nav|None, equity_return|None), ...],
    失败返回 None。第二项为单位净值,第三项为累计净值(复权,报文无 ACWorthTrend 时
    为 None),第四项 equity_return 取自报文 `equityReturn`(单位口径当日涨跌幅 %)。
    旧调用方只解包前两项 (date, nav) 时不受影响——额外项对索引 [0]/[1] 访问无影响。
    """
    url = _PINGZHONG_URL.format(code=code)
    try:
        req = urllib.request.Request(url, headers=_UA)
        raw = urllib.request.urlopen(req, timeout=15, context=_CTX).read().decode(
            "utf-8", errors="ignore"
        )
    except Exception as e:
        print(f"[nav_history] 拉取 {code} 失败: {type(e).__name__} {e}")
        return None

    m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", raw, re.S)
    if not m:
        return None
    try:
        trend = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not trend:
        return None

    acc_map = _parse_acc_map(raw)
    series = []
    for pt in trend:
        ts_ms, nav = pt.get("x"), pt.get("y")
        if ts_ms is None or nav is None:
            continue
        date = datetime.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
        series.append((date, _f(nav), acc_map.get(date), _f(pt.get("equityReturn"))))
    return series or None


def _g(row, i):
    """安全取元组第 i 项,兼容旧调用方传入的短元组(date,nav)二元组。"""
    return row[i] if len(row) > i else None


def _compute_adj_return(series):
    """基于累计净值算复权口径当日涨跌幅 %,返回与 series 等长的 list,与每行对齐。

    累计净值缺失(早期无 ACWorthTrend 或该日无点)时,回落到报文 equityReturn(单位口径)。
    series 升序遍历,维护前一日的累计净值 prev_acc。
    """
    out = []
    prev_acc = None
    for row in series:
        acc = _g(row, 2)
        eq = _g(row, 3)
        if acc is not None and prev_acc is not None and prev_acc != 0:
            adj = round((acc / prev_acc - 1) * 100, 4)
        else:
            adj = eq  # 回落单位口径(数据缺失日)
        out.append(adj)
        if acc is not None:
            prev_acc = acc
    return out


def refresh_nav_history(conn, codes):
    """批量拉取并写入 fund_nav_history(含复权列)。主键 (fund_code,nav_date) 幂等去重。

    写入 nav/equity_return(单位口径)与 nav_adj/equity_return_adj(累计复权口径)。
    兼容旧调用方 mock 传入的短元组(date,nav)二元组:缺项按 None 写入。
    返回成功写入的基金数(非点数)。
    """
    ok = 0
    for code in codes:
        series = fetch_nav_history(code)
        if not series:
            continue
        adj_returns = _compute_adj_return(series)
        rows = []
        for i, row in enumerate(series):
            rows.append((
                code,
                _g(row, 0),                       # nav_date
                _g(row, 1),                       # nav(单位净值)
                _g(row, 3),                        # equity_return(单位口径)
                _g(row, 2),                       # nav_adj(累计净值,复权)
                adj_returns[i],                   # equity_return_adj(复权口径)
            ))
        conn.executemany(
            "INSERT INTO fund_nav_history("
            "fund_code,nav_date,nav,equity_return,nav_adj,equity_return_adj) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(fund_code,nav_date) DO UPDATE SET "
            "nav=excluded.nav, equity_return=excluded.equity_return, "
            "nav_adj=excluded.nav_adj, equity_return_adj=excluded.equity_return_adj",
            rows,
        )
        ok += 1
    conn.commit()
    return ok


if __name__ == "__main__":
    s = fetch_nav_history("020608")
    print(f"拉到 {len(s) if s else 0} 个净值点")
