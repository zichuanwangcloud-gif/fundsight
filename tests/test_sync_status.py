# -*- coding: utf-8 -*-
"""M9-A 抓取任务可观测性测试:_record_run 落库 + sync_status 只读接口。

不发起真实网络/抓取,用临时库隔离。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend import scheduler
from backend.api import sync_status, _router
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

    def _count_task_run(self):
        conn = sqlite3.connect(self.path)
        n = conn.execute("SELECT COUNT(*) FROM task_run").fetchone()[0]
        conn.close()
        return n


class TestRecordRun(_TmpDbMixin, unittest.TestCase):
    def test_ok_records_success(self):
        n, status, error = scheduler._record_run("quote_refresh", lambda: 3)
        self.assertEqual((n, status, error), (3, "ok", None))
        self.assertEqual(self._count_task_run(), 1)
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT task_name,status,affected,error,duration_ms FROM task_run"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "quote_refresh")
        self.assertEqual(row[1], "ok")
        self.assertEqual(row[2], 3)
        self.assertIsNone(row[3])
        self.assertGreaterEqual(row[4], 0)

    def test_fail_records_exception(self):
        def boom():
            raise RuntimeError("network unreachable")

        n, status, error = scheduler._record_run("nav_refresh", boom)
        self.assertIsNone(n)
        self.assertEqual(status, "fail")
        self.assertIn("RuntimeError", error)
        self.assertIn("network unreachable", error)
        conn = sqlite3.connect(self.path)
        row = conn.execute("SELECT status,affected,error FROM task_run").fetchone()
        conn.close()
        self.assertEqual(row[0], "fail")
        self.assertIsNone(row[1])
        self.assertIn("RuntimeError", row[2])

    def test_non_int_result_records_null_affected(self):
        n, status, _ = scheduler._record_run("quote_one", lambda: None)
        self.assertEqual((n, status), (None, "ok"))
        conn = sqlite3.connect(self.path)
        row = conn.execute("SELECT affected FROM task_run").fetchone()
        conn.close()
        self.assertIsNone(row[0])

    def test_write_failure_does_not_propagate(self):
        # 模拟 get_conn 抛异常:_record_run 不应向上冒泡
        with patch("backend.scheduler.get_conn", side_effect=sqlite3.OperationalError("disk")):
            n, status, error = scheduler._record_run("profile_refresh", lambda: 5)
        self.assertEqual((n, status), (5, "ok"))  # fn() 仍执行成功
        self.assertEqual(self._count_task_run(), 0)  # 没写进去


class TestSafeSyncWritesTaskRun(_TmpDbMixin, unittest.TestCase):
    def test_safe_sync_success_logs(self):
        scheduler._safe_sync(lambda: 100)
        self.assertEqual(self._count_task_run(), 1)
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT task_name,status,affected FROM task_run"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("fund_list_sync", "ok", 100))

    def test_safe_sync_failure_logs_fail(self):
        def boom():
            raise RuntimeError("nope")

        scheduler._safe_sync(boom)
        conn = sqlite3.connect(self.path)
        row = conn.execute("SELECT status,error FROM task_run").fetchone()
        conn.close()
        self.assertEqual(row[0], "fail")
        self.assertIn("RuntimeError", row[1])


class TestSyncStatusApi(_TmpDbMixin, unittest.TestCase):
    def _ctx(self, user_id=None, **kw):
        return _router.Ctx(query=kw, user_id=user_id)

    def _call(self, fn, **kw):
        # handler 成功返回单 dict(dispatch 会包成 (200, dict)),失败返回 (code, dict)
        r = fn(self._ctx(**kw))
        return r if isinstance(r, tuple) else (200, r)

    def test_summary_requires_login(self):
        code, obj = self._call(sync_status.handle_summary, user_id=None)
        self.assertEqual(code, 401)

    def test_list_requires_login(self):
        code, obj = self._call(sync_status.handle_list, user_id=None)
        self.assertEqual(code, 401)

    def test_summary_returns_latest_per_task(self):
        scheduler._record_run("quote_refresh", lambda: 3)
        scheduler._record_run("nav_refresh", lambda: 1)
        # quote_refresh 再跑一次失败,应取代前一次成为「最近」
        scheduler._record_run("quote_refresh", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        code, obj = self._call(sync_status.handle_summary, user_id=1)
        self.assertEqual(code, 200)
        tasks = {t["task_name"]: t for t in obj["tasks"]}
        self.assertEqual(tasks["quote_refresh"]["status"], "fail")
        self.assertEqual(tasks["nav_refresh"]["status"], "ok")

    def test_list_returns_recent_ordered(self):
        for i in range(3):
            scheduler._record_run("quote_refresh", lambda i=i: i)
        code, obj = self._call(sync_status.handle_list, user_id=1, limit="2")
        self.assertEqual(code, 200)
        self.assertEqual(len(obj["runs"]), 2)

    def test_list_limit_clamped(self):
        # 非法 limit 回退默认 100;超限回退默认 100
        for _ in range(3):
            scheduler._record_run("quote_refresh", lambda: 1)
        code, obj = self._call(sync_status.handle_list, user_id=1, limit="abc")
        self.assertEqual(code, 200)
        self.assertEqual(len(obj["runs"]), 3)  # 全部返回(默认 100 > 3)
        code, obj = self._call(sync_status.handle_list, user_id=1, limit="9999")
        self.assertEqual(len(obj["runs"]), 3)


if __name__ == "__main__":
    unittest.main()
