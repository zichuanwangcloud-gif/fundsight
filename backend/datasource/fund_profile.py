# -*- coding: utf-8 -*-
"""基金基本面抓取 —— 数据源: 天天基金 pingzhongdata 接口(与 nav_history.py 同源)。

从 https://fund.eastmoney.com/pingzhongdata/{code}.js 解析:
  - fS_name                  基金全称
  - syl_1n/syl_3y/syl_6y/syl_1y  近1年/3月/6月/1月收益率 %(pingzhongdata 原始
    变量命名如此,非本模块笔误,详情见各字段行内注释)
  - fund_Rate                现行管理费率
  - Data_currentFundManager  现任基金经理(取第一位 name)
  - Data_fluctuationScale    规模变动(取 series 最后一项 y,单位:亿元)

低频调用 + 写入 fund_profile 缓存,业务层只读。抓取/解析失败一律返回 None,
不抛异常(优雅降级)。

用法: python3 backend/datasource/fund_profile.py
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


def _str_field(raw, var_name):
    """解析形如 var xxx = "yyy"; 的字符串字段。未命中返回 None。"""
    m = re.search(var_name + r'\s*=\s*"([^"]*)"', raw)
    return m.group(1) if m else None


def _json_field(raw, var_name):
    """解析形如 var xxx = [...]; 或 {...}; 的 JSON 字段(贪婪到分号)。

    未命中或解析失败返回 None。
    """
    m = re.search(var_name + r"\s*=\s*([\[{].*?[\]}])\s*;", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def fetch_profile(code):
    """拉取单只基金的基本面快照。成功返回 dict,失败(网络/解析)返回 None。"""
    url = _PINGZHONG_URL.format(code=code)
    try:
        req = urllib.request.Request(url, headers=_UA)
        raw = urllib.request.urlopen(req, timeout=15, context=_CTX).read().decode(
            "utf-8", errors="ignore"
        )
    except Exception as e:
        print(f"[fund_profile] 拉取 {code} 失败: {type(e).__name__} {e}")
        return None

    try:
        name = _str_field(raw, "fS_name")
        rate = _str_field(raw, "fund_Rate")
        syl_1n = _f(_str_field(raw, "syl_1n"))
        syl_3y = _f(_str_field(raw, "syl_3y"))
        syl_6y = _f(_str_field(raw, "syl_6y"))
        syl_1y = _f(_str_field(raw, "syl_1y"))

        manager = None
        managers = _json_field(raw, "Data_currentFundManager")
        if managers:
            # Data_currentFundManager 后有时紧跟一个多余空格再分号(如 "] ;"),
            # 上面的 JSON 字段正则以非贪婪匹配到最近的 "];" 已足够覆盖常见报文。
            first = managers[0] if isinstance(managers, list) and managers else None
            if isinstance(first, dict):
                manager = first.get("name")

        scale = None
        fluct = _json_field(raw, "Data_fluctuationScale")
        if isinstance(fluct, dict):
            series = fluct.get("series") or []
            if series and isinstance(series[-1], dict):
                scale = _f(series[-1].get("y"))

        if not name and manager is None and scale is None and rate is None:
            return None  # 报文完全不含预期字段,视为解析失败

        return {
            "fund_code": code,
            "name": name,
            "manager": manager,
            "scale": scale,
            "rate": rate,
            "syl_1n": syl_1n,
            "syl_3y": syl_3y,
            "syl_6y": syl_6y,
            "syl_1y": syl_1y,
        }
    except Exception as e:  # noqa: BLE001 —— 解析环节兜底,绝不向上抛
        print(f"[fund_profile] 解析 {code} 失败: {type(e).__name__} {e}")
        return None


def refresh_profile(conn, codes):
    """批量拉取并写入 fund_profile。主键 fund_code 幂等覆盖。返回成功数。"""
    ok = 0
    for code in codes:
        d = fetch_profile(code)
        if not d:
            continue
        conn.execute(
            """INSERT INTO fund_profile(
                 fund_code,name,manager,scale,rate,syl_1n,syl_3y,syl_6y,syl_1y,updated_at)
               VALUES (:fund_code,:name,:manager,:scale,:rate,
                       :syl_1n,:syl_3y,:syl_6y,:syl_1y,datetime('now','localtime'))
               ON CONFLICT(fund_code) DO UPDATE SET
                 name=excluded.name, manager=excluded.manager, scale=excluded.scale,
                 rate=excluded.rate, syl_1n=excluded.syl_1n, syl_3y=excluded.syl_3y,
                 syl_6y=excluded.syl_6y, syl_1y=excluded.syl_1y,
                 updated_at=excluded.updated_at""",
            d,
        )
        ok += 1
    conn.commit()
    return ok


if __name__ == "__main__":
    conn = get_conn()
    print(fetch_profile("020608"))
    conn.close()
