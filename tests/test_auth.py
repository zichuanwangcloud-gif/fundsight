# -*- coding: utf-8 -*-
"""用户体系测试：密码哈希、账号、会话、存量迁移、自选数据隔离。

沿用 test_db 的「临时 DB 文件 + monkeypatch db.DB_PATH」手法，不起真实 HTTP。
auth 与 app 的 holding 函数都通过 db.get_conn() 访问库，get_conn 在调用时
读取 db.DB_PATH，故 monkeypatch 即可隔离。
"""
import os
import tempfile
import unittest

from backend.models import db
from backend import auth
from backend import app


class UserSystemTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        self._tmp_path = path
        self._orig_path = db.DB_PATH
        db.DB_PATH = path
        db.init_db(with_seed=False)
        # add_holding 会后台触发估值/历史拉取（M6/M7）；测试里 stub 掉，保持离线且不起线程
        self._orig_tq = app.trigger_quote_for
        self._orig_th = app.trigger_history_for
        app.trigger_quote_for = lambda code, *a, **k: None
        app.trigger_history_for = lambda code, *a, **k: None

    def tearDown(self):
        app.trigger_quote_for = self._orig_tq
        app.trigger_history_for = self._orig_th
        db.DB_PATH = self._orig_path
        if os.path.exists(self._tmp_path):
            os.remove(self._tmp_path)


class TestPasswordHashing(UserSystemTestBase):
    def test_hash_roundtrip_verifies(self):
        h, salt = auth.hash_password("s3cret")
        self.assertTrue(auth.verify_password("s3cret", salt, h))

    def test_wrong_password_rejected(self):
        h, salt = auth.hash_password("s3cret")
        self.assertFalse(auth.verify_password("wrong", salt, h))

    def test_salt_makes_hashes_differ(self):
        h1, s1 = auth.hash_password("same")
        h2, s2 = auth.hash_password("same")
        self.assertNotEqual(s1, s2)
        self.assertNotEqual(h1, h2)

    def test_password_not_stored_plaintext(self):
        auth.create_user("alice", "plaintextpw")
        conn = db.get_conn()
        row = conn.execute("SELECT pwd_hash, pwd_salt FROM user WHERE username=?",
                           ("alice",)).fetchone()
        conn.close()
        self.assertNotIn("plaintextpw", row["pwd_hash"])
        self.assertTrue(auth.verify_password("plaintextpw", row["pwd_salt"], row["pwd_hash"]))


class TestUsers(UserSystemTestBase):
    def test_create_and_authenticate(self):
        uid = auth.create_user("alice", "pw")
        self.assertEqual(auth.authenticate("alice", "pw"), uid)

    def test_authenticate_wrong_password(self):
        auth.create_user("alice", "pw")
        self.assertIsNone(auth.authenticate("alice", "nope"))

    def test_authenticate_unknown_user(self):
        self.assertIsNone(auth.authenticate("ghost", "pw"))

    def test_duplicate_username_rejected(self):
        auth.create_user("alice", "pw")
        with self.assertRaises(auth.UsernameTaken):
            auth.create_user("alice", "other")

    def test_empty_credentials_rejected(self):
        with self.assertRaises(ValueError):
            auth.create_user("", "pw")
        with self.assertRaises(ValueError):
            auth.create_user("bob", "")


class TestLegacyMigration(UserSystemTestBase):
    def test_first_user_inherits_global_holdings(self):
        # 模拟历史全局自选（user_id=0）
        conn = db.get_conn()
        conn.execute("INSERT INTO holding(user_id,fund_code) VALUES (0,'020608')")
        conn.execute("INSERT INTO holding(user_id,fund_code) VALUES (0,'000001')")
        conn.commit()
        conn.close()

        first = auth.create_user("alice", "pw")
        got = app.list_holdings(first)
        self.assertEqual(len(got["items"]), 2)

    def test_second_user_does_not_inherit(self):
        conn = db.get_conn()
        conn.execute("INSERT INTO holding(user_id,fund_code) VALUES (0,'020608')")
        conn.commit()
        conn.close()
        auth.create_user("alice", "pw")          # 首个用户吃掉存量
        second = auth.create_user("bob", "pw")   # 第二个不应继承
        self.assertEqual(len(app.list_holdings(second)["items"]), 0)


class TestSessions(UserSystemTestBase):
    def test_create_and_resolve(self):
        uid = auth.create_user("alice", "pw")
        token = auth.create_session(uid)
        self.assertEqual(auth.get_user_by_token(token), uid)

    def test_expired_session_invalid(self):
        uid = auth.create_user("alice", "pw")
        token = auth.create_session(uid, ttl_days=-1)  # 已过期
        self.assertIsNone(auth.get_user_by_token(token))

    def test_logout_deletes_session(self):
        uid = auth.create_user("alice", "pw")
        token = auth.create_session(uid)
        auth.delete_session(token)
        self.assertIsNone(auth.get_user_by_token(token))

    def test_unknown_token(self):
        self.assertIsNone(auth.get_user_by_token("nope"))
        self.assertIsNone(auth.get_user_by_token(None))


class TestHoldingIsolation(UserSystemTestBase):
    def setUp(self):
        super().setUp()
        self.a = auth.create_user("alice", "pw")
        self.b = auth.create_user("bob", "pw")

    def _add(self, uid, code):
        app.add_holding({"fund_code": code}, uid)
        conn = db.get_conn()
        hid = conn.execute("SELECT id FROM holding WHERE user_id=? AND fund_code=?",
                          (uid, code)).fetchone()["id"]
        conn.close()
        return hid

    def test_list_only_own(self):
        self._add(self.a, "020608")
        self.assertEqual(len(app.list_holdings(self.a)["items"]), 1)
        self.assertEqual(len(app.list_holdings(self.b)["items"]), 0)

    def test_cannot_delete_others(self):
        hid = self._add(self.a, "020608")
        app.delete_holding(hid, self.b)  # bob 越权删 alice 的
        self.assertEqual(len(app.list_holdings(self.a)["items"]), 1)  # 仍在
        app.delete_holding(hid, self.a)  # 本人可删
        self.assertEqual(len(app.list_holdings(self.a)["items"]), 0)

    def test_cannot_update_others(self):
        hid = self._add(self.a, "020608")
        app.update_holding(hid, {"hold_amount": 999}, self.b)  # 越权改
        conn = db.get_conn()
        amt = conn.execute("SELECT hold_amount FROM holding WHERE id=?", (hid,)).fetchone()["hold_amount"]
        conn.close()
        self.assertIsNone(amt)  # 未被改动


if __name__ == "__main__":
    unittest.main()
