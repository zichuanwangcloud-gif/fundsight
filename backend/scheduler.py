# -*- coding: utf-8 -*-
"""进程内轻量调度 —— 全量基金列表的启动自拉与定时刷新。

坚持项目「单进程 + 零第三方依赖」约定:不引入 cron/celery,定时任务用
标准库 threading 的 daemon 线程实现。任何同步失败只打日志、不影响服务。

- maybe_bootstrap_sync(): 启动时若 fund_list 仍是初始种子态,后台拉一次全量。
- start_periodic_sync():  daemon 线程,按周期(默认 7 天)刷新全量列表。

可观测性(M9-A):每次后台任务经 _record_run() 落 task_run 表,记录开始/结束/
耗时/状态(ok|fail)/处理条数/错误信息,供 /api/admin/sync-status 只读排查。
"""
import threading
import time
from datetime import datetime, timedelta

from backend.models.db import get_conn, SEED_FUNDS


def _now_iso():
    """本地时间字符串,与历史表 datetime('now','localtime') 风格一致。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _insert_run(task_name, started, finished, duration_ms, status, affected, error):
    """直接写一条 task_run 记录(供 _record_run 与断点检测等复用)。

    写入本身失败只打日志,绝不向上冒泡。
    """
    conn = None
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO task_run(task_name,started_at,finished_at,duration_ms,"
            "status,affected,error) VALUES(?,?,?,?,?,?,?)",
            (task_name, started, finished, duration_ms, status, affected, error),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001 —— 记录失败不能影响业务
        print(f"[scheduler] task_run 写入失败({task_name}): {type(e).__name__} {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _record_run(task_name, fn):
    """执行 fn() 并把结果落 task_run 表;吞掉 fn 的异常(仅日志),保证不影响调用方。

    返回 (result, status, error):fn() 的返回值(失败为 None) / 'ok'|'fail' /
    失败描述 "ExcType: msg"(成功为 None)。
    """
    started = _now_iso()
    t0 = time.monotonic()
    result, status, error, affected = None, "ok", None, None
    try:
        result = fn()
        if isinstance(result, bool):  # bool 不算条数,转 int 后再判
            result = int(result)
        if isinstance(result, int):
            affected = result
    except Exception as e:  # noqa: BLE001 —— 后台任务必须兜住一切
        status = "fail"
        error = f"{type(e).__name__}: {e}"
    finished = _now_iso()
    duration_ms = int((time.monotonic() - t0) * 1000)
    _insert_run(task_name, started, finished, duration_ms, status, affected, error)
    return result, status, error


def _default_sync():
    # 延迟导入,避免模块级引入抓取层(其可能触发 ssl 上下文构建)
    from backend.datasource.fund_list_sync import sync
    return sync()


def _fund_list_count():
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM fund_list").fetchone()[0]
    finally:
        conn.close()


def _safe_sync(sync_fn):
    """执行一次同步,吞掉所有异常(仅日志),保证不影响调用方。结果落 task_run。"""
    n, status, error = _record_run("fund_list_sync", sync_fn)
    if status == "ok":
        print(f"[scheduler] 全量列表同步完成,共 {n} 只基金。")
    else:
        print(f"[scheduler] 全量列表同步失败(不影响服务): {error}")


def maybe_bootstrap_sync(seed_count=None, sync_fn=None, background=True):
    """启动时判定是否需要拉全量列表。

    当 fund_list 行数 <= 种子数(仍是初始态)时触发一次同步。
    background=True 时在 daemon 线程里跑(不阻塞服务启动);
    False 时同步执行(测试用)。返回是否触发了同步。
    """
    seed_count = len(SEED_FUNDS) if seed_count is None else seed_count
    sync_fn = sync_fn or _default_sync
    if _fund_list_count() > seed_count:
        return False  # 已有全量数据,无需再拉
    if background:
        threading.Thread(
            target=_safe_sync, args=(sync_fn,), name="bootstrap-sync", daemon=True
        ).start()
    else:
        _safe_sync(sync_fn)
    return True


def start_periodic_sync(interval_days=7, sync_fn=None):
    """启动定时刷新 daemon 线程,返回该线程。"""
    sync_fn = sync_fn or _default_sync
    interval = interval_days * 86400

    def _loop():
        while True:
            time.sleep(interval)
            _safe_sync(sync_fn)

    t = threading.Thread(target=_loop, name="periodic-sync", daemon=True)
    t.start()
    return t


def _refresh_holdings_nav():
    """对当前持仓的基金批量回填收盘官方净值。返回成功数。"""
    from backend.datasource.akshare_nav import refresh_nav
    conn = get_conn()
    try:
        codes = [r[0] for r in conn.execute("SELECT DISTINCT fund_code FROM holding")]
        if not codes:
            return 0
        return refresh_nav(conn, codes)
    finally:
        conn.close()


def _safe_nav_refresh(nav_fn):
    n, status, error = _record_run("nav_refresh", nav_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 收盘净值回填完成,更新 {n} 只持仓基金。")
    else:
        print(f"[scheduler] 收盘净值回填失败(不影响服务): {error}")


def start_nav_refresh(interval_hours=12, nav_fn=None, run_now=True):
    """启动收盘官方净值定时回填 daemon 线程,返回该线程。

    run_now=True 时先立即回填一次(启动即补齐历史持仓的官方净值),
    之后每 interval_hours 刷新一次。全程吞异常,失败仅日志。
    """
    nav_fn = nav_fn or _refresh_holdings_nav
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _safe_nav_refresh(nav_fn)
        while True:
            time.sleep(interval)
            _safe_nav_refresh(nav_fn)

    t = threading.Thread(target=_loop, name="nav-refresh", daemon=True)
    t.start()
    return t


# ---- 盘中估值:后台定时刷新，业务层只读缓存（M6） ----

def _refresh_holdings_quotes():
    """对当前持仓的基金批量刷新盘中估值。返回成功数。"""
    from backend.datasource.fundgz import refresh_quotes
    conn = get_conn()
    try:
        codes = [r[0] for r in conn.execute("SELECT DISTINCT fund_code FROM holding")]
        if not codes:
            return 0
        return refresh_quotes(conn, codes)
    finally:
        conn.close()


def _refresh_one_quote(code):
    """刷新单只基金估值（供新增持仓时补空窗）。"""
    from backend.datasource.fundgz import refresh_quotes
    conn = get_conn()
    try:
        return refresh_quotes(conn, [code])
    finally:
        conn.close()


def _safe_quote_refresh(quote_fn):
    n, status, error = _record_run("quote_refresh", quote_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 盘中估值刷新完成,更新 {n} 只持仓基金。")
    else:
        print(f"[scheduler] 盘中估值刷新失败(不影响服务): {error}")


def start_quote_refresh(interval_seconds=60, quote_fn=None, run_now=True):
    """启动盘中估值定时刷新 daemon 线程,返回该线程。

    业务层(list_holdings)只读缓存,估值由此后台线程写入,不再在请求路径现拉。
    """
    quote_fn = quote_fn or _refresh_holdings_quotes

    def _loop():
        if run_now:
            _safe_quote_refresh(quote_fn)
        while True:
            time.sleep(interval_seconds)
            _safe_quote_refresh(quote_fn)

    t = threading.Thread(target=_loop, name="quote-refresh", daemon=True)
    t.start()
    return t


def trigger_quote_for(code, one_fn=None):
    """后台拉取单只基金估值(不阻塞调用方),供新增持仓补空窗。返回线程。"""
    one_fn = one_fn or _refresh_one_quote

    def _run():
        _, status, error = _record_run("quote_one", lambda: one_fn(code))
        if status != "ok":
            print(f"[scheduler] 新增持仓估值拉取失败 {code}: {error}")

    t = threading.Thread(target=_run, name=f"quote-one-{code}", daemon=True)
    t.start()
    return t


# ---- 历史净值序列:后台日更，走势图用（M7） ----

def _refresh_holdings_history():
    """对当前持仓的基金批量刷新历史净值序列。返回成功数。"""
    from backend.datasource.nav_history import refresh_nav_history
    conn = get_conn()
    try:
        codes = [r[0] for r in conn.execute("SELECT DISTINCT fund_code FROM holding")]
        if not codes:
            return 0
        return refresh_nav_history(conn, codes)
    finally:
        conn.close()


def _refresh_one_history(code):
    from backend.datasource.nav_history import refresh_nav_history
    conn = get_conn()
    try:
        return refresh_nav_history(conn, [code])
    finally:
        conn.close()


def _safe_history_refresh(hist_fn):
    n, status, error = _record_run("history_refresh", hist_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 历史净值刷新完成,更新 {n} 只持仓基金。")
    else:
        print(f"[scheduler] 历史净值刷新失败(不影响服务): {error}")


def start_history_refresh(interval_hours=24, hist_fn=None, run_now=True):
    """启动历史净值序列定时刷新 daemon 线程(日更),返回该线程。"""
    hist_fn = hist_fn or _refresh_holdings_history
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _safe_history_refresh(hist_fn)
        while True:
            time.sleep(interval)
            _safe_history_refresh(hist_fn)

    t = threading.Thread(target=_loop, name="history-refresh", daemon=True)
    t.start()
    return t


def trigger_history_for(code, one_fn=None):
    """后台拉取单只基金历史序列(不阻塞),供新增持仓补空窗。返回线程。"""
    one_fn = one_fn or _refresh_one_history

    def _run():
        _, status, error = _record_run("history_one", lambda: one_fn(code))
        if status != "ok":
            print(f"[scheduler] 新增持仓历史拉取失败 {code}: {error}")

    t = threading.Thread(target=_run, name=f"history-one-{code}", daemon=True)
    t.start()
    return t


# ---- 基金基本面:后台日更，详情页用（M8-B） ----

def _profile_target_codes(conn):
    """日更目标 = 当前持仓基金 ∪ 已被查过详情(fund_profile 已有记录)的基金。

    「被查基金」以 fund_profile 是否已有记录为准 —— 详情页首次访问会低频
    按需抓取入库(见 backend/api/fund_detail.py::_ensure_cached),之后即
    进入这里的日更范围,无需额外记录「浏览历史」表。
    """
    holding_codes = {r[0] for r in conn.execute("SELECT DISTINCT fund_code FROM holding")}
    profile_codes = {r[0] for r in conn.execute("SELECT DISTINCT fund_code FROM fund_profile")}
    return sorted(holding_codes | profile_codes)


def _refresh_tracked_profiles():
    """对持仓/被查基金批量刷新基本面(profile)。返回成功数。"""
    from backend.datasource.fund_profile import refresh_profile
    conn = get_conn()
    try:
        codes = _profile_target_codes(conn)
        if not codes:
            return 0
        return refresh_profile(conn, codes)
    finally:
        conn.close()


def _safe_profile_refresh(profile_fn):
    n, status, error = _record_run("profile_refresh", profile_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 基本面刷新完成,更新 {n} 只基金。")
    else:
        print(f"[scheduler] 基本面刷新失败(不影响服务): {error}")


def start_profile_refresh(interval_hours=24, profile_fn=None, run_now=False):
    """启动基金基本面定时刷新 daemon 线程(日更),返回该线程。

    与 start_history_refresh 同构,但 run_now 默认 False —— 基本面数据变化
    比净值更慢,启动即拉非必要;首次访问由 fund_detail.py 的按需抓取兜底。
    """
    profile_fn = profile_fn or _refresh_tracked_profiles
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _safe_profile_refresh(profile_fn)
        while True:
            time.sleep(interval)
            _safe_profile_refresh(profile_fn)

    t = threading.Thread(target=_loop, name="profile-refresh", daemon=True)
    t.start()
    return t


# ---- 净值断点检测:持仓基金净值连续缺失即告警(M9-C) ----

NAV_GAP_THRESHOLD_DAYS = 5  # 自然日,覆盖周末;max(nav_date) 距今超过即视为断点


def _push_nav_gap_notifications(stale_codes):
    """对持有断点基金的 user 推送站内通知(M9-D)。

    去重:同一 user + fund_code + nav_gap 若已有未读通知则跳过,避免每次巡检
    都新增一条。通知本身写入失败只日志,不影响检测。
    """
    if not stale_codes:
        return
    conn = None
    try:
        conn = get_conn()
        for code in stale_codes:
            uids = [r[0] for r in conn.execute(
                "SELECT DISTINCT user_id FROM holding WHERE fund_code=?", (code,))]
            for uid in uids:
                exists = conn.execute(
                    "SELECT 1 FROM notification WHERE user_id=? AND fund_code=? "
                    "AND kind='nav_gap' AND read_at IS NULL",
                    (uid, code),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO notification(user_id,fund_code,kind,message,created_at) "
                    "VALUES(?,?,?,?,datetime('now','localtime'))",
                    (uid, code, "nav_gap",
                     f"{code} 净值已连续缺失,抓取可能异常,请查看系统状态"),
                )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 站内通知写入失败: {type(e).__name__} {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _detect_nav_gaps(threshold_days=NAV_GAP_THRESHOLD_DAYS):
    """检测持仓基金净值断点:max(nav_date) 距今 > threshold_days 或无记录。

    检测结果写入 task_run(task_name='nav_gap_check'):有缺失记 fail +
    error=缺失代码列表;无缺失记 ok。检测本身异常也只记 fail,不抛。
    返回缺失基金数。
    """
    started = _now_iso()
    t0 = time.monotonic()
    status, error, stale = "ok", None, []
    conn = None
    try:
        conn = get_conn()
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT fund_code FROM holding")]
        if codes:
            today = datetime.now().date()
            cutoff = today - timedelta(days=threshold_days)
            placeholders = ",".join("?" * len(codes))
            rows = conn.execute(
                f"SELECT fund_code, MAX(nav_date) FROM fund_nav_history "
                f"WHERE fund_code IN ({placeholders}) GROUP BY fund_code",
                codes,
            ).fetchall()
            last_by_code = {r[0]: r[1] for r in rows}
            for code in codes:
                last = last_by_code.get(code)
                if not last:  # 从无净值记录
                    stale.append(code)
                    continue
                try:
                    d = datetime.strptime(last, "%Y-%m-%d").date()
                except ValueError:  # 日期格式异常,跳过不误报
                    continue
                if d < cutoff:
                    stale.append(code)
    except Exception as e:  # noqa: BLE001 —— 检测失败不能影响服务
        status = "fail"
        error = f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    affected = len(stale)
    if stale:
        status = "fail"
        tail = " ..." if len(stale) > 50 else ""
        error = "净值断点: " + ", ".join(stale[:50]) + tail
    finished = _now_iso()
    duration_ms = int((time.monotonic() - t0) * 1000)
    _insert_run("nav_gap_check", started, finished, duration_ms, status, affected, error)
    if stale:
        print(f"[scheduler] 净值断点检测告警: {len(stale)} 只持仓基金净值缺失 "
              f"({', '.join(stale[:10])})")
        _push_nav_gap_notifications(stale)
    return len(stale)


def start_nav_gap_check(interval_hours=24, run_now=True):
    """启动净值断点检测 daemon 线程(日更),返回该线程。

    检测持仓基金 nav_history 的 max(nav_date) 距今是否超过阈值(默认 5 天,
    覆盖周末)。有缺失记 task_run fail + error=代码列表,无缺失记 ok;
    前端「系统状态」页据此标红告警。
    """
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _detect_nav_gaps()
        while True:
            time.sleep(interval)
            _detect_nav_gaps()

    t = threading.Thread(target=_loop, name="nav-gap-check", daemon=True)
    t.start()
    return t
