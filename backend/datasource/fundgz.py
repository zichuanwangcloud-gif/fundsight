# -*- coding: utf-8 -*-
"""盘中估值抓取 —— 数据源: 天天基金 fundgz JSONP 接口。

已于 2026-07-02 实测可用（编号 020608）。这是全项目唯一稳定可达的外部接口。
低频调用 + 写入 fund_quote 缓存，业务层只读缓存。
"""
import json
import re
import ssl
import urllib.request
from datetime import datetime, time as _time

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


# A 股交易时段(本地时间)。非交易时段 fundgz 返回的是上一交易日定格值,
# 采样无意义且浪费请求 —— scheduler.start_quote_refresh 据此门控跳过抓取。
_MARKET_OPEN = (_time(9, 30), _time(15, 0))


def is_market_open(now=None):
    """当前是否在 A 股交易时段:周一至周五 09:30–15:00(本地时间)。"""
    n = now or datetime.now()
    if n.weekday() >= 5:  # 周六、周日
        return False
    t = n.time()
    return _MARKET_OPEN[0] <= t <= _MARKET_OPEN[1]


def refresh_quotes(conn, codes):
    """批量刷新给定基金的估值:写 fund_quote 最新快照 + 追加 fund_quote_tick 时序点。

    返回成功数。fund_quote 仍按 ON CONFLICT 覆盖(兼容 enrich_holding 只读最新快照);
    fund_quote_tick 用 INSERT OR IGNORE 按本地采样时刻去重,今日逐点累积成折线。
    tick 表缺失(旧库未迁移)时写时序只日志、不影响快照写入。
    """
    ok = 0
    now = datetime.now()
    qd, qt = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
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
        # 追加今日时序点(表缺失兜底,不阻断快照写入)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO fund_quote_tick"
                "(fund_code,quote_date,quote_time,gsz,gszzl,dwjz,gztime) "
                "VALUES(:fund_code,:qd,:qt,:gsz,:gszzl,:dwjz,:gztime)",
                {**d, "qd": qd, "qt": qt},
            )
        except Exception as e:  # noqa: BLE001 —— tick 表未迁移等,只日志
            print(f"[fundgz] 写 tick 失败 {code}: {type(e).__name__} {e}")
        ok += 1
    conn.commit()
    return ok


if __name__ == "__main__":
    print(fetch_estimate("020608"))
