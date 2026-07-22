# -*- coding: utf-8 -*-
"""基金排行榜抓取 —— 数据源: 东方财富基金排行(P1b)。

对标天天基金「基金排行」逛入口。抓 6 大类 × 5 区间 的 topN 榜单,低频(日更)写入
fund_rank 缓存,业务层只读(守 CLAUDE.md「抓取层是唯一对外接口」红线)。

数据源: fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf&ft={类}&sc={区间}&st=desc&pi=1&pn={N}
返回 `var rankData = {datas:["code,name,pinyin,navdate,nav,accnav,日增,近1周,近1月,近3月,
近6月,近1年,近2年,近3年,今年来,成立来,成立日,...", ...], allRecords:..., ...};`
CSV 字段下标(已实测 2026-07-21 校准):0 代码 1 名称 3 净值日 4 单位净值
8 近1月 9 近3月 10 近6月 11 近1年 14 今年来。

parse_rank 与 fetch_rank 分离,便于离线报文单测(不发真实网络)。抓取失败优雅降级,
不动已有榜单快照。
"""
import json
import re
import ssl
import urllib.request

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/data/fundranking.html",
}

# 大类:(内部key/ft值, 展示名)。all 走无 ft 的全市场开放式。
CATEGORIES = [
    ("all", "全部"),
    ("gp", "股票"),
    ("hh", "混合"),
    ("zs", "指数"),
    ("zq", "债券"),
    ("qdii", "QDII"),
]
# 区间:(内部key, 展示名, 东财 sc 排序字段)
PERIODS = [
    ("1m", "近1月", "1yzf"),
    ("3m", "近3月", "3yzf"),
    ("6m", "近6月", "6yzf"),
    ("1y", "近1年", "1nzf"),
    ("ytd", "今年来", "jnzf"),
]
TOP_N = 30

_URL = "https://fund.eastmoney.com/data/rankhandler.aspx"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _at(fields, i):
    """安全取 CSV 第 i 段,越界/空串返回 ''。"""
    return fields[i] if i < len(fields) and fields[i] != "" else ""


def parse_rank(raw):
    """解析 rankhandler 报文 → [{fund_code,name,nav_date,nav,r_1m,r_3m,r_6m,r_1y,r_ytd}, ...]。

    datas 是 JS 字符串数组(每项一只基金的 CSV)。非 JSON / 无 datas 返回 []。
    """
    m = re.search(r"datas:\s*(\[.*?\])\s*,\s*allRecords", raw, re.S)
    if not m:
        # 兜底:无 allRecords 尾巴时,取第一个 [...] 块
        m = re.search(r"datas:\s*(\[.*?\])", raw, re.S)
        if not m:
            return []
    try:
        items = json.loads(m.group(1))
    except (ValueError, TypeError):
        return []
    out = []
    for s in items:
        if not isinstance(s, str):
            continue
        f = s.split(",")
        if len(f) < 15 or not f[0]:
            continue
        out.append({
            "fund_code": f[0],
            "name": f[1],
            "nav_date": _at(f, 3),
            "nav": _f(_at(f, 4)),
            "r_1m": _f(_at(f, 8)),
            "r_3m": _f(_at(f, 9)),
            "r_6m": _f(_at(f, 10)),
            "r_1y": _f(_at(f, 11)),
            "r_ytd": _f(_at(f, 14)),
        })
    return out


def _build_url(ft, sc, top):
    url = f"{_URL}?op=ph&dt=kf&sc={sc}&st=desc&pi=1&pn={top}&dx=1"
    if ft and ft != "all":
        url += f"&ft={ft}"
    return url


def fetch_rank(ft, sc, top=TOP_N):
    """拉单个(大类,区间)榜单。成功返回 list,失败返回 [](不抛,优雅降级)。"""
    try:
        req = urllib.request.Request(_build_url(ft, sc, top), headers=_UA)
        raw = urllib.request.urlopen(req, timeout=12, context=_CTX).read().decode("utf-8", "replace")
        return parse_rank(raw)
    except Exception as e:  # noqa: BLE001 —— 抓取失败不影响服务
        print(f"[fund_rank] 拉取榜单失败 ft={ft} sc={sc}(不影响服务): {type(e).__name__} {e}")
        return []


def refresh_rank(conn, fetch=fetch_rank, categories=CATEGORIES, periods=PERIODS, top=TOP_N):
    """抓 6 大类 × 5 区间榜单,每组「先删后插」写 fund_rank。返回写入总行数。

    fetch(ft, sc, top) 可注入,便于单测喂离线样本。某组抓取失败(返回空)则跳过、
    保留该组已有榜单,不清空。
    """
    total = 0
    for ft, _cat_label in categories:
        for pkey, _p_label, sc in periods:
            rows = fetch(ft, sc, top)
            if not rows:
                continue
            conn.execute(
                "DELETE FROM fund_rank WHERE period=? AND category=?", (pkey, ft))
            for i, r in enumerate(rows, 1):
                conn.execute(
                    """INSERT OR REPLACE INTO fund_rank
                       (period,category,rank,fund_code,name,nav_date,nav,
                        r_1m,r_3m,r_6m,r_1y,r_ytd,updated_at)
                       VALUES (:period,:category,:rank,:fund_code,:name,:nav_date,:nav,
                        :r_1m,:r_3m,:r_6m,:r_1y,:r_ytd,datetime('now','localtime'))""",
                    {**r, "period": pkey, "category": ft, "rank": i},
                )
                total += 1
            conn.commit()
    return total


if __name__ == "__main__":
    print(fetch_rank("gp", "1nzf", 3))
