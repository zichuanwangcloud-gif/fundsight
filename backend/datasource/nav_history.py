# -*- coding: utf-8 -*-
"""历史净值序列抓取 —— 数据源: 天天基金 pingzhongdata 接口。

从 https://fund.eastmoney.com/pingzhongdata/{code}.js 解析
`Data_netWorthTrend`(单位净值走势)完整序列,用于持仓卡片走势图。

与 akshare_nav.py 同源(同一份报文),但那里只取最后一点当最新净值,
这里保留整条序列。抓取失败一律返回 None,业务层容忍缺历史。

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


def fetch_nav_history(code):
    """拉取单只基金的完整历史净值序列。

    返回 [(date_str 'YYYY-MM-DD', nav float, equity_return float|None), ...],
    失败返回 None。第三项 equity_return 取自报文的 `equityReturn`(当日涨跌幅 %),
    用于详情页涨跌柱;旧调用方只解包前两项(date, nav)时不受影响,因为额外一项
    对索引 [0]/[1] 访问无影响。
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

    series = []
    for pt in trend:
        ts_ms, nav = pt.get("x"), pt.get("y")
        if ts_ms is None or nav is None:
            continue
        date = datetime.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
        series.append((date, _f(nav), _f(pt.get("equityReturn"))))
    return series or None


def refresh_nav_history(conn, codes):
    """批量拉取并写入 fund_nav_history。主键 (fund_code,nav_date) 幂等去重。

    兼容旧调用方传入的 (date, nav) 二元组(equity_return 按 None 写入)。
    返回成功写入的基金数(非点数)。
    """
    ok = 0
    for code in codes:
        series = fetch_nav_history(code)
        if not series:
            continue
        rows = [
            (code, row[0], row[1], row[2] if len(row) > 2 else None)
            for row in series
        ]
        conn.executemany(
            "INSERT INTO fund_nav_history(fund_code,nav_date,nav,equity_return) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(fund_code,nav_date) DO UPDATE SET "
            "nav=excluded.nav, equity_return=excluded.equity_return",
            rows,
        )
        ok += 1
    conn.commit()
    return ok


if __name__ == "__main__":
    s = fetch_nav_history("020608")
    print(f"拉到 {len(s) if s else 0} 个净值点")
