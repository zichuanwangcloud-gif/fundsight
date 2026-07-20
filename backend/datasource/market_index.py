# -*- coding: utf-8 -*-
"""大盘指数抓取 —— 数据源: 东方财富 push2 行情接口(P1a)。

抓 4 大核心指数:上证指数 / 深证成指 / 创业板指 / 沪深300。低频调用 + 写入
market_index 缓存,业务层只读缓存(与 fundgz 同构,守 CLAUDE.md「抓取层是唯一
对外接口」红线)。

⚠️ 该接口在开发沙箱可能受反爬限制;失败时不影响已有 market_index(上次快照照常可用)。
parse_indices 与 fetch_indices 分离,便于用离线报文做单元测试(不发真实网络)。
"""
import json
import ssl
import urllib.request

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

# push2 secid 格式: market.code(market 1=沪 / 0=深)。展示顺序即前端指数条顺序。
INDICES = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000300", "沪深300"),
]
# 表内主键存 f12(纯代码),前端按此顺序展示
DISPLAY_CODES = [secid.split(".", 1)[1] for secid, _ in INDICES]

_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_FIELDS = "f2,f3,f4,f12,f13,f14"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_url():
    secids = ",".join(secid for secid, _ in INDICES)
    return f"{_URL}?fltt=2&fields={_FIELDS}&secids={secids}"


def parse_indices(raw):
    """解析 push2 报文字符串 → [{code,name,price,change,change_pct}, ...]。

    健壮处理:data 为 null / diff 缺失 / 字段为 "-"(停牌或盘前)时 price 等为 None。
    解析失败(非 JSON)返回 []。
    """
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return []
    diff = ((d or {}).get("data") or {}).get("diff") or []
    out = []
    for it in diff:
        code = it.get("f12")
        if not code:
            continue
        out.append({
            "code": code,
            "name": it.get("f14"),
            "price": _f(it.get("f2")),
            "change": _f(it.get("f4")),
            "change_pct": _f(it.get("f3")),
        })
    return out


def fetch_indices():
    """拉 4 大指数最新行情。成功返回 list,失败返回 [](不抛,守优雅降级)。"""
    try:
        req = urllib.request.Request(_build_url(), headers=_UA)
        raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8")
        return parse_indices(raw)
    except Exception as e:  # noqa: BLE001 —— 抓取失败不影响服务
        print(f"[market_index] 拉取指数失败(不影响服务): {type(e).__name__} {e}")
        return []


def refresh_indices(conn, fetch=fetch_indices):
    """抓取并写入 market_index(ON CONFLICT 覆盖最新)。返回成功写入条数。

    fetch 可注入,便于单测喂离线样本。无数据(抓取失败)返回 0,不动已有快照。
    """
    rows = fetch()
    if not rows:
        return 0
    ok = 0
    for r in rows:
        if not r.get("code"):
            continue
        conn.execute(
            """INSERT INTO market_index(code,name,price,change,change_pct,updated_at)
               VALUES (:code,:name,:price,:change,:change_pct,datetime('now','localtime'))
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, price=excluded.price, change=excluded.change,
                 change_pct=excluded.change_pct, updated_at=excluded.updated_at""",
            r,
        )
        ok += 1
    conn.commit()
    return ok


if __name__ == "__main__":
    print(fetch_indices())
