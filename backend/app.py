# -*- coding: utf-8 -*-
"""盈见 FundSight 业务 API —— 纯标准库 http.server，零依赖。

接口:
  GET  /api/search?q=关键字        搜本地 fund_list（代码/名称/拼音）
  GET  /api/holdings               我的自选 + 实时估值 + 盈亏 + 距预期
  POST /api/holdings               加自选/录持仓
  DELETE /api/holdings/{id}        移除自选
  GET  /                           前端页面
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.models.db import get_conn, init_db  # noqa: E402
from backend.scheduler import (  # noqa: E402
    maybe_bootstrap_sync, start_periodic_sync, start_nav_refresh,
    start_quote_refresh, trigger_quote_for,
)

FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "index.html")


def search_funds(q):
    conn = get_conn()
    like = f"%{q}%"
    rows = conn.execute(
        "SELECT fund_code,name,fund_type FROM fund_list "
        "WHERE fund_code LIKE ? OR name LIKE ? OR pinyin LIKE ? LIMIT 20",
        (like, like, like),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def enrich_holding(h, quote):
    """单条持仓 + 行情缓存 → 富集后的展示项（纯计算，无副作用）。

    h: holding 行 dict；quote: fund_quote 行 dict 或 None。
    取不到行情时只返回持仓基础字段，业务层容忍缺估值。
    """
    item = dict(h)
    if not quote:
        return item
    q = dict(quote)
    item["name"] = q["name"]
    item["gszzl"] = q["gszzl"]      # 当日涨幅
    item["gsz"] = q["gsz"]
    item["dwjz"] = q["dwjz"]
    item["gztime"] = q["gztime"]
    item["quote_updated_at"] = q.get("updated_at")  # 缓存写入时间(新鲜度提示用)
    nav = q.get("nav")
    # 今日浮动盈亏 = 份额 * (gsz - dwjz)，份额 = 持仓金额 / dwjz
    shares = None
    if h["hold_amount"] and q["dwjz"] and q["gsz"]:
        shares = h["hold_amount"] / q["dwjz"]
        item["today_pl"] = round(shares * (q["gsz"] - q["dwjz"]), 2)
        item["est_value"] = round(shares * q["gsz"], 2)
    # 收盘真实盈亏（官方净值 nav）：与估算并存，份额同口径便于对照
    if nav is not None and h["hold_amount"] and q["dwjz"]:
        real_shares = h["hold_amount"] / q["dwjz"]
        item["nav"] = nav
        item["nav_date"] = q.get("nav_date")
        item["real_value"] = round(real_shares * nav, 2)
        item["real_pl"] = round(real_shares * (nav - q["dwjz"]), 2)
    # 距目标: 目标净值 - 当前估值
    if h["target_price"] and q["gsz"]:
        item["gap_to_target"] = round(h["target_price"] - q["gsz"], 4)
    # 持仓收益率%（估算口径）= (估算市值 - 成本) / 成本 * 100
    if h["cost_amount"] and item.get("est_value") is not None:
        cost_return_rate = (item["est_value"] - h["cost_amount"]) / h["cost_amount"] * 100
        item["cost_return_rate"] = round(cost_return_rate, 2)
        # 真实收益率%（官方净值口径，若有 nav）
        real_return_rate = None
        if item.get("real_value") is not None:
            real_return_rate = (item["real_value"] - h["cost_amount"]) / h["cost_amount"] * 100
            item["real_return_rate"] = round(real_return_rate, 2)
        # 止盈止损优先用真实收益率（准），无 nav 时回退估算收益率
        judge_rate = real_return_rate if real_return_rate is not None else cost_return_rate
        if h["stop_profit"] is not None:
            item["hit_stop_profit"] = judge_rate >= h["stop_profit"]
        if h["stop_loss"] is not None:
            item["hit_stop_loss"] = judge_rate <= h["stop_loss"]
        # 距目标收益率（沿用估算口径）
        if h["target_rate"] is not None:
            item["gap_to_target_rate"] = round(h["target_rate"] - cost_return_rate, 2)
    return item


def summarize(items):
    """富集项列表 → 组合总览汇总（纯计算）。

    口径约定：总市值 / 今日盈亏累加所有有值的持仓；累计盈亏与总收益率
    只对「同时具备 est_value 与 cost_amount」的子集计算，避免混入无成本
    记录导致收益率失真。matched_count 供前端标注「基于 N 笔有成本记录」。
    """
    total_today_pl = 0.0
    total_est_value = 0.0
    total_cost = 0.0
    matched_est = 0.0
    matched_count = 0
    total_real_value = 0.0
    total_real_pl = 0.0
    real_count = 0
    for it in items:
        if it.get("today_pl") is not None:
            total_today_pl += it["today_pl"]
        if it.get("est_value") is not None:
            total_est_value += it["est_value"]
            if it.get("cost_amount") is not None:
                total_cost += it["cost_amount"]
                matched_est += it["est_value"]
                matched_count += 1
        # 真实口径：仅累加有官方净值(real_value)的持仓
        if it.get("real_value") is not None:
            total_real_value += it["real_value"]
            if it.get("real_pl") is not None:
                total_real_pl += it["real_pl"]
            real_count += 1
    total_pl = round(matched_est - total_cost, 2) if matched_count else None
    total_return_rate = (
        round((matched_est - total_cost) / total_cost * 100, 2)
        if total_cost > 0 else None
    )
    return {
        "count": len(items),
        "total_today_pl": round(total_today_pl, 2),
        "total_est_value": round(total_est_value, 2),
        "total_cost": round(total_cost, 2),
        "matched_count": matched_count,
        "total_pl": total_pl,
        "total_return_rate": total_return_rate,
        "total_real_value": round(total_real_value, 2),
        "total_real_pl": round(total_real_pl, 2) if real_count else None,
    }


def list_holdings():
    # 业务层只读缓存：估值由 scheduler 后台定时写入 fund_quote，
    # 这里绝不触发外部抓取（防封 IP + 扛并发 + 响应快）。
    conn = get_conn()
    holds = [dict(r) for r in conn.execute("SELECT * FROM holding ORDER BY id").fetchall()]
    items = []
    for h in holds:
        q = conn.execute("SELECT * FROM fund_quote WHERE fund_code=?", (h["fund_code"],)).fetchone()
        items.append(enrich_holding(h, q))
    conn.close()
    return {"items": items, "summary": summarize(items)}


def add_holding(data):
    conn = get_conn()
    conn.execute(
        "INSERT INTO holding(fund_code,hold_amount,cost_amount,target_rate,target_price,"
        "stop_profit,stop_loss,created_at) VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))",
        (
            data.get("fund_code"),
            _num(data.get("hold_amount")),
            _num(data.get("cost_amount")),
            _num(data.get("target_rate")),
            _num(data.get("target_price")),
            _num(data.get("stop_profit")),
            _num(data.get("stop_loss")),
        ),
    )
    conn.commit()
    conn.close()
    # 补空窗：新增持仓后后台拉一次该基金估值，用户几秒内即可见，
    # 无需等 60 秒定时周期（拉取失败由定时任务兜底）。
    code = data.get("fund_code")
    if code:
        trigger_quote_for(code)


def delete_holding(hid):
    conn = get_conn()
    conn.execute("DELETE FROM holding WHERE id=?", (hid,))
    conn.commit()
    conn.close()


def update_holding(hid, data):
    conn = get_conn()
    conn.execute(
        "UPDATE holding SET hold_amount=?,cost_amount=?,target_rate=?,"
        "target_price=?,stop_profit=?,stop_loss=? WHERE id=?",
        (
            _num(data.get("hold_amount")),
            _num(data.get("cost_amount")),
            _num(data.get("target_rate")),
            _num(data.get("target_price")),
            _num(data.get("stop_profit")),
            _num(data.get("stop_loss")),
            hid,
        ),
    )
    conn.commit()
    conn.close()


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            return self._serve_html()
        if u.path == "/api/search":
            q = (parse_qs(u.query).get("q") or [""])[0].strip()
            return self._json(search_funds(q) if q else [])
        if u.path == "/api/holdings":
            return self._json(list_holdings())
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path == "/api/holdings":
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or "{}")
            add_holding(data)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def do_PUT(self):
        p = urlparse(self.path).path
        if p.startswith("/api/holdings/"):
            hid = p.rsplit("/", 1)[-1]
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or "{}")
            update_holding(hid, data)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if p.startswith("/api/holdings/"):
            hid = p.rsplit("/", 1)[-1]
            delete_holding(hid)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def _serve_html(self):
        with open(FRONTEND, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 静默


def main():
    init_db()
    # 启动即拉全量列表(仅初始种子态时),并起定时刷新;均为后台 daemon,不阻塞。
    maybe_bootstrap_sync()
    start_periodic_sync(interval_days=7)
    # 收盘官方净值:启动即回填一次,之后每 12h 刷新持仓基金的官方净值。
    start_nav_refresh(interval_hours=12)
    # 盘中估值:后台每 60 秒刷新持仓估值,业务层只读缓存(不在请求路径现拉)。
    start_quote_refresh(interval_seconds=60)
    port = int(os.environ.get("PORT", 8000))
    print(f"盈见 FundSight 已启动 → http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
