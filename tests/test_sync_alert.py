# -*- coding: utf-8 -*-
"""M10C 数据可靠性收口测试:失败重试 + sync_alert 告警推送 + 去重 + 状态页查询。

不发起真实网络/抓取,用临时库隔离,直接构造 task_run / holding / notification。
"""
import os
import sqlite3
import tempfile
import unittest

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

    def _exec(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def _count(self, table, where="", args=()):
        conn = sqlite3.connect(self.path)
        n = conn.execute(f"SELECT COUNT(*) FROM {table} {where}", args).fetchone()[0]
        conn.close()
        return n

    def _add_holding(self, user_id, code):
        self._exec(
            "INSERT INTO holding(user_id,fund_code,hold_amount,created_at) "
            "VALUES(?,?,?,datetime('now'))",
            (user_id, code, 1000.0),
        )

    def _plant_fails(self, task_name, n):
        def boom():
            raise RuntimeError("transient failure")
        for _ in range(n):
            scheduler._record_run(task_name, boom)


class TestRunWithRetries(_TmpDbMixin, unittest.TestCase):
    def test_no_retry_when_ok(self):
        n, status, error = scheduler._run_with_retries(
            "quote_refresh", lambda: 3, retries=2,
            base_delay=0, sleep=lambda _: None,
        )
        self.assertEqual((n, status, error), (3, "ok", None))
        self.assertEqual(self._count("task_run"), 1)

    def test_retry_succeeds_on_second_attempt(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return 5

        n, status, error = scheduler._run_with_retries(
            "quote_refresh", flaky, retries=2,
            base_delay=0, sleep=lambda _: None,
        )
        self.assertEqual(status, "ok")
        self.assertEqual(n, 5)
        # 1 次失败 + 1 次重试成功 = 2 行 task_run
        self.assertEqual(self._count("task_run"), 2)
        conn = sqlite3.connect(self.path)
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM task_run ORDER BY id").fetchall()]
        conn.close()
        self.assertEqual(statuses, ["fail", "ok"])

    def test_retry_all_fail_records_each_attempt(self):
        def boom():
            raise RuntimeError("down")

        n, status, error = scheduler._run_with_retries(
            "nav_refresh", boom, retries=2,
            base_delay=0, sleep=lambda _: None,
        )
        self.assertEqual(status, "fail")
        self.assertIsNone(n)
        self.assertIn("RuntimeError", error)
        # 初次 + 2 次重试 = 3 行
        self.assertEqual(self._count("task_run"), 3)
        conn = sqlite3.connect(self.path)
        rows = conn.execute("SELECT status FROM task_run").fetchall()
        conn.close()
        self.assertTrue(all(r[0] == "fail" for r in rows))

    def test_retries_zero_no_retry(self):
        def boom():
            raise RuntimeError("down")

        _, status, _ = scheduler._run_with_retries(
            "nav_refresh", boom, retries=0,
            base_delay=0, sleep=lambda _: None,
        )
        self.assertEqual(status, "fail")
        self.assertEqual(self._count("task_run"), 1)

    def test_delay_increases_per_retry(self):
        delays = []
        scheduler._run_with_retries(
            "nav_refresh", lambda: (_ for _ in ()).throw(RuntimeError("x")),
            retries=3, base_delay=10, sleep=lambda d: delays.append(d),
        )
        # 间隔递增:10, 20, 30
        self.assertEqual(delays, [10, 20, 30])


class TestConsecutiveFailCount(_TmpDbMixin, unittest.TestCase):
    def test_zero_when_no_runs(self):
        self.assertEqual(scheduler._consecutive_fail_count("quote_refresh"), 0)

    def test_counts_trailing_fails(self):
        self._plant_fails("quote_refresh", 3)
        self.assertEqual(scheduler._consecutive_fail_count("quote_refresh"), 3)

    def test_resets_on_ok(self):
        self._plant_fails("quote_refresh", 3)
        scheduler._record_run("quote_refresh", lambda: 1)  # ok 中断连续
        self.assertEqual(scheduler._consecutive_fail_count("quote_refresh"), 0)

    def test_capped_at_threshold(self):
        # 连续 5 次失败,计数上限取阈值(默认 3)
        self._plant_fails("quote_refresh", 5)
        self.assertEqual(scheduler._consecutive_fail_count("quote_refresh"), 3)


class TestPushSyncAlerts(_TmpDbMixin, unittest.TestCase):
    def test_no_push_below_threshold(self):
        self._add_holding(1, "020608")
        self._plant_fails("quote_refresh", 2)  # < 阈值 3
        n = scheduler._push_sync_alerts("quote_refresh")
        self.assertEqual(n, 0)
        self.assertEqual(self._count("notification"), 0)

    def test_push_when_threshold_exceeded(self):
        self._add_holding(1, "020608")
        self._plant_fails("quote_refresh", 3)
        n = scheduler._push_sync_alerts("quote_refresh")
        self.assertEqual(n, 1)
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT user_id,fund_code,kind,message,read_at FROM notification"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "quote_refresh")  # 任务名存 fund_code 列
        self.assertEqual(row[2], "sync_alert")
        self.assertIn("quote_refresh", row[3])
        self.assertIn("020608", row[3])
        self.assertIsNone(row[4])  # 未读

    def test_dedup_unread_alert_not_duplicated(self):
        self._add_holding(1, "020608")
        self._plant_fails("quote_refresh", 3)
        self.assertEqual(scheduler._push_sync_alerts("quote_refresh"), 1)
        # 再推一次,已有未读 sync_alert → 去重不重复
        self.assertEqual(scheduler._push_sync_alerts("quote_refresh"), 0)
        self.assertEqual(self._count("notification"), 1)

    def test_dedup_resets_after_read(self):
        self._add_holding(1, "020608")
        self._plant_fails("quote_refresh", 3)
        scheduler._push_sync_alerts("quote_refresh")
        # 标记已读后再巡检,允许重新告警
        self._exec(
            "UPDATE notification SET read_at=datetime('now','localtime') "
            "WHERE kind='sync_alert'"
        )
        self._plant_fails("quote_refresh", 1)  # 累计仍 >= 3
        self.assertEqual(scheduler._push_sync_alerts("quote_refresh"), 1)
        self.assertEqual(self._count("notification"), 2)

    def test_push_per_user_with_their_holdings(self):
        self._add_holding(1, "020608")
        self._add_holding(1, "005827")
        self._add_holding(2, "161725")
        self._plant_fails("nav_refresh", 3)
        n = scheduler._push_sync_alerts("nav_refresh")
        self.assertEqual(n, 2)  # 两个持仓 user 各一条
        conn = sqlite3.connect(self.path)
        rows = conn.execute(
            "SELECT user_id, message FROM notification WHERE user_id=1"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertIn("020608", rows[0][1])
        self.assertIn("005827", rows[0][1])

    def test_no_holdings_no_push(self):
        self._plant_fails("quote_refresh", 3)
        self.assertEqual(scheduler._push_sync_alerts("quote_refresh"), 0)

    def test_push_does_not_throw_on_db_error(self):
        import unittest.mock as mock
        with mock.patch("backend.scheduler.get_conn",
                        side_effect=sqlite3.OperationalError("disk")):
            n = scheduler._push_sync_alerts("quote_refresh")
        self.assertEqual(n, 0)


class TestDispatchAlerts(_TmpDbMixin, unittest.TestCase):
    def test_dispatch_iterates_all_tasks(self):
        self._add_holding(1, "020608")
        self._plant_fails("quote_refresh", 3)
        self._plant_fails("nav_refresh", 2)  # 未超阈值
        scheduler._dispatch_alerts()
        # 只 quote_refresh 触发告警
        conn = sqlite3.connect(self.path)
        rows = conn.execute(
            "SELECT fund_code FROM notification WHERE kind='sync_alert'"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "quote_refresh")


class TestSyncAlertsApi(_TmpDbMixin, unittest.TestCase):
    def _ctx(self, user_id=None, **kw):
        return _router.Ctx(query=kw, user_id=user_id)

    def _call(self, fn, **kw):
        r = fn(self._ctx(**kw))
        return r if isinstance(r, tuple) else (200, r)

    def test_alerts_requires_login(self):
        code, _ = self._call(sync_status.handle_alerts, user_id=None)
        self.assertEqual(code, 401)

    def test_alerts_empty_when_none(self):
        code, obj = self._call(sync_status.handle_alerts, user_id=1)
        self.assertEqual(code, 200)
        self.assertEqual(obj["alerts"], [])

    def test_alerts_lists_unrecovered_with_funds(self):
        self._add_holding(1, "020608")
        self._add_holding(1, "005827")
        self._plant_fails("quote_refresh", 3)
        code, obj = self._call(sync_status.handle_alerts, user_id=1)
        self.assertEqual(code, 200)
        self.assertEqual(len(obj["alerts"]), 1)
        a = obj["alerts"][0]
        self.assertEqual(a["task_name"], "quote_refresh")
        self.assertEqual(a["consecutive_fails"], 3)
        self.assertIn("020608", a["affected_funds"])
        self.assertIn("005827", a["affected_funds"])
        self.assertIn("RuntimeError", a["last_error"])

    def test_alerts_excludes_recovered_tasks(self):
        self._add_holding(1, "020608")
        self._plant_fails("quote_refresh", 3)
        scheduler._record_run("quote_refresh", lambda: 1)  # 恢复
        code, obj = self._call(sync_status.handle_alerts, user_id=1)
        self.assertEqual(code, 200)
        self.assertEqual(obj["alerts"], [])


class TestStartAlertDispatcher(unittest.TestCase):
    def test_returns_daemon_thread(self):
        t = scheduler.start_alert_dispatcher(interval_hours=6, run_now=False)
        self.assertTrue(t.daemon)
        self.assertTrue(t.is_alive())


if __name__ == "__main__":
    unittest.main()
