# -*- coding: utf-8 -*-
"""M9-D 站内通知测试:推送去重 + API 鉴权/隔离/已读 + 断点检测联动。

不发起真实网络/抓取,用临时库隔离。
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from backend import scheduler
from backend.api import notifications, _router
from backend.models import db as db_mod


class _TmpDbMixin:
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _exec(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def _add_holding(self, uid, code):
        self._exec(
            "INSERT INTO holding(user_id,fund_code,hold_amount,created_at) "
            "VALUES(?,?,?,datetime('now'))",
            (uid, code, 1000.0),
        )

    def _add_nav(self, code, days_ago):
        d = (datetime.now().date() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        self._exec(
            "INSERT OR REPLACE INTO fund_nav_history(fund_code,nav_date,nav) VALUES(?,?,?)",
            (code, d, 1.23),
        )

    def _unread(self, uid):
        conn = sqlite3.connect(self.path)
        n = conn.execute(
            "SELECT COUNT(*) FROM notification WHERE user_id=? AND read_at IS NULL",
            (uid,),
        ).fetchone()[0]
        conn.close()
        return n


class TestPushNavGapNotifications(_TmpDbMixin, unittest.TestCase):
    def test_writes_for_holders(self):
        self._add_holding(1, "020608")
        scheduler._push_nav_gap_notifications(["020608"])
        self.assertEqual(self._unread(1), 1)

    def test_no_write_for_non_holders(self):
        # 无人持有 005827 → 不写任何通知
        scheduler._push_nav_gap_notifications(["005827"])
        self.assertEqual(self._unread(1), 0)

    def test_dedup(self):
        self._add_holding(1, "020608")
        scheduler._push_nav_gap_notifications(["020608"])
        scheduler._push_nav_gap_notifications(["020608"])  # 重复调,不增
        self.assertEqual(self._unread(1), 1)

    def test_multi_user_each_notified(self):
        self._add_holding(1, "020608")
        self._add_holding(2, "020608")
        scheduler._push_nav_gap_notifications(["020608"])
        self.assertEqual(self._unread(1), 1)
        self.assertEqual(self._unread(2), 1)


class TestNotificationsApi(_TmpDbMixin, unittest.TestCase):
    def _ctx(self, user_id=None, params=None, **kw):
        return _router.Ctx(query=kw, params=params or {}, user_id=user_id)

    def _call(self, fn, **kw):
        r = fn(self._ctx(**kw))
        return r if isinstance(r, tuple) else (200, r)

    def test_list_requires_login(self):
        code, _ = self._call(notifications.handle_list, user_id=None)
        self.assertEqual(code, 401)

    def test_list_returns_unread_only(self):
        self._add_holding(1, "020608")
        scheduler._push_nav_gap_notifications(["020608"])
        code, obj = self._call(notifications.handle_list, user_id=1)
        self.assertEqual(code, 200)
        self.assertEqual(len(obj["notifications"]), 1)
        self.assertEqual(obj["notifications"][0]["kind"], "nav_gap")

    def test_all_flag_includes_read(self):
        self._add_holding(1, "020608")
        scheduler._push_nav_gap_notifications(["020608"])
        # 标记已读
        code, _ = self._call(notifications.handle_read, user_id=1, params={"id": "1"})
        self.assertEqual(code, 200)
        # 默认(未读)列表为空
        _, obj = self._call(notifications.handle_list, user_id=1)
        self.assertEqual(len(obj["notifications"]), 0)
        # all=1 含已读
        _, obj = self._call(notifications.handle_list, user_id=1, all="1")
        self.assertEqual(len(obj["notifications"]), 1)

    def test_mark_read_cross_user_no_effect(self):
        self._add_holding(1, "020608")
        scheduler._push_nav_gap_notifications(["020608"])  # user1 的通知 id=1
        # user2 试图标记 user1 的通知 → 404,不影响
        code, obj = self._call(notifications.handle_read, user_id=2, params={"id": "1"})
        self.assertEqual(code, 404)
        self.assertFalse(obj["ok"])
        # user1 的通知仍未读
        self.assertEqual(self._unread(1), 1)

    def test_mark_read_invalid_id(self):
        code, _ = self._call(notifications.handle_read, user_id=1, params={"id": "0"})
        self.assertEqual(code, 400)
        code, _ = self._call(notifications.handle_read, user_id=1, params={"id": "abc"})
        self.assertEqual(code, 400)


class TestDetectNavGapsTriggersNotification(_TmpDbMixin, unittest.TestCase):
    def test_gap_triggers_notification(self):
        self._add_holding(1, "020608")
        self._add_nav("020608", 10)  # 断点
        scheduler._detect_nav_gaps()
        self.assertEqual(self._unread(1), 1)

    def test_no_gap_no_notification(self):
        self._add_holding(1, "020608")
        self._add_nav("020608", 1)  # 正常
        scheduler._detect_nav_gaps()
        self.assertEqual(self._unread(1), 0)


if __name__ == "__main__":
    unittest.main()
