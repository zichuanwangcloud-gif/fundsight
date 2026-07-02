# -*- coding: utf-8 -*-
"""盘中估值抓取 —— 数据源: 天天基金 fundgz JSONP 接口。

已于 2026-07-02 实测可用（编号 020608）。这是全项目唯一稳定可达的外部接口。
低频调用 + 写入 fund_quote 缓存，业务层只读缓存。
"""
import json
import re
import ssl
import urllib.request

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch_estimate(code):
    """拉单只基金的盘中估值。成功返回 dict，失败返回 None。"""
    url = f"https://fundgz.1234567.com.cn/js/{code}.js"
    try:
        req = urllib.request.Request(url, headers=_UA)
        raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8")
        m = re.search(r"jsonpgz\((.*)\)", raw)
        if not m:
            return None
        d = json.loads(m.group(1))
        return {
            "fund_code": d.get("fundcode"),
            "name": d.get("name"),
            "dwjz": _f(d.get("dwjz")),
            "gsz": _f(d.get("gsz")),
            "gszzl": _f(d.get("gszzl")),
            "gztime": d.get("gztime"),
        }
    except Exception as e:
        print(f"[fundgz] 拉取 {code} 失败: {type(e).__name__} {e}")
        return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def refresh_quotes(conn, codes):
    """批量刷新给定基金的估值并写入 fund_quote 缓存。返回成功数。"""
    ok = 0
    for code in codes:
        d = fetch_estimate(code)
        if not d:
            continue
        conn.execute(
            """INSERT INTO fund_quote(fund_code,name,dwjz,gsz,gszzl,gztime,updated_at)
               VALUES (:fund_code,:name,:dwjz,:gsz,:gszzl,:gztime,datetime('now','localtime'))
               ON CONFLICT(fund_code) DO UPDATE SET
                 name=excluded.name, dwjz=excluded.dwjz, gsz=excluded.gsz,
                 gszzl=excluded.gszzl, gztime=excluded.gztime, updated_at=excluded.updated_at""",
            d,
        )
        ok += 1
    conn.commit()
    return ok


if __name__ == "__main__":
    print(fetch_estimate("020608"))
