# -*- coding: utf-8 -*-
"""基金重仓股抓取 —— 数据源: 天天基金 F10 jjcc(季度持仓明细)(P2)。

从 fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}&topline=10
解析最新一期 Top10 重仓股(股票代码/名称/占净值比例/报告期)。低频(日更/首访兜底)
写入 fund_holding_stock 缓存,业务层只读(守 CLAUDE.md「抓取层是唯一对外接口」)。

返回体形如 var apidata={ content:"<div>...2026年2季度股票投资明细...<tbody>
<tr><td>1</td><td><a ...>600519</a></td><td class='tol'><a ...>贵州茅台</a></td>
...<td class='tor'>9.77%</td>...</tr>...</tbody>...",arryear:[...],curyear:...};

parse_holdings 与 fetch_holdings 分离,便于离线报文单测。失败优雅降级返回 []。
"""
import re
import ssl
import urllib.request

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://fundf10.eastmoney.com/",
}

_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}&topline=10"
_TOP_N = 10

# 行内:股票代码(quote 链接)/ 名称(td.tol)/ 占净值比例(首个带 % 的 td.tor)
_RE_PERIOD = re.compile(r"(\d{4}年\d季度)")
_RE_TBODY = re.compile(r"<tbody>(.*?)</tbody>", re.S)
_RE_TR = re.compile(r"<tr>(.*?)</tr>", re.S)
_RE_CODE = re.compile(r"unify/r/\d\.(\d+)")
_RE_NAME = re.compile(r"class='tol'>\s*<a[^>]*>([^<]+)</a>")
_RE_WEIGHT = re.compile(r"<td class='tor'>\s*([\d.]+)%")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_holdings(raw):
    """解析 F10 jjcc 报文 → [{rank,stock_code,stock_name,weight,report_period}, ...]。

    仅取最新一期(第一个 tbody)Top N;无 tbody / 无有效行返回 []。
    """
    period_m = _RE_PERIOD.search(raw or "")
    period = period_m.group(1) if period_m else None
    body_m = _RE_TBODY.search(raw or "")
    if not body_m:
        return []
    out = []
    for tr in _RE_TR.findall(body_m.group(1)):
        code_m = _RE_CODE.search(tr)
        name_m = _RE_NAME.search(tr)
        if not code_m or not name_m:
            continue
        w_m = _RE_WEIGHT.search(tr)
        out.append({
            "rank": len(out) + 1,
            "stock_code": code_m.group(1),
            "stock_name": name_m.group(1).strip(),
            "weight": _f(w_m.group(1)) if w_m else None,
            "report_period": period,
        })
        if len(out) >= _TOP_N:
            break
    return out


def fetch_holdings(code):
    """拉单只基金 Top10 重仓股。成功返回 list,失败返回 [](不抛,优雅降级)。"""
    try:
        req = urllib.request.Request(_URL.format(code=code), headers=_UA)
        raw = urllib.request.urlopen(req, timeout=12, context=_CTX).read().decode("utf-8", "replace")
        return parse_holdings(raw)
    except Exception as e:  # noqa: BLE001 —— 抓取失败不影响服务
        print(f"[fund_holdings] 拉取 {code} 重仓股失败(不影响服务): {type(e).__name__} {e}")
        return []


def refresh_holdings(conn, codes, fetch=fetch_holdings):
    """批量抓取重仓股,每只基金「先删后插」最新一期。返回有数据的基金数。

    fetch(code) 可注入,便于单测。某基金抓取失败(空)则跳过、保留其已有持仓。
    """
    ok = 0
    for code in codes:
        rows = fetch(code)
        if not rows:
            continue
        conn.execute("DELETE FROM fund_holding_stock WHERE fund_code=?", (code,))
        for r in rows:
            conn.execute(
                """INSERT OR REPLACE INTO fund_holding_stock
                   (fund_code,rank,stock_code,stock_name,weight,report_period,updated_at)
                   VALUES (:fund_code,:rank,:stock_code,:stock_name,:weight,:report_period,
                           datetime('now','localtime'))""",
                {**r, "fund_code": code},
            )
        ok += 1
        conn.commit()
    return ok


if __name__ == "__main__":
    print(fetch_holdings("110022"))
