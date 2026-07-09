# -*- coding: utf-8 -*-
"""进程内轻量调度 —— 全量基金列表的启动自拉与定时刷新。

坚持项目「单进程 + 零第三方依赖」约定:不引入 cron/celery,定时任务用
标准库 threading 的 daemon 线程实现。任何同步失败只打日志、不影响服务。

- maybe_bootstrap_sync(): 启动时若 fund_list 仍是初始种子态,后台拉一次全量。
- start_periodic_sync():  daemon 线程,按周期(默认 7 天)刷新全量列表。
"""
import threading
import time

from backend.models.db import get_conn, SEED_FUNDS


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
    """执行一次同步,吞掉所有异常(仅日志),保证不影响调用方。"""
    try:
        n = sync_fn()
        print(f"[scheduler] 全量列表同步完成,共 {n} 只基金。")
    except Exception as e:  # noqa: BLE001 —— 后台任务必须兜住一切
        print(f"[scheduler] 全量列表同步失败(不影响服务): {type(e).__name__} {e}")


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
    try:
        n = nav_fn()
        if n:
            print(f"[scheduler] 收盘净值回填完成,更新 {n} 只持仓基金。")
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 收盘净值回填失败(不影响服务): {type(e).__name__} {e}")


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
