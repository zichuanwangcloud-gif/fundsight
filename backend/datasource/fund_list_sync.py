# -*- coding: utf-8 -*-
"""全量基金列表同步 —— 把全市场约 1.8 万只基金写入 fund_list（搜索用）。

数据源: fund.eastmoney.com/js/fundcode_search.js（返回 var r = [["代码","拼音","名称","类型","全拼"],...]）

⚠️ 注意: 该接口有反爬。开发沙箱环境实测被 418 拦截；请在**能正常联网的部署环境**运行本脚本。
失败时不影响已有 fund_list（种子数据或上次同步结果照常可用）。

用法: python3 backend/datasource/fund_list_sync.py
"""
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

URL = "https://fund.eastmoney.com/js/fundcode_search.js"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/",
    "Accept": "*/*",
}


def fetch_all_funds():
    """返回 [(code, name, pinyin, type), ...]，失败返回 None。"""
    req = urllib.request.Request(URL, headers=_HEADERS)
    raw = urllib.request.urlopen(req, timeout=20, context=_CTX).read().decode("utf-8")
    m = re.search(r"=\s*(\[.*\])", raw, re.S)
    if not m:
        return None
    arr = json.loads(m.group(1))
    # 每项: [代码, 拼音简称, 名称, 类型, 全拼]
    return [(row[0], row[2], row[1], row[3]) for row in arr if len(row) >= 4]


def sync():
    try:
        funds = fetch_all_funds()
    except Exception as e:
        print(f"[fund_list_sync] 同步失败（环境可能受反爬限制）: {type(e).__name__} {e}")
        print("→ 已有 fund_list 数据不受影响，可继续使用。请在联网环境重试。")
        return 0
    if not funds:
        print("[fund_list_sync] 未解析到数据，跳过。")
        return 0
    conn = get_conn()
    conn.executemany(
        "INSERT INTO fund_list(fund_code,name,pinyin,fund_type,synced_at) "
        "VALUES (?,?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(fund_code) DO UPDATE SET "
        "name=excluded.name, pinyin=excluded.pinyin, fund_type=excluded.fund_type, "
        "synced_at=excluded.synced_at",
        funds,
    )
    conn.commit()
    conn.close()
    print(f"[fund_list_sync] 同步完成，共 {len(funds)} 只基金。")
    return len(funds)


if __name__ == "__main__":
    sync()
