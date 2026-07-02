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
from backend.datasource.fundgz import refresh_quotes  # noqa: E402

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


def list_holdings():
    conn = get_conn()
    holds = [dict(r) for r in conn.execute("SELECT * FROM holding ORDER BY id").fetchall()]
    codes = [h["fund_code"] for h in holds]
    if codes:
        refresh_quotes(conn, codes)  # 刷新估值到缓存
    result = []
    for h in holds:
        q = conn.execute("SELECT * FROM fund_quote WHERE fund_code=?", (h["fund_code"],)).fetchone()
        item = dict(h)
        if q:
            q = dict(q)
            item["name"] = q["name"]
            item["gszzl"] = q["gszzl"]      # 当日涨幅
            item["gsz"] = q["gsz"]
            item["dwjz"] = q["dwjz"]
            item["gztime"] = q["gztime"]
            # 今日浮动盈亏 = 份额 * (gsz - dwjz)，份额 = 持仓金额 / dwjz
            if h["hold_amount"] and q["dwjz"] and q["gsz"]:
                shares = h["hold_amount"] / q["dwjz"]
                item["today_pl"] = round(shares * (q["gsz"] - q["dwjz"]), 2)
                item["est_value"] = round(shares * q["gsz"], 2)
            # 距目标: 目标净值 - 当前估值
            if h["target_price"] and q["gsz"]:
                item["gap_to_target"] = round(h["target_price"] - q["gsz"], 4)
        result.append(item)
    conn.close()
    return result


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


def delete_holding(hid):
    conn = get_conn()
    conn.execute("DELETE FROM holding WHERE id=?", (hid,))
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
    port = int(os.environ.get("PORT", 8000))
    print(f"盈见 FundSight 已启动 → http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
