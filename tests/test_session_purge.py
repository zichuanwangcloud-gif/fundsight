# -*- coding: utf-8 -*-
"""M9-E 鉴权加固测试:过期 session 清理 + Cookie Secure(env 控制)。

不发起真实网络/抓取,用临时库隔离。
"""
import os
import sqlite3
import tempfile
import unittest

from backend import auth, scheduler
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

    def _add_session(self, token, uid, days):
        """插一条 session,expires_at = now + days 天(days 为负即过期)。"""
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO session(token,user_id,created_at,expires_at) "
            "VALUES(?,?,datetime('now','localtime'),datetime('now','localtime',?))",
            (token, uid, f"{days:+d} days"),
        )
        conn.commit()
        conn.close()


class TestPurgeExpiredSessions(_TmpDbMixin, unittest.TestCase):
    def test_deletes_expired_keeps_valid(self):
        uid = auth.create_user("u1", "pw12345")
        self._add_session("tok_expired", uid, -1)
        self._add_session("tok_valid", uid, 30)
        n = auth.purge_expired_sessions()
        self.assertEqual(n, 1)
        self.assertEqual(auth.get_user_by_token("tok_valid"), uid)
        self.assertIsNone(auth.get_user_by_token("tok_expired"))

    def test_no_expired_returns_zero(self):
        uid = auth.create_user("u1", "pw12345")
        self._add_session("tok", uid, 30)
        self.assertEqual(auth.purge_expired_sessions(), 0)


class TestSafeSessionPurge(_TmpDbMixin, unittest.TestCase):
    def test_writes_task_run(self):
        uid = auth.create_user("u", "pw12345")
        self._add_session("t", uid, -1)
        scheduler._safe_session_purge()
        conn = sqlite3.connect(self.path)
        c = conn.execute(
            "SELECT status,affected FROM task_run WHERE task_name='session_purge'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(c)
        self.assertEqual(c[0], "ok")
        self.assertEqual(c[1], 1)

    def test_no_expired_still_records_ok(self):
        scheduler._safe_session_purge()
        conn = sqlite3.connect(self.path)
        c = conn.execute(
            "SELECT status,affected FROM task_run WHERE task_name='session_purge'"
        ).fetchone()
        conn.close()
        self.assertEqual(c[0], "ok")
        self.assertEqual(c[1], 0)  # purge 返回 0 条,affected=0


class TestStartSessionPurge(unittest.TestCase):
    def test_returns_daemon_thread(self):
        t = scheduler.start_session_purge(interval_hours=24, run_now=False)
        self.assertTrue(t.daemon)
        self.assertTrue(t.is_alive())


class TestCookieSecureFlag(unittest.TestCase):
    """Cookie 的 Secure 标志由 FUNDSIGHT_SECURE_COOKIE 控制(HTTPS 部署开)。"""

    def _cookie_value(self, secure_flag):
        from backend import app as app_mod
        orig = app_mod.SECURE_COOKIE
        app_mod.SECURE_COOKIE = secure_flag
        try:
            class _Stub:
                pass
            return app_mod.Handler._session_cookie_header(_Stub(), "tok")[1]
        finally:
            app_mod.SECURE_COOKIE = orig

    def test_secure_off(self):
        v = self._cookie_value(False)
        self.assertIn("HttpOnly", v)
        self.assertNotIn("Secure", v)

    def test_secure_on(self):
        v = self._cookie_value(True)
        self.assertIn("HttpOnly", v)
        self.assertIn("Secure", v)


if __name__ == "__main__":
    unittest.main()
