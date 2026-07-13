# -*- coding: utf-8 -*-
"""站内通知(M9-D)。

后台巡检(净值断点等)发现异常时,推送站内通知给相关持仓用户;前端轮询未读数
展示角标。按 user_id 隔离,越权访问不生效(读写均带 user_id 校验)。

接口:
  GET  /api/notifications              当前用户未读列表(?all=1 含已读,最近 50)
  POST /api/notifications/{id}/read    标记一条已读
"""
from backend.api._router import Ctx  # noqa: F401  (保持路由约定一致)
from backend.models.db import get_conn

LIST_LIMIT = 50


def _list(uid, all_flag=False):
    conn = get_conn()
    try:
        if all_flag:
            sql = ("SELECT id,fund_code,kind,message,created_at,read_at "
                   "FROM notification WHERE user_id=? ORDER BY id DESC LIMIT ?")
        else:
            sql = ("SELECT id,fund_code,kind,message,created_at,read_at "
                   "FROM notification WHERE user_id=? AND read_at IS NULL "
                   "ORDER BY id DESC LIMIT ?")
        rows = conn.execute(sql, (uid, LIST_LIMIT)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _mark_read(uid, nid):
    """标记一条已读(带 user_id 校验,越权返回 False)。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE notification SET read_at=datetime('now','localtime') "
            "WHERE id=? AND user_id=? AND read_at IS NULL",
            (nid, uid),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def handle_list(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    all_flag = ctx.q("all") in ("1", "true", "yes")
    return {"notifications": _list(ctx.user_id, all_flag)}


def handle_read(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    try:
        nid = int(ctx.params.get("id", 0) or 0)
    except (TypeError, ValueError):
        return (400, {"error": "invalid id"})
    if nid <= 0:
        return (400, {"error": "invalid id"})
    ok = _mark_read(ctx.user_id, nid)
    return (200 if ok else 404, {"ok": ok})


ROUTES = [
    ("GET", "api/notifications", handle_list),
    ("POST", "api/notifications/{id}/read", handle_read),
]
