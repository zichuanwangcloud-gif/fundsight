# -*- coding: utf-8 -*-
"""盘中估值抓取 —— 数据源: 天天基金移动端行情接口 FundMNFInfo。

原 JSONP 接口 fundgz.1234567.com.cn/js/{code}.js 已于 2026-07 被上游下线
(任意基金码均返回「页面未找到」),改用天天基金 App 在用的移动端接口:
  https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo?Fcodes={code}&...
返回 JSON,单条含:
  NAV      最新官方单位净值(收盘)         —— 映射 dwjz(估值基准=最近收盘价)
  GSZ      盘中估算净值(非交易时段为 null) —— 映射 gsz
  GSZZL    盘中估算涨跌幅 %                 —— 映射 gszzl
  GZTIME   估值时间                        —— 映射 gztime
  SHORTNAME 简称                           —— 映射 name

低频调用 + 写入 fund_quote 缓存,业务层只读缓存。dwjz/gsz 口径与官方净值(nav)
分离(见 akshare_nav):dwjz=最近收盘,供盘中估算盈亏;真实盈亏用 nav/nav_prev。
"""
import json
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, time as _time

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# 移动端接口需带 App 标识参数,否则返回「网络繁忙」。deviceid 任意常量即可。
_BASE = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo"
_PARAMS = {
    "pageIndex": "1", "pageSize": "1", "plat": "Iphone", "appType": "ttjj",
    "product": "EFund", "Version": "6.4.0", "deviceid": "fundsight",
}
_UA = {"User-Agent": "EMProjJijin/6.4.0 (iPhone; iOS 15.0)"}


def fetch_estimate(code):
    """拉单只基金的盘中估值。成功返回 dict，失败返回 None。

    返回字段与历史保持一致(fund_code/name/dwjz/gsz/gszzl/gztime),上层无需改动。
    非交易时段 gsz/gszzl/gztime 为 None(接口不给估值),dwjz 仍是最新收盘价。
    """
    params = dict(_PARAMS, Fcodes=code)
    url = f"{_BASE}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers=_UA)
        raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8")
        d = json.loads(raw)
        datas = d.get("Datas") or []
        if not datas:
            return None
        r = datas[0]
        return {
            "fund_code": r.get("FCODE") or code,
            "name": r.get("SHORTNAME"),
            "dwjz": _f(r.get("NAV")),        # 最近官方收盘价 = 盘中估算基准
            "gsz": _f(r.get("GSZ")),
            "gszzl": _f(r.get("GSZZL")),
            "gztime": r.get("GZTIME"),
        }
    except Exception as e:
        print(f"[fundgz] 拉取 {code} 失败: {type(e).__name__} {e}")
        return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# A 股交易时段(本地时间)。非交易时段接口 gsz 为空(无估值),采样无意义,
# scheduler.start_quote_refresh 据此门控跳过抓取。
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
