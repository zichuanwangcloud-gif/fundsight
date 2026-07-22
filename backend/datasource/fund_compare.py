# -*- coding: utf-8 -*-
"""同类对比走势抓取 —— 数据源: 天天基金 pingzhongdata Data_grandTotal(P3)。

从 https://fund.eastmoney.com/pingzhongdata/{code}.js 解析 Data_grandTotal:
  [{name:"<本基金名>", data:[[ts_ms, 累计收益率%], ...]},
   {name:"同类平均",   data:[[ts_ms, val], ...]},
   {name:"沪深300",    data:[[ts_ms, val], ...]}]
归一为 self / peer / hs300 三序列,写 fund_compare_trend 缓存,业务层只读。

对标天天基金详情页「本基金 vs 同类 vs 沪深300」累计收益率叠加图。parse_compare 与
fetch_compare 分离,便于离线报文单测。失败优雅降级返回 []。
"""
import json
import re
import ssl
import urllib.request
from datetime import datetime, timedelta

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/",
}

_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
_RE_GRANDTOTAL = re.compile(r"var\s+Data_grandTotal\s*=\s*(\[.*?\])\s*;", re.S)

# 序列展示名(前端图例用)
SERIES_LABELS = {"self": "本基金", "peer": "同类平均", "hs300": "沪深300"}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ms_to_date(ms):
    """pingzhong 毫秒时间戳 → 北京日期字符串。失败返回 None。"""
    try:
        return (datetime.utcfromtimestamp(ms / 1000) + timedelta(hours=8)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _series_key(name):
    """按名称归一为 self / peer / hs300。名称含'同类'→peer,含'沪深300'→hs300,否则 self。"""
    nm = name or ""
    if "同类" in nm:
        return "peer"
    if "沪深300" in nm or "300" in nm:
        return "hs300"
    return "self"


def parse_compare(raw):
    """解析 Data_grandTotal → [{key, name, points:[{date,value}]}, ...]。

    无该变量 / 解析失败返回 []。同名序列去重(保留首个)。
    """
    m = _RE_GRANDTOTAL.search(raw or "")
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except (ValueError, TypeError):
        return []
    out, seen = [], set()
    for s in arr:
        if not isinstance(s, dict):
            continue
        key = _series_key(s.get("name"))
        if key in seen:
            continue
        pts = []
        for item in (s.get("data") or []):
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            d = _ms_to_date(item[0])
            v = _f(item[1])
            if d is not None and v is not None:
                pts.append({"date": d, "value": v})
        if pts:
            seen.add(key)
            out.append({"key": key, "name": SERIES_LABELS[key], "points": pts})
    return out


def fetch_compare(code):
    """拉单只基金的同类对比走势。成功返回 list,失败返回 [](不抛,优雅降级)。"""
    try:
        req = urllib.request.Request(_URL.format(code=code), headers=_UA)
        raw = urllib.request.urlopen(req, timeout=15, context=_CTX).read().decode("utf-8", "ignore")
        return parse_compare(raw)
    except Exception as e:  # noqa: BLE001 —— 抓取失败不影响服务
        print(f"[fund_compare] 拉取 {code} 对比走势失败(不影响服务): {type(e).__name__} {e}")
        return []


def refresh_compare(conn, codes, fetch=fetch_compare):
    """批量抓取同类对比走势,每只基金「先删后插」。返回有数据的基金数。

    fetch(code) 可注入,便于单测。某基金抓取失败(空)则跳过、保留其已有序列。
    """
    ok = 0
    for code in codes:
        series = fetch(code)
        if not series:
            continue
        conn.execute("DELETE FROM fund_compare_trend WHERE fund_code=?", (code,))
        for s in series:
            for p in s["points"]:
                conn.execute(
                    "INSERT OR REPLACE INTO fund_compare_trend"
                    "(fund_code,series_key,trade_date,value,updated_at) "
                    "VALUES(?,?,?,?,datetime('now','localtime'))",
                    (code, s["key"], p["date"], p["value"]),
                )
        ok += 1
        conn.commit()
    return ok


if __name__ == "__main__":
    for s in fetch_compare("110022"):
        print(s["key"], s["name"], len(s["points"]), "末点", s["points"][-1] if s["points"] else None)
