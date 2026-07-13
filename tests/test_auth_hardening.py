# -*- coding: utf-8 -*-
"""M10B 鉴权加固测试:接口限流 / token 主动吊销 / 登录审计落库 / 越权只读。

不发起真实网络,用临时库隔离,沿用 test_notifications 的 Ctx 手法直接驱动 handler。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from backend import auth
from backend.api import _router, login_audit
from backend.models import db as db_mod


class _TmpDbMixin:
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)
        # 限流内存缓存在进程内常驻,每用例前清空,避免相互污染。
        auth._rate_cache.clear()

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)
        auth._rate_cache.clear()

    def _add_session(self, token, uid, days=30):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO session(token,user_id,created_at,expires_at) "
            "VALUES(?,?,datetime('now','localtime'),datetime('now','localtime',?))",
            (token, uid, f"{days:+d} days"),
        )
        conn.commit()
        conn.close()


# ---------- B1 接口限流 ----------

class TestRateLimit(_TmpDbMixin, unittest.TestCase):
    def test_allows_under_threshold(self):
        uid = auth.create_user("u", "pw12345")
        for _ in range(60):
            self.assertTrue(auth.check_rate_limit(uid, "/api/holdings"))
        # 第 60 次后仍未超限(60 次/分钟)

    def test_blocks_over_threshold_returns_false(self):
        uid = auth.create_user("u", "pw12345")
        for _ in range(60):
            auth.check_rate_limit(uid, "/api/holdings")
        # 第 61 次 → 超限
        self.assertFalse(auth.check_rate_limit(uid, "/api/holdings"))

    def test_endpoints_independent(self):
        uid = auth.create_user("u", "pw12345")
        for _ in range(60):
            auth.check_rate_limit(uid, "/api/holdings")
        # /api/holdings 已打满,/api/search 仍可用(按 user+端点 隔离)
        self.assertTrue(auth.check_rate_limit(uid, "/api/search"))

    def test_users_independent(self):
        a = auth.create_user("a", "pw12345")
        b = auth.create_user("b", "pw12345")
        for _ in range(60):
            auth.check_rate_limit(a, "/api/holdings")
        # a 被限不影响 b
        self.assertTrue(auth.check_rate_limit(b, "/api/holdings"))

    def test_window_resets(self):
        uid = auth.create_user("u", "pw12345")
        # 用较小窗口便于测试:2 次/2 秒
        for _ in range(2):
            self.assertTrue(auth.check_rate_limit(uid, "/api/x", limit=2, window_sec=2))
        self.assertFalse(auth.check_rate_limit(uid, "/api/x", limit=2, window_sec=2))
        # 推进时间越过窗口边界 → 计数重置
        with mock.patch("backend.auth.time.time", return_value=10.0):
            self.assertTrue(auth.check_rate_limit(uid, "/api/x", limit=2, window_sec=2))

    def test_guard_returns_429_when_over(self):
        """_router.rate_limit_guard:超限返回 (429, obj),放行返回 None。"""
        uid = auth.create_user("u", "pw12345")
        self.assertIsNone(_router.rate_limit_guard(uid, "/api/holdings"))
        for _ in range(60):
            auth.check_rate_limit(uid, "/api/holdings")
        blocked = _router.rate_limit_guard(uid, "/api/holdings")
        self.assertIsNotNone(blocked)
        code, obj = blocked
        self.assertEqual(code, 429)
        self.assertIn("error", obj)

    def test_guard_anonymous_passes(self):
        """未登录(user_id=None)不做限流,放行。"""
        self.assertIsNone(_router.rate_limit_guard(None, "/api/holdings"))


# ---------- B2 token 主动吊销 ----------

class TestRevokeSessions(_TmpDbMixin, unittest.TestCase):
    def test_revoke_all_sessions_for_user(self):
        uid = auth.create_user("u", "pw12345")
        self._add_session("tok_a", uid)
        self._add_session("tok_b", uid)
        self.assertEqual(auth.get_user_by_token("tok_a"), uid)
        self.assertEqual(auth.get_user_by_token("tok_b"), uid)
        n = auth.revoke_user_sessions(uid)
        self.assertEqual(n, 2)
        self.assertIsNone(auth.get_user_by_token("tok_a"))
        self.assertIsNone(auth.get_user_by_token("tok_b"))

    def test_revoke_does_not_touch_other_user(self):
        a = auth.create_user("a", "pw12345")
        b = auth.create_user("b", "pw12345")
        self._add_session("tok_a", a)
        self._add_session("tok_b", b)
        auth.revoke_user_sessions(a)
        self.assertIsNone(auth.get_user_by_token("tok_a"))
        self.assertEqual(auth.get_user_by_token("tok_b"), b)

    def test_change_password_revokes_sessions(self):
        """改密后旧 token 全部失效,新密码可登录。"""
        uid = auth.create_user("u", "pw12345")
        self._add_session("tok_old", uid)
        self.assertEqual(auth.get_user_by_token("tok_old"), uid)
        ok = auth.change_password(uid, "pw12345", "newpw678")
        self.assertTrue(ok)
        # 旧 session 失效(B2 核心)
        self.assertIsNone(auth.get_user_by_token("tok_old"))
        # 旧密码登录失败,新密码可登录
        self.assertIsNone(auth.authenticate("u", "pw12345"))
        self.assertEqual(auth.authenticate("u", "newpw678"), uid)

    def test_change_password_wrong_old_rejected(self):
        uid = auth.create_user("u", "pw12345")
        self._add_session("tok", uid)
        ok = auth.change_password(uid, "WRONG", "newpw678")
        self.assertFalse(ok)
        # 旧密码错 → 不改不改密,session 仍有效
        self.assertEqual(auth.get_user_by_token("tok"), uid)


# ---------- B3 登录审计 ----------

class TestLoginAudit(_TmpDbMixin, unittest.TestCase):
    def test_record_success(self):
        uid = auth.create_user("u", "pw12345")
        auth.record_login_audit(uid, "1.2.3.4", "Mozilla/5.0", True)
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT user_id,ip,ua,ok FROM login_audit WHERE user_id=?", (uid,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], uid)
        self.assertEqual(row[1], "1.2.3.4")
        self.assertEqual(row[2], "Mozilla/5.0")
        self.assertEqual(row[3], 1)

    def test_record_failure_unknown_user(self):
        # 未知用户登录失败:user_id 落 NULL,ok=0
        auth.record_login_audit(None, "9.9.9.9", "curl/8", False)
        conn = sqlite3.connect(self.path)
        row = conn.execute("SELECT user_id,ok FROM login_audit").fetchone()
        conn.close()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], 0)


# ---------- B4 越权审计只读接口 ----------

class TestLoginAuditApi(_TmpDbMixin, unittest.TestCase):
    def _ctx(self, user_id=None, **kw):
        return _router.Ctx(query=kw, params={}, user_id=user_id)

    def _call(self, **kw):
        r = login_audit.handle_list(self._ctx(**kw))
        return r if isinstance(r, tuple) else (200, r)

    def _seed_audit(self, uid, ok=True, ip="1.1.1.1"):
        auth.record_login_audit(uid, ip, "ua", ok)

    def test_requires_login(self):
        code, _ = self._call(user_id=None)
        self.assertEqual(code, 401)

    def test_returns_only_own_records(self):
        a = auth.create_user("a", "pw12345")
        b = auth.create_user("b", "pw12345")
        self._seed_audit(a, ok=True)
        self._seed_audit(a, ok=False, ip="2.2.2.2")
        self._seed_audit(b, ok=True)
        code, obj = self._call(user_id=a)
        self.assertEqual(code, 200)
        records = obj["records"]
        self.assertEqual(len(records), 2)
        # 全是 a 自己的记录
        self.assertTrue(all(r["user_id"] == a for r in records))
        # 按 id DESC 排序
        self.assertEqual(records[0]["ip"], "2.2.2.2")

    def test_cross_user_query_param_returns_404(self):
        """越权:带 ?user_id=他人 → 404,不泄露他人记录。"""
        a = auth.create_user("a", "pw12345")
        b = auth.create_user("b", "pw12345")
        self._seed_audit(b, ok=True)
        ctx = _router.Ctx(query={"user_id": [str(b)]}, params={}, user_id=a)
        r = login_audit.handle_list(ctx)
        code, obj = (r if isinstance(r, tuple) else (200, r))
        self.assertEqual(code, 404)

    def test_own_user_id_param_ok(self):
        a = auth.create_user("a", "pw12345")
        self._seed_audit(a, ok=True)
        ctx = _router.Ctx(query={"user_id": [str(a)]}, params={}, user_id=a)
        r = login_audit.handle_list(ctx)
        code, obj = (r if isinstance(r, tuple) else (200, r))
        self.assertEqual(code, 200)
        self.assertEqual(len(obj["records"]), 1)


# ---------- 清理 daemon ----------

class TestRateLimitCleanup(_TmpDbMixin, unittest.TestCase):
    def test_purge_stale_rows(self):
        uid = auth.create_user("u", "pw12345")
        conn = sqlite3.connect(self.path)
        # 插一条远古窗口的计数行
        conn.execute(
            "INSERT INTO rate_limit_state(user_id,endpoint,window_start,count) "
            "VALUES(?,?,?,?)",
            (uid, "/api/x", "1", 999),
        )
        conn.commit()
        conn.close()
        n = auth.purge_stale_rate_limit(window_sec=60)
        self.assertEqual(n, 1)

    def test_start_returns_daemon(self):
        t = auth.start_rate_limit_cleanup(interval_hours=24, run_now=False)
        self.assertTrue(t.daemon)
        self.assertTrue(t.is_alive())


if __name__ == "__main__":
    unittest.main()
