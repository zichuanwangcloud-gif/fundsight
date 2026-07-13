# -*- coding: utf-8 -*-
"""抓取任务可观测性:只读查询 task_run 表(M9-A)。

供排查「上次各抓取任务成功了吗 / 耗时多久 / 失败原因」之用,业务层只读,
绝不触发外部抓取(抓取层是唯一对外接口,详见 CLAUDE.md)。需登录(私享级,
任何登录账号均可读,不区分管理员)。

接口:
  GET /api/admin/sync-status   各任务最近一次状态(概览)
  GET /api/admin/sync-runs     最近执行流水(默认 100 条,limit 可调 1-500)
  GET /api/admin/sync-alerts   未恢复失败任务(连续失败超阈值)+ 受影响基金(M10C)
"""
from backend.api._router import Ctx  # noqa: F401  (保持路由约定一致)
from backend.models.db import get_conn
from backend import scheduler

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _list_recent(limit=DEFAULT_LIMIT):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,task_name,started_at,finished_at,duration_ms,status,affected,error "
            "FROM task_run ORDER BY started_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _summary():
    """每个任务最近一次执行状态(按 task_name 取 max(id))。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT t.task_name,t.status,t.started_at,t.duration_ms,t.affected,t.error "
            "FROM task_run t JOIN ("
            "  SELECT task_name, MAX(id) AS max_id FROM task_run GROUP BY task_name"
            ") m ON m.max_id = t.id "
            "ORDER BY t.task_name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _parse_limit(raw):
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return n if 1 <= n <= MAX_LIMIT else DEFAULT_LIMIT


def handle_summary(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    return {"tasks": _summary()}


def handle_list(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    limit = _parse_limit(ctx.q("limit"))
    return {"runs": _list_recent(limit)}


def _list_sync_alerts():
    """未恢复失败任务(连续失败超阈值)+ 受影响基金 + 最近错误,供 admin 告警区。

    阈值与告警推送一致(scheduler.SYNC_ALERT_THRESHOLD):任务最近阈值次执行全
    fail 即视为未恢复;出现 ok 即恢复、从告警区消失。受影响基金取全部持仓
    fund_code(去重排序)。
    """
    conn = get_conn()
    try:
        names = [r[0] for r in conn.execute(
            "SELECT DISTINCT task_name FROM task_run").fetchall()]
        out = []
        for name in names:
            fails = scheduler._consecutive_fail_count(name, conn)
            if fails < scheduler.SYNC_ALERT_THRESHOLD:
                continue
            codes = [r[0] for r in conn.execute(
                "SELECT DISTINCT fund_code FROM holding "
                "WHERE fund_code IS NOT NULL ORDER BY fund_code"
            ).fetchall()]
            last = conn.execute(
                "SELECT error, started_at FROM task_run WHERE task_name=? "
                "ORDER BY id DESC LIMIT 1", (name,)
            ).fetchone()
            out.append({
                "task_name": name,
                "consecutive_fails": fails,
                "affected_funds": codes,
                "last_error": last[0] if last else None,
                "last_started_at": last[1] if last else None,
            })
        return out
    finally:
        conn.close()


def handle_alerts(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    return {"alerts": _list_sync_alerts()}


ROUTES = [
    ("GET", "api/admin/sync-status", handle_summary),
    ("GET", "api/admin/sync-runs", handle_list),
    ("GET", "api/admin/sync-alerts", handle_alerts),
]
