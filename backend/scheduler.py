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


# ---- 抓取失败重试 + 连续失败告警(M10C) ----
DEFAULT_RETRIES = 2        # _safe_* 失败后重试次数(默认 2,间隔递增)
RETRY_BASE_DELAY = 10     # 重试间隔基数(秒):第 i 次重试 sleep RETRY_BASE_DELAY * i
SYNC_ALERT_THRESHOLD = 3  # 同任务连续失败超此阈值即给持仓 user 推 sync_alert


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


def _retry_attempts(task_name, fn, retries, base_delay=RETRY_BASE_DELAY,
                    sleep=time.sleep):
    """执行至多 retries 次重试(不含首次),间隔递增,每次落新 task_run 行;命中 ok 即停。

    间隔 = base_delay * i(i=1..retries)。sleep 可注入,便于测试。返回最终
    (result, status, error) —— 全失败时为 (None, "fail", error)。
    """
    result, status, error = None, "fail", None
    for i in range(1, retries + 1):
        sleep(base_delay * i)
        result, status, error = _record_run(task_name, fn)
        if status == "ok":
            break
    return result, status, error


def _run_with_retries(task_name, fn, retries=DEFAULT_RETRIES,
                      base_delay=RETRY_BASE_DELAY, sleep=time.sleep):
    """初始尝试 + 失败重试,每次尝试各落一行 task_run。返回最终 (result,status,error)。

    同步执行(含 sleep);主要供测试直接驱动(重试成功/失败、间隔递增)。
    """
    result, status, error = _record_run(task_name, fn)
    if status != "ok" and retries:
        result, status, error = _retry_attempts(
            task_name, fn, retries, base_delay, sleep)
    return result, status, error


def _maybe_retry(task_name, fn, retries):
    """失败后在独立 daemon 线程里按间隔递增重试 retries 次,不阻塞调用方(M10C)。"""
    if not retries:
        return
    threading.Thread(
        target=_retry_attempts,
        args=(task_name, fn, retries),
        name=f"retry-{task_name}",
        daemon=True,
    ).start()


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


def _safe_sync(sync_fn, retries=DEFAULT_RETRIES):
    """执行一次同步,吞掉所有异常(仅日志),保证不影响调用方。结果落 task_run。

    失败时在独立线程按间隔递增重试 retries 次,每次重试落新 task_run 行(M10C)。
    """
    n, status, error = _record_run("fund_list_sync", sync_fn)
    if status == "ok":
        print(f"[scheduler] 全量列表同步完成,共 {n} 只基金。")
    else:
        print(f"[scheduler] 全量列表同步失败(不影响服务): {error}")
        _maybe_retry("fund_list_sync", sync_fn, retries)


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


