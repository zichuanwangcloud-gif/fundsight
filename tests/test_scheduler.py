# -*- coding: utf-8 -*-
"""backend.scheduler 单元测试。

覆盖启动时全量同步的触发判定与容错，不发起真实网络请求：
用 mock 替换 fund_list_sync.sync，并用临时数据库隔离。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend import scheduler
from backend.models import db as db_mod


class TestMaybeBootstrapSync(unittest.TestCase):
    def setUp(self):
        # 用独立临时库,避免污染真实 data/fundsight.db
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=True)  # 写入种子数据

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _count(self):
        conn = sqlite3.connect(self.path)
        n = conn.execute("SELECT COUNT(*) FROM fund_list").fetchone()[0]
        conn.close()
        return n

    def test_triggers_when_only_seed(self):
        # 仅种子数据 → 应触发同步
        called = {"n": 0}

        def fake_sync():
            called["n"] += 1
            return 100

        triggered = scheduler.maybe_bootstrap_sync(
            seed_count=len(db_mod.SEED_FUNDS), sync_fn=fake_sync, background=False
        )
        self.assertTrue(triggered)
        self.assertEqual(called["n"], 1)

    def test_skips_when_already_populated(self):
        # 塞入远多于种子数的数据 → 不应触发
        conn = sqlite3.connect(self.path)
        conn.executemany(
            "INSERT OR IGNORE INTO fund_list(fund_code,name,pinyin,fund_type,synced_at)"
            " VALUES (?,?,?,?,datetime('now'))",
            [(f"9{i:05d}", f"基金{i}", "jj", "混合") for i in range(500)],
        )
        conn.commit()
        conn.close()

        called = {"n": 0}

        def fake_sync():
            called["n"] += 1
            return 0

        triggered = scheduler.maybe_bootstrap_sync(
            seed_count=len(db_mod.SEED_FUNDS), sync_fn=fake_sync, background=False
        )
        self.assertFalse(triggered)
        self.assertEqual(called["n"], 0)

    def test_swallows_sync_exception(self):
        # sync 抛异常时不应向上冒泡,服务照常
        def boom():
            raise RuntimeError("network unreachable")

        # 不抛异常即通过
        triggered = scheduler.maybe_bootstrap_sync(
            seed_count=len(db_mod.SEED_FUNDS), sync_fn=boom, background=False
        )
        self.assertTrue(triggered)  # 触发了(尽管内部失败)


class TestStartPeriodicSync(unittest.TestCase):
    def test_returns_daemon_thread(self):
        # 返回的应是已启动的 daemon 线程,不阻塞
        t = scheduler.start_periodic_sync(interval_days=7, sync_fn=lambda: 0)
        self.assertTrue(t.daemon)
        self.assertTrue(t.is_alive())


class TestQuoteRefresh(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def test_refresh_holdings_quotes_no_holdings(self):
        # 无持仓 → 返回 0,不调 refresh_quotes
        with patch("backend.datasource.fundgz.refresh_quotes") as mock_rq:
            n = scheduler._refresh_holdings_quotes()
        self.assertEqual(n, 0)
        mock_rq.assert_not_called()

    def test_refresh_holdings_quotes_calls_with_distinct_codes(self):
        conn = sqlite3.connect(self.path)
        conn.executemany(
            "INSERT INTO holding(fund_code,hold_amount,created_at) VALUES (?,?,datetime('now'))",
            [("020608", 1000.0), ("020608", 2000.0), ("005827", 3000.0)],  # 020608 重复
        )
        conn.commit()
        conn.close()
        with patch("backend.datasource.fundgz.refresh_quotes", return_value=2) as mock_rq:
            scheduler._refresh_holdings_quotes()
        # 应对 DISTINCT codes 调用一次
        mock_rq.assert_called_once()
        called_codes = mock_rq.call_args[0][1]
        self.assertEqual(set(called_codes), {"020608", "005827"})

    def test_start_quote_refresh_returns_daemon(self):
        t = scheduler.start_quote_refresh(interval_seconds=60, quote_fn=lambda: 0, run_now=False)
        self.assertTrue(t.daemon)
        self.assertTrue(t.is_alive())

    def test_trigger_quote_for_runs_without_blocking(self):
        # trigger_quote_for 返回线程,不抛异常
        called = {"code": None}

        def fake_one(code):
            called["code"] = code

        t = scheduler.trigger_quote_for("020608", one_fn=fake_one)
        t.join(timeout=2)
        self.assertEqual(called["code"], "020608")


if __name__ == "__main__":
    unittest.main()
