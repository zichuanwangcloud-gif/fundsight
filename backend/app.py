# -*- coding: utf-8 -*-
"""盈见 FundSight 业务 API —— 纯标准库 http.server，零依赖。

接口:
  POST /api/register              注册账号（用户名+密码）→ 登录态
  POST /api/login                 登录 → 签发会话 Cookie
  POST /api/logout                登出 → 清会话
  GET  /api/me                    当前登录用户（未登录 401）
  GET  /api/search?q=关键字        搜本地 fund_list（代码/名称/拼音，公共数据）
  GET  /api/holdings               我的自选 + 实时估值 + 盈亏 + 距预期（按用户隔离）
  POST /api/holdings               加自选/录持仓
  DELETE /api/holdings/{id}        移除自选
  GET  /                           前端页面

自选数据按登录用户隔离：所有 holding 读写均带 user_id，越权访问不生效。
"""
import json
import os
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.models.db import get_conn, init_db  # noqa: E402
from backend.scheduler import (  # noqa: E402
    maybe_bootstrap_sync, start_periodic_sync, start_nav_refresh,
    start_quote_refresh, trigger_quote_for,
    start_history_refresh, trigger_history_for,
)
from backend import auth  # noqa: E402

SESSION_COOKIE = "fs_session"

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


def nav_history(code, days=90):
    """读 fund_nav_history 缓存,返回最近 days 天的净值序列(升序)。

    纯读缓存,不触发外部抓取(延续 M6 业务层只读原则)。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT nav_date, nav FROM fund_nav_history WHERE fund_code=? "
        "ORDER BY nav_date DESC LIMIT ?",
        (code, days),
    ).fetchall()
    conn.close()
    # 取最近 days 天后翻回升序,便于前端从左到右画图
    points = [{"d": r["nav_date"], "v": r["nav"]} for r in reversed(rows)]
    return {"code": code, "points": points}


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


def list_holdings(user_id):
    # 业务层只读缓存：估值由 scheduler 后台定时写入 fund_quote，
    # 这里绝不触发外部抓取（防封 IP + 扛并发 + 响应快）。按用户隔离。
    conn = get_conn()
    holds = [dict(r) for r in conn.execute(
        "SELECT * FROM holding WHERE user_id=? ORDER BY id", (user_id,)).fetchall()]
    items = []
    for h in holds:
        q = conn.execute("SELECT * FROM fund_quote WHERE fund_code=?", (h["fund_code"],)).fetchone()
        items.append(enrich_holding(h, q))
    conn.close()
    return {"items": items, "summary": summarize(items)}


def add_holding(data, user_id):
    conn = get_conn()
    conn.execute(
        "INSERT INTO holding(user_id,fund_code,hold_amount,cost_amount,target_rate,target_price,"
        "stop_profit,stop_loss,created_at) VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))",
        (
            user_id,
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
        trigger_history_for(code)  # 顺带拉历史序列,新持仓卡片几秒内有走势图


def delete_holding(hid, user_id):
    conn = get_conn()
    conn.execute("DELETE FROM holding WHERE id=? AND user_id=?", (hid, user_id))
    conn.commit()
    conn.close()


def update_holding(hid, data, user_id):
    conn = get_conn()
    conn.execute(
        "UPDATE holding SET hold_amount=?,cost_amount=?,target_rate=?,"
        "target_price=?,stop_profit=?,stop_loss=? WHERE id=? AND user_id=?",
        (
            _num(data.get("hold_amount")),
            _num(data.get("cost_amount")),
            _num(data.get("target_rate")),
            _num(data.get("target_price")),
            _num(data.get("stop_profit")),
            _num(data.get("stop_loss")),
            hid,
            user_id,
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
    def _json(self, obj, code=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # ---- 鉴权辅助 ----
    def _session_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except Exception:
            return None
        m = jar.get(SESSION_COOKIE)
        return m.value if m else None

    def _current_user(self):
        """当前登录用户 id | None。"""
        return auth.get_user_by_token(self._session_token())

    def _require_auth(self):
        """返回 user_id；未登录则回 401 并返回 None（调用方据此提前 return）。"""
        uid = self._current_user()
        if uid is None:
            self._json({"error": "unauthorized"}, 401)
            return None
        return uid

    def _session_cookie_header(self, token, max_age=30 * 24 * 3600):
        # 本地 http 不加 Secure；HTTPS 部署应追加 "; Secure"。
        return (
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={max_age}",
        )

    def _clear_cookie_header(self):
        return (
            "Set-Cookie",
            f"{SESSION_COOKIE}=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0",
        )

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or "{}") if n else {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            return self._serve_html()
        if u.path == "/api/me":
            uid = self._require_auth()
            if uid is None:
                return
            return self._json({"username": auth.get_username(uid)})
        if u.path == "/api/search":
            if self._require_auth() is None:
                return
            q = (parse_qs(u.query).get("q") or [""])[0].strip()
            return self._json(search_funds(q) if q else [])
        if u.path == "/api/holdings":
            uid = self._require_auth()
            if uid is None:
                return
            return self._json(list_holdings(uid))
        if u.path == "/api/nav_history":
            if self._require_auth() is None:
                return
            qs = parse_qs(u.query)
            code = (qs.get("code") or [""])[0].strip()
            try:
                days = int((qs.get("days") or ["90"])[0])
            except ValueError:
                days = 90
            return self._json(nav_history(code, days) if code else {"code": "", "points": []})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/register":
            return self._handle_register()
        if p == "/api/login":
            return self._handle_login()
        if p == "/api/logout":
            return self._handle_logout()
        if p == "/api/holdings":
            uid = self._require_auth()
            if uid is None:
                return
            add_holding(self._read_json(), uid)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def do_PUT(self):
        p = urlparse(self.path).path
        if p.startswith("/api/holdings/"):
            uid = self._require_auth()
            if uid is None:
                return
            hid = p.rsplit("/", 1)[-1]
            update_holding(hid, self._read_json(), uid)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if p.startswith("/api/holdings/"):
            uid = self._require_auth()
            if uid is None:
                return
            hid = p.rsplit("/", 1)[-1]
            delete_holding(hid, uid)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    # ---- 账号端点 ----
    def _handle_register(self):
        data = self._read_json()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            return self._json({"error": "用户名和密码不能为空"}, 400)
        try:
            uid = auth.create_user(username, password)
        except auth.UsernameTaken:
            return self._json({"error": "用户名已被占用"}, 409)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        token = auth.create_session(uid)
        self._json({"username": username}, extra_headers=[self._session_cookie_header(token)])

    def _handle_login(self):
        data = self._read_json()
        uid = auth.authenticate(data.get("username"), data.get("password") or "")
        if uid is None:
            return self._json({"error": "用户名或密码错误"}, 401)
        token = auth.create_session(uid)
        self._json({"username": auth.get_username(uid)},
                   extra_headers=[self._session_cookie_header(token)])

    def _handle_logout(self):
        auth.delete_session(self._session_token())
        self._json({"ok": True}, extra_headers=[self._clear_cookie_header()])

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
    # 历史净值序列:后台日更持仓基金的走势数据(走势图用)。
    start_history_refresh(interval_hours=24)
    port = int(os.environ.get("PORT", 8000))
    print(f"盈见 FundSight 已启动 → http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
