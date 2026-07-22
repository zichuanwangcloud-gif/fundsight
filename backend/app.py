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
    start_index_refresh,
    start_rank_refresh,
    start_history_refresh, trigger_history_for,
    start_profile_refresh,
    start_holdings_refresh,
    start_nav_gap_check,
    start_session_purge,
    start_tick_purge,
    start_alert_dispatcher,
    start_trailing_stop_check,
    start_dca_plan_check,
)
from backend import auth  # noqa: E402
from backend.api import ALL_ROUTES  # noqa: E402
from backend.api._router import dispatch, rate_limit_guard  # noqa: E402

SESSION_COOKIE = "fs_session"
# HTTPS 部署时设 FUNDSIGHT_SECURE_COOKIE=1 给会话 Cookie 追加 Secure 标志
# (本地 http 不能开,否则浏览器不回传 Cookie)。README 对应 TODO 落地。
SECURE_COOKIE = os.environ.get("FUNDSIGHT_SECURE_COOKIE", "").lower() in ("1", "true", "yes")

FRONTEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "index.html")
FRONTEND_DIR = os.path.dirname(FRONTEND)

# 静态资源白名单:扩展名 → Content-Type
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def _recover_path(raw):
    """还原请求路径为 UTF-8。

    http.server 按 latin-1 解码请求行,URL 里未百分号编码的中文(如浏览器地址栏
    直接输入或某些客户端未编码)会被拆成逐字节的 latin-1 乱码。按 latin-1 编回原始
    字节、再以 UTF-8 解码即可还原;纯 ASCII 或已百分号编码(%XX 均为 ASCII)的路径
    round-trip 无损,是空操作。无法还原(非法字节序列)时原样返回,不影响原有行为。
    """
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


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
        "target_price=?,stop_profit=?,stop_loss=?,trailing_stop_pct=? "
        "WHERE id=? AND user_id=?",
        (
            _num(data.get("hold_amount")),
            _num(data.get("cost_amount")),
            _num(data.get("target_rate")),
            _num(data.get("target_price")),
            _num(data.get("stop_profit")),
            _num(data.get("stop_loss")),
            _num(data.get("trailing_stop_pct")),
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

    def _client_ip(self):
        """客户端 IP:X-Forwarded-0(反代场景)优先,否则 client_address。"""
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _rate_limited(self, path):
        """分发前限流检查(M10B-B1):超限回 429 并返回 True,否则返回 False。

        按 user_id + 端点 限频(60 次/分,宽松自用级)。未登录端点(login/register)
        不限流:user_id 为 None 时放行。
        """
        uid = self._current_user()
        if uid is None:
            return False
        blocked = rate_limit_guard(uid, path)
        if blocked is None:
            return False
        code, obj = blocked
        self._json(obj, code)
        return True

    def _session_cookie_header(self, token, max_age=30 * 24 * 3600):
        # HTTPS 部署设 FUNDSIGHT_SECURE_COOKIE=1 即追加 Secure(本地 http 不开)。
        secure = "; Secure" if SECURE_COOKIE else ""
        return (
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax{secure}; Max-Age={max_age}",
        )

    def _clear_cookie_header(self):
        secure = "; Secure" if SECURE_COOKIE else ""
        return (
            "Set-Cookie",
            f"{SESSION_COOKIE}=; HttpOnly; Path=/; SameSite=Lax{secure}; Max-Age=0",
        )

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or "{}") if n else {}

    def _path(self):
        """UTF-8 还原后的请求路径(见模块级 _recover_path)。"""
        return _recover_path(self.path)

    def do_GET(self):
        u = urlparse(self._path())
        if not u.path.startswith("/api/"):
            return self._serve_static(u.path)
        if self._rate_limited(u.path):
            return
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
        if self._try_api_routes("GET"):
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self._path()).path
        if self._rate_limited(p):
            return
        if p == "/api/register":
            return self._handle_register()
        if p == "/api/login":
            return self._handle_login()
        if p == "/api/logout":
            return self._handle_logout()
        if p == "/api/change-password":
            return self._handle_change_password()
        if p == "/api/holdings":
            uid = self._require_auth()
            if uid is None:
                return
            add_holding(self._read_json(), uid)
            return self._json({"ok": True})
        if self._try_api_routes("POST"):
            return
        self._json({"error": "not found"}, 404)

    def do_PUT(self):
        p = urlparse(self._path()).path
        if self._rate_limited(p):
            return
        if p.startswith("/api/holdings/"):
            uid = self._require_auth()
            if uid is None:
                return
            hid = p.rsplit("/", 1)[-1]
            update_holding(hid, self._read_json(), uid)
            return self._json({"ok": True})
        if self._try_api_routes("PUT"):
            return
        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        p = urlparse(self._path()).path
        if self._rate_limited(p):
            return
        if p.startswith("/api/holdings/"):
            uid = self._require_auth()
            if uid is None:
                return
            hid = p.rsplit("/", 1)[-1]
            delete_holding(hid, uid)
            return self._json({"ok": True})
        if self._try_api_routes("DELETE"):
            return
        self._json({"error": "not found"}, 404)

    def _try_api_routes(self, method):
        """尝试用扩展路由表(ALL_ROUTES)处理请求。命中并写响应返回 True。

        供后续线路(市场/详情/流水)注册的新端点用;当前登录用户注入 ctx.user_id,
        需要鉴权的 handler 自行判断。
        """
        u = urlparse(self._path())
        body = self._read_json() if method in ("POST", "PUT") else {}
        result = dispatch(ALL_ROUTES, method, u.path, parse_qs(u.query), body, self._current_user())
        if result is None:
            return False
        code, obj = result
        self._json(obj, code)
        return True

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
        username = data.get("username") or ""
        password = data.get("password") or ""
        uid = auth.authenticate(username, password)
        ip = self._client_ip()
        ua = self.headers.get("User-Agent", "")
        if uid is None:
            # 失败也落审计:未知用户 user_id 记 NULL
            auth.record_login_audit(auth.get_user_id(username), ip, ua, False)
            return self._json({"error": "用户名或密码错误"}, 401)
        auth.record_login_audit(uid, ip, ua, True)
        token = auth.create_session(uid)
        self._json({"username": auth.get_username(uid)},
                   extra_headers=[self._session_cookie_header(token)])

    def _handle_logout(self):
        # M10B-B2:登出使该用户**所有**存量 session 失效(全设备下线),
        # 而非仅删当前 token。
        token = self._session_token()
        uid = auth.get_user_by_token(token)
        if uid is not None:
            auth.revoke_user_sessions(uid)
        self._json({"ok": True}, extra_headers=[self._clear_cookie_header()])

    def _handle_change_password(self):
        """改密:校验旧密码 → 重设 → 吊销该用户所有存量 session → 重签当前会话。

        旧 token 失效(B2);当前设备拿到新 token 保持登录。
        """
        uid = self._require_auth()
        if uid is None:
            return
        data = self._read_json()
        old = data.get("old_password") or ""
        new = data.get("new_password") or ""
        if not new:
            return self._json({"error": "新密码不能为空"}, 400)
        if not auth.change_password(uid, old, new):
            return self._json({"error": "旧密码不正确"}, 401)
        # 改密已吊销所有 session(含当前);给当前设备重签,保持登录。
        token = auth.create_session(uid)
        self._json({"ok": True, "username": auth.get_username(uid), "revoked_sessions": True},
                   extra_headers=[self._session_cookie_header(token)])

    def _serve_static(self, path):
        # "/" → index.html;其余按 basename 在 frontend/ 下找,白名单扩展防穿越
        rel = "index.html" if path in ("/", "/index.html") else os.path.basename(path)
        ext = os.path.splitext(rel)[1]
        if ext not in _STATIC_TYPES:
            return self._json({"error": "not found"}, 404)
        fpath = os.path.join(FRONTEND_DIR, rel)
        if not os.path.isfile(fpath):
            return self._json({"error": "not found"}, 404)
        with open(fpath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _STATIC_TYPES[ext])
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
    # 大盘指数:启动拉一次(收盘也能拿到最新收盘价)+ 盘中每 60 秒刷新,业务层只读(P1a)。
    start_index_refresh(interval_seconds=60)
    # 基金排行榜:启动拉一次填充榜单 + 日更刷新(6 大类×5 区间 topN),业务层只读(P1b)。
    start_rank_refresh(interval_hours=24)
    # 历史净值序列:后台日更持仓基金的走势数据(走势图用)。
    start_history_refresh(interval_hours=24)
    # 基本面(经理/规模/收益/费率):后台日更持仓/被查基金的 profile,变化慢故 run_now=False。
    start_profile_refresh(interval_hours=24)
    # 重仓股 Top10(F10):后台日更持仓/被查基金的季度持仓明细,变化慢故 run_now=False(P2)。
    start_holdings_refresh(interval_hours=24)
    # 净值断点检测:日更检查持仓基金 max(nav_date) 距今是否超阈值(默认 5 天),
    # 有缺失记 task_run fail,前端「系统状态」页据此标红告警(M9-C)。
    start_nav_gap_check(interval_hours=24)
    # 过期 session 清理:日更删除 expires_at 过期的 token 行,防 session 表膨胀(M9-E)。
    start_session_purge(interval_hours=24)
    # 盘中估值时序清理:日更删除 7 天前 fund_quote_tick 旧数据,防时序表膨胀。
    start_tick_purge(interval_hours=24)
    # M10B 限流状态清理:日更删除已结束窗口的 rate_limit_state 行,防表膨胀。
    auth.start_rate_limit_cleanup(interval_hours=24)
    # 连续失败告警巡检:6h 扫一次抓取任务,连续失败超阈值即给持仓 user 推 sync_alert(M10C)。
    start_alert_dispatcher(interval_hours=6)
    start_trailing_stop_check(interval_hours=1)
    start_dca_plan_check(interval_hours=24)
    port = int(os.environ.get("PORT", 8000))
    print(f"盈见 FundSight 已启动 → http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