def _safe_nav_refresh(nav_fn, retries=DEFAULT_RETRIES):
    n, status, error = _record_run("nav_refresh", nav_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 收盘净值回填完成,更新 {n} 只持仓基金。")
    else:
        print(f"[scheduler] 收盘净值回填失败(不影响服务): {error}")
        _maybe_retry("nav_refresh", nav_fn, retries)


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

def _quote_target_codes(conn):
    """盘中估值刷新目标 = 持仓基金 ∪ 今日已采时序基金 ∪ 被查过详情基金(profile)。

    今日已采基金取自 fund_quote_tick 当日记录 —— 用户点开过的基金会被持续采样,
    次日按 quote_date 自然清零重新累计。被查详情基金取自 fund_profile 已有记录
    (详情首次访问由 fund_detail._ensure_cached 兜底入库)。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    codes = set()
    codes.update(r[0] for r in conn.execute("SELECT DISTINCT fund_code FROM holding"))
    try:  # fund_quote_tick 表理论上由 init_db 建好,旧库兜底
        codes.update(r[0] for r in conn.execute(
            "SELECT DISTINCT fund_code FROM fund_quote_tick WHERE quote_date=?", (today,)))
    except Exception:  # noqa: BLE001 —— 表缺失不阻断,降级为持仓+profile
        pass
    codes.update(r[0] for r in conn.execute("SELECT DISTINCT fund_code FROM fund_profile"))
    return sorted(codes)


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


def _refresh_tracked_quotes():
    """对持仓 ∪ 今日已采 ∪ 被查详情基金批量刷新盘中估值。返回成功数。

    比 _refresh_holdings_quotes 范围更广:市场页点开过的基金也会被持续采样,
    让其详情页折线在盘中有数据延伸。
    """
    from backend.datasource.fundgz import refresh_quotes
    conn = get_conn()
    try:
        codes = _quote_target_codes(conn)
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


def _safe_quote_refresh(quote_fn, retries=DEFAULT_RETRIES):
    n, status, error = _record_run("quote_refresh", quote_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 盘中估值刷新完成,更新 {n} 只基金。")
    else:
        print(f"[scheduler] 盘中估值刷新失败(不影响服务): {error}")
        _maybe_retry("quote_refresh", quote_fn, retries)


def start_quote_refresh(interval_seconds=60, quote_fn=None, run_now=True):
    """启动盘中估值定时刷新 daemon 线程,返回该线程。

    业务层(list_holdings)只读缓存,估值由此后台线程写入,不在请求路径现拉。
    交易时段门控:非交易时段(周末/夜间)跳过抓取,不发起任何网络请求,保留
    60s 心跳节奏;fundgz 非交易时段返回上一交易日定格值,采了也无意义且守合规。
    """
    from backend.datasource import fundgz
    quote_fn = quote_fn or _refresh_tracked_quotes

    def _loop():
        if run_now and fundgz.is_market_open():
            _safe_quote_refresh(quote_fn)
        while True:
            time.sleep(interval_seconds)
            if not fundgz.is_market_open():
                continue
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


# ---- 大盘指数:后台定时刷新，业务层只读缓存（P1a） ----

def _refresh_indices():
    """刷新 4 大指数最新行情。返回成功写入条数。"""
    from backend.datasource.market_index import refresh_indices
    conn = get_conn()
    try:
        return refresh_indices(conn)
    finally:
        conn.close()


def _safe_index_refresh(index_fn, retries=DEFAULT_RETRIES):
    n, status, error = _record_run("index_refresh", index_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 大盘指数刷新完成,更新 {n} 个指数。")
    else:
        print(f"[scheduler] 大盘指数刷新失败(不影响服务): {error}")
        _maybe_retry("index_refresh", index_fn, retries)


def start_index_refresh(interval_seconds=60, index_fn=None, run_now=True):
    """启动大盘指数定时刷新 daemon 线程,返回该线程。

    与 start_quote_refresh 同构,但 run_now 时**无条件先拉一次**——指数在收盘后
    接口返回的是最新收盘价(有展示价值),故启动即拉以填充「大盘指数条」;之后
    仅在交易时段每 interval_seconds 刷新,非交易时段跳过抓取(守合规、省请求)。
    """
    from backend.datasource import fundgz
    index_fn = index_fn or _refresh_indices

    def _loop():
        if run_now:
            _safe_index_refresh(index_fn)
        while True:
            time.sleep(interval_seconds)
            if not fundgz.is_market_open():
                continue
            _safe_index_refresh(index_fn)

    t = threading.Thread(target=_loop, name="index-refresh", daemon=True)
    t.start()
    return t


# ---- 基金排行榜:后台日更，排行页用（P1b） ----

def _refresh_rank():
    """抓 6 大类 × 5 区间榜单写 fund_rank。返回写入总行数。"""
    from backend.datasource.fund_rank import refresh_rank
    conn = get_conn()
    try:
        return refresh_rank(conn)
    finally:
        conn.close()


def _safe_rank_refresh(rank_fn, retries=DEFAULT_RETRIES):
    n, status, error = _record_run("rank_refresh", rank_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 排行榜刷新完成,更新 {n} 行。")
    else:
        print(f"[scheduler] 排行榜刷新失败(不影响服务): {error}")
        _maybe_retry("rank_refresh", rank_fn, retries)


def start_rank_refresh(interval_hours=24, rank_fn=None, run_now=True):
    """启动基金排行榜定时刷新 daemon 线程(日更),返回该线程。

    排行数据变化慢(日更即可),run_now 启动即拉一次填充榜单;之后每 interval_hours
    刷新。全程吞异常、落 task_run,失败不影响服务。
    """
    rank_fn = rank_fn or _refresh_rank
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _safe_rank_refresh(rank_fn)
        while True:
            time.sleep(interval)
            _safe_rank_refresh(rank_fn)

    t = threading.Thread(target=_loop, name="rank-refresh", daemon=True)
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


def _safe_history_refresh(hist_fn, retries=DEFAULT_RETRIES):
    n, status, error = _record_run("history_refresh", hist_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 历史净值刷新完成,更新 {n} 只持仓基金。")
    else:
        print(f"[scheduler] 历史净值刷新失败(不影响服务): {error}")
        _maybe_retry("history_refresh", hist_fn, retries)


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


def _safe_profile_refresh(profile_fn, retries=DEFAULT_RETRIES):
    n, status, error = _record_run("profile_refresh", profile_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 基本面刷新完成,更新 {n} 只基金。")
    else:
        print(f"[scheduler] 基本面刷新失败(不影响服务): {error}")
        _maybe_retry("profile_refresh", profile_fn, retries)


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


# ---- 基金重仓股:后台日更 + 首访兜底，详情页 F10 用（P2） ----

def _refresh_tracked_holdings():
    """对持仓 ∪ 被查基金批量刷新 Top10 重仓股。返回有数据的基金数。"""
    from backend.datasource.fund_holdings import refresh_holdings
    conn = get_conn()
    try:
        codes = _profile_target_codes(conn)
        if not codes:
            return 0
        return refresh_holdings(conn, codes)
    finally:
        conn.close()


def _safe_holdings_refresh(holdings_fn, retries=DEFAULT_RETRIES):
    n, status, error = _record_run("holdings_refresh", holdings_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 重仓股刷新完成,更新 {n} 只基金。")
    else:
        print(f"[scheduler] 重仓股刷新失败(不影响服务): {error}")
        _maybe_retry("holdings_refresh", holdings_fn, retries)


def start_holdings_refresh(interval_hours=24, holdings_fn=None, run_now=False):
    """启动重仓股定时刷新 daemon 线程(日更),返回该线程。

    与 start_profile_refresh 同构:run_now 默认 False —— 持仓明细按季度披露、变化慢,
    启动即拉非必要;详情页首次访问由 fund_detail._ensure_cached 兜底抓一次。
    """
    holdings_fn = holdings_fn or _refresh_tracked_holdings
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _safe_holdings_refresh(holdings_fn)
        while True:
            time.sleep(interval)
            _safe_holdings_refresh(holdings_fn)

    t = threading.Thread(target=_loop, name="holdings-refresh", daemon=True)
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


# ---- Session 过期清理:日更 daemon(M9-E) ----

def _safe_session_purge(retries=DEFAULT_RETRIES):
    from backend import auth
    purge_fn = auth.purge_expired_sessions
    n, status, error = _record_run("session_purge", purge_fn)
    if status == "ok":
        if n:
            print(f"[scheduler] 过期 session 清理完成,删除 {n} 条。")
    else:
        print(f"[scheduler] 过期 session 清理失败(不影响服务): {error}")
        _maybe_retry("session_purge", purge_fn, retries)


def start_session_purge(interval_hours=24, run_now=True):
    """启动过期 session 清理 daemon(日更),返回该线程。

    token 默认 30 天有效,过期行不再被使用但留库膨胀。日更清理把
    expires_at <= now 的 session 删除,结果落 task_run 供可观测。
    """
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _safe_session_purge()
        while True:
            time.sleep(interval)
            _safe_session_purge()

    t = threading.Thread(target=_loop, name="session-purge", daemon=True)
    t.start()
    return t


# ---- 盘中时序清理:日更删除 7 天前 fund_quote_tick 旧数据,防膨胀 ----

def _safe_tick_purge(retries=DEFAULT_RETRIES):
    def _purge():
        conn = get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM fund_quote_tick "
                "WHERE quote_date < date('now','localtime','-7 days')"
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    n, status, error = _record_run("tick_purge", _purge)
    if status == "ok":
        if n:
            print(f"[scheduler] 盘中时序清理完成,删除 {n} 行。")
    else:
        print(f"[scheduler] 盘中时序清理失败(不影响服务): {error}")
        _maybe_retry("tick_purge", _purge, retries)


def start_tick_purge(interval_hours=24, run_now=True):
    """启动盘中时序清理 daemon(日更),返回该线程。

    fund_quote_tick 每基金每日约 300 行(交易分钟数),7 天前旧数据无展示价值
    (前端只查今日 quote_date),日更删除防表膨胀。表缺失(tick 未建)时降级。
    """
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _safe_tick_purge()
        while True:
            time.sleep(interval)
            _safe_tick_purge()

    t = threading.Thread(target=_loop, name="tick-purge", daemon=True)
    t.start()
    return t


# ---- 连续失败告警:抓取任务连续失败超阈值即给持仓 user 推 sync_alert(M10C) ----

def _consecutive_fail_count(task_name, conn=None):
    """从 task_run 末尾数连续 fail 行数(遇 ok / 空即停),上限取阈值。

    只取最近 SYNC_ALERT_THRESHOLD 行,从最新向旧数连续 fail,遇到首个 ok 即停:
    最近一次为 ok 即视为已恢复(0)。conn 可注入复用(供 API 查询),不传则自管连接。
    """
    own = conn is None
    conn = None if own else conn
    try:
        if own:
            conn = get_conn()
        rows = conn.execute(
            "SELECT status FROM task_run WHERE task_name=? "
            "ORDER BY id DESC LIMIT ?",
            (task_name, SYNC_ALERT_THRESHOLD),
        ).fetchall()
        count = 0
        for r in rows:
            if r[0] == "fail":
                count += 1
            else:
                break  # 遇 ok,连续失败被打断
        return count
    except Exception:  # noqa: BLE001 —— 计数失败按 0 处理,不抛
        return 0
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _push_sync_alerts(task_name):
    """连续失败超阈值时,给持仓受影响 user 推 kind=sync_alert 站内通知。

    去重逻辑仿 _push_nav_gap_notifications:同 user + 任务名(存 fund_code 列)+
    kind='sync_alert' + 未读(read_at IS NULL)则跳过,避免每次巡检刷屏。
    受影响基金取该 user 的持仓 fund_code,合并写入 message。写入失败只日志,
    不抛。返回本次新写入条数。
    """
    fails = _consecutive_fail_count(task_name)
    if fails < SYNC_ALERT_THRESHOLD:
        return 0
    conn = None
    pushed = 0
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT user_id, fund_code FROM holding "
            "WHERE user_id IS NOT NULL AND fund_code IS NOT NULL "
            "ORDER BY user_id, fund_code"
        ).fetchall()
        by_user = {}
        for r in rows:
            by_user.setdefault(r[0], []).append(r[1])
        for uid, codes in by_user.items():
            exists = conn.execute(
                "SELECT 1 FROM notification WHERE user_id=? AND fund_code=? "
                "AND kind='sync_alert' AND read_at IS NULL",
                (uid, task_name),
            ).fetchone()
            if exists:
                continue
            shown = codes[:20]
            tail = "" if len(codes) <= 20 else f" 等 {len(codes)} 只"
            msg = (f"「{task_name}」连续失败 {fails} 次,受影响基金: "
                   f"{', '.join(shown)}{tail}")
            conn.execute(
                "INSERT INTO notification(user_id,fund_code,kind,message,created_at) "
                "VALUES(?,?,?,?,datetime('now','localtime'))",
                (uid, task_name, "sync_alert", msg),
            )
            pushed += 1
        conn.commit()
    except Exception as e:  # noqa: BLE001 —— 推送失败不能影响业务
        print(f"[scheduler] sync_alert 推送失败({task_name}): "
              f"{type(e).__name__} {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return pushed


def _dispatch_alerts():
    """巡检所有抓取任务,对连续失败超阈值的给持仓 user 推 sync_alert。

    task_run 表里出现过的任务名各调一次 _push_sync_alerts(内部自判阈值 +
    去重)。任务列表读取失败只日志、跳过本轮。
    """
    conn = None
    names = []
    try:
        conn = get_conn()
        names = [r[0] for r in conn.execute(
            "SELECT DISTINCT task_name FROM task_run").fetchall()]
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 告警巡检任务列表读取失败: {type(e).__name__} {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    for name in names:
        _push_sync_alerts(name)


def start_alert_dispatcher(interval_hours=6, run_now=True):
    """启动连续失败告警巡检 daemon(默认 6h),返回该线程。

    定期扫所有抓取任务,对连续失败超阈值的给持仓 user 推 sync_alert 站内通知
    (同 user+任务未读告警去重,仿 _push_nav_gap_notifications)。仅应用内
    notification,不做手机/Web Push(红线)。
    """
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _dispatch_alerts()
        while True:
            time.sleep(interval)
            _dispatch_alerts()

    t = threading.Thread(target=_loop, name="alert-dispatcher", daemon=True)
    t.start()
    return t


# ---- 移动止盈:日更 peak_nav + 回撤触发通知(PRD-07) ----

def _check_trailing_stops():
    """巡检设了移动止盈的持仓:更新 peak_nav(只增不减),回撤到线即推通知。

    当前价取 fund_quote.nav(收盘)回落 gsz(盘中)。触发用更新前的旧 peak:
    先判断触发再更新 peak,避免本次新高被记为触发。通知去重
    (同 user+fund+trailing_stop_hit 未读跳过)。返回触发条数。
    """
    conn = None
    triggered = 0
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT h.id, h.user_id, h.fund_code, h.trailing_stop_pct, h.peak_nav, "
            "q.nav, q.gsz FROM holding h "
            "LEFT JOIN fund_quote q ON q.fund_code = h.fund_code "
            "WHERE h.trailing_stop_pct IS NOT NULL AND h.trailing_stop_pct > 0"
        ).fetchall()
        for r in rows:
            cur = r["nav"] if r["nav"] is not None else r["gsz"]
            if cur is None:
                continue
            peak = r["peak_nav"]
            trailing = r["trailing_stop_pct"]
            if peak is not None and peak > 0:
                line = peak * (1 - trailing / 100)
                if cur <= line:
                    exists = conn.execute(
                        "SELECT 1 FROM notification WHERE user_id=? AND fund_code=? "
                        "AND kind='trailing_stop_hit' AND read_at IS NULL",
                        (r["user_id"], r["fund_code"]),
                    ).fetchone()
                    if not exists:
                        conn.execute(
                            "INSERT INTO notification(user_id,fund_code,kind,message,created_at) "
                            "VALUES(?,?,?,?,datetime('now','localtime'))",
                            (r["user_id"], r["fund_code"], "trailing_stop_hit",
                             f"{r['fund_code']} 从高点 {round(peak, 4)} "
                             f"回撤 {trailing}% 触发移动止盈"),
                        )
                        triggered += 1
            if peak is None or cur > peak:
                conn.execute(
                    "UPDATE holding SET peak_nav=? WHERE id=?", (cur, r["id"]))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 移动止盈巡检失败: {type(e).__name__} {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return triggered


def start_trailing_stop_check(interval_hours=1, run_now=True):
    """启动移动止盈巡检 daemon(默认 1h),返回该线程。

    更新持仓 peak_nav(只增不减),回撤到 trailing_stop_pct 即推 trailing_stop_hit
    站内通知(去重)。仅应用内 notification,不做 Web Push(红线)。
    """
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _check_trailing_stops()
        while True:
            time.sleep(interval)
            _check_trailing_stops()

    t = threading.Thread(target=_loop, name="trailing-stop-check", daemon=True)
    t.start()
    return t


# ---- 定投计划:到点站内提醒 + next_date 滚动(PRD-04 P1) ----

def _check_dca_plans():
    """巡检到期定投计划:next_date<=today 推 dca_due 通知,next_date 滚到下一期。

    通知去重(同 user+fund+dca_due 未读跳过)。返回触发条数。
    """
    from datetime import date as _date, timedelta as _td
    conn = None
    triggered = 0
    try:
        conn = get_conn()
        today = _date.today().isoformat()
        rows = conn.execute(
            "SELECT id, user_id, fund_code, per_amount, freq, invest_day, next_date "
            "FROM dca_plan WHERE active=1 AND next_date <= ?", (today,)
        ).fetchall()
        for r in rows:
            exists = conn.execute(
                "SELECT 1 FROM notification WHERE user_id=? AND fund_code=? "
                "AND kind='dca_due' AND read_at IS NULL",
                (r["user_id"], r["fund_code"]),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO notification(user_id,fund_code,kind,message,created_at) "
                    "VALUES(?,?,?,?,datetime('now','localtime'))",
                    (r["user_id"], r["fund_code"], "dca_due",
                     f"该给 {r['fund_code']} 定投 ¥{r['per_amount']} 了"),
                )
                triggered += 1
            try:
                cur = _date.fromisoformat(r["next_date"])
            except (ValueError, TypeError):
                continue
            freq, inv = r["freq"], r["invest_day"]
            if freq == "monthly":
                y, m = cur.year, cur.month
                m += 1
                if m > 12:
                    y, m = y + 1, 1
                try:
                    nxt = _date(y, m, inv)
                except ValueError:
                    nxt = _date(y, m, 28)
            elif freq == "biweekly":
                nxt = cur + _td(days=14)
            else:
                nxt = cur + _td(days=7)
            conn.execute("UPDATE dca_plan SET next_date=? WHERE id=?",
                         (nxt.isoformat(), r["id"]))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 定投计划巡检失败: {type(e).__name__} {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return triggered


def start_dca_plan_check(interval_hours=24, run_now=True):
    """启动定投计划巡检 daemon(默认日更),返回该线程。

    到点(next_date<=today)推 dca_due 站内通知(去重),next_date 滚动下一期。
    仅应用内 notification,不做 Web Push(红线)。
    """
    interval = interval_hours * 3600

    def _loop():
        if run_now:
            _check_dca_plans()
        while True:
            time.sleep(interval)
            _check_dca_plans()

    t = threading.Thread(target=_loop, name="dca-plan-check", daemon=True)
    t.start()
    return t
