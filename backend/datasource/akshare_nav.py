# -*- coding: utf-8 -*-
"""收盘官方净值抓取 —— 数据源: 天天基金 pingzhongdata 净值走势接口。

优先使用纯标准库方案，从
https://fund.eastmoney.com/pingzhongdata/{code}.js 中解析
`Data_netWorthTrend`（单位净值走势）序列，取最后一个点作为最新收盘官方净值。
若该接口失败（反爬 418 / 超时 / 解析失败），再尝试 akshare（若未安装则跳过）。

⚠️ 注意: 开发沙箱环境可能被反爬拦截或无公网出口，实测可能拉取失败。
失败时一律返回 None，不抛异常，业务层应容忍取不到官方净值的情况。

用法: python3 backend/datasource/akshare_nav.py
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


def _fetch_via_pingzhongdata(code):
    """从东财 pingzhongdata 接口解析最新官方净值。失败返回 None。"""
    url = _PINGZHONG_URL.format(code=code)
    req = urllib.request.Request(url, headers=_UA)
    raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode(
        "utf-8", errors="ignore"
    )

    name_m = re.search(r'fS_name\s*=\s*"([^"]*)"', raw)
    name = name_m.group(1) if name_m else None

    trend_m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", raw, re.S)
    if not trend_m:
        return None
    trend = json.loads(trend_m.group(1))
    if not trend:
        return None
    last = trend[-1]
    nav = last.get("y")
    ts_ms = last.get("x")
    if nav is None or ts_ms is None:
        return None
    nav_date = _cst_date(ts_ms)
    # 上一交易日净值(倒数第二点),供"收盘真实盈亏"作基准 —— 不依赖 fundgz 的 dwjz
    nav_prev = _f(trend[-2].get("y")) if len(trend) >= 2 else None
    return {
        "fund_code": code,
        "name": name,
        "nav": _f(nav),
        "nav_date": nav_date,
        "nav_prev": nav_prev,
    }


def _fetch_via_akshare(code):
    """兜底方案: 使用 akshare 拉取基金净值历史。未安装或失败返回 None。"""
    try:
        import akshare as ak
    except ImportError:
        print(f"[akshare_nav] akshare 未安装，跳过兜底方案（{code}）")
        return None
    try:
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        if df is None or df.empty:
            return None
        last_row = df.iloc[-1]
        nav_date = str(last_row.get("净值日期"))
        nav = _f(last_row.get("单位净值"))
        if nav is None:
            return None
        nav_prev = _f(df.iloc[-2].get("单位净值")) if len(df) >= 2 else None
        return {
            "fund_code": code,
            "name": None,
            "nav": nav,
            "nav_date": nav_date,
            "nav_prev": nav_prev,
        }
    except Exception as e:
        print(f"[akshare_nav] akshare 拉取 {code} 失败: {type(e).__name__} {e}")
        return None


def fetch_nav(code):
    """拉单只基金的收盘官方净值。成功返回 dict，失败返回 None。"""
    try:
        d = _fetch_via_pingzhongdata(code)
        if d:
            return d
    except Exception as e:
        print(f"[akshare_nav] pingzhongdata 拉取 {code} 失败: {type(e).__name__} {e}")

    return _fetch_via_akshare(code)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cst_date(ts_ms):
    """东财 pingzhongdata 的净值点时间戳是「北京时间当日零点」的毫秒值。

    直接 utcfromtimestamp 会按 UTC 解读,把北京 00:00(=UTC 前一日 16:00)算成
    前一天,导致净值日期整体早一天(如把 7-24 标成 7-23)。这里 +8 小时按东八区
    还原真实交易日,消除 off-by-one。
    """
    return datetime.datetime.utcfromtimestamp(ts_ms / 1000 + 8 * 3600).strftime("%Y-%m-%d")


def refresh_nav(conn, codes):
    """批量刷新给定基金的收盘官方净值并写入 fund_quote。返回成功数。

    只写 nav/nav_date/nav_prev/name(官方口径),绝不触碰 dwjz/gsz(fundgz 盘中估值口径)
    —— 两套字段分离,fundgz 不可用时官方口径仍可独立成算。nav_prev 缺失时用 COALESCE
    保留旧值,不清空。
    """
    ok = 0
    for code in codes:
        d = fetch_nav(code)
        if not d:
            continue
        d.setdefault("nav_prev", None)  # 兜底:旧数据源/mock 可能不带该键
        conn.execute(
            """INSERT INTO fund_quote(fund_code,name,nav,nav_date,nav_prev,updated_at)
               VALUES (:fund_code,:name,:nav,:nav_date,:nav_prev,datetime('now','localtime'))
               ON CONFLICT(fund_code) DO UPDATE SET
                 name=COALESCE(excluded.name, fund_quote.name),
                 nav=excluded.nav, nav_date=excluded.nav_date,
                 nav_prev=COALESCE(excluded.nav_prev, fund_quote.nav_prev),
                 updated_at=excluded.updated_at""",
            d,
        )
        ok += 1
    conn.commit()
    return ok


if __name__ == "__main__":
    print(fetch_nav("020608"))
