# -*- coding: utf-8 -*-
"""基金看板 数据验证 spike —— 验证能否拿到 涨幅/估值/净值 并算盈亏"""
import json, re, urllib.request

def fetch_fund(code):
    url = f"https://fundgz.1234567.com.cn/js/{code}.js"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
    # 返回形如 jsonpgz({...}); 去掉外层拿到 JSON
    m = re.search(r"jsonpgz\((.*)\)", raw)
    return json.loads(m.group(1))

def show(code, hold_amount=None, profit=None):
    d = fetch_fund(code)
    name = d["name"]
    gszzl = float(d["gszzl"])   # 今日估算涨幅 %
    gsz   = float(d["gsz"])     # 今日估算净值
    dwjz  = float(d["dwjz"])    # 昨日单位净值
    arrow = "▲" if gszzl >= 0 else "▼"

    print(f"【{name}】({code})")
    print(f"  昨日净值 {dwjz}  →  今日估值 {gsz}   {arrow} {gszzl:+.2f}%")
    print(f"  估值时间 {d['gztime']}")

    if hold_amount is not None:
        # 用持仓金额反推份额(按昨日净值), 再用今日估值算今日浮动盈亏
        shares = hold_amount / dwjz
        today_pl = shares * (gsz - dwjz)      # 今天这一天赚/亏多少
        est_now  = shares * gsz               # 今日估算市值
        print(f"  持仓金额 {hold_amount:.2f}  ≈ {shares:.2f} 份")
        print(f"  今日估算市值 {est_now:.2f}   今日浮动盈亏 {today_pl:+.2f}")
        if profit is not None:
            print(f"  你的累计收益 {profit:+.2f}（成本约 {hold_amount - profit:.2f}）")
    print()

if __name__ == "__main__":
    # 先只验证数据 (无持仓)
    show("020608")
    # 再演示: 假设持仓 10000 元、累计收益 +1500 元
    print("—— 下面用假设的 持仓10000 / 收益+1500 演示盈亏计算 ——")
    show("020608", hold_amount=10000, profit=1500)
