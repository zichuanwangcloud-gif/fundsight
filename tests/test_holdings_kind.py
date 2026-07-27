# -*- coding: utf-8 -*-
"""自选/持有 分离:add_holding / update_holding / list_holdings 的 kind 行为。

覆盖:
- 显式 kind='watch'/'hold' 落库;缺省时按是否录金额推断(_kind_of)。
- list_holdings 每条带 kind,summary 分 hold_count/watch_count。
- 转持有(补金额+kind='hold')与转自选(清金额+kind='watch')的往返。

add_holding 会异步触发行情/净值/基本面拉取(网络),测试中 patch 掉这些副作用,
只验证入库与汇总的纯逻辑,不发起真实网络请求。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend import app
from backend.app import add_holding, update_holding, list_holdings, _kind_of
from backend.models import db as db_mod


class TestHoldingKind(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)
        # 屏蔽 add_holding 的异步网络副作用
        self._patches = [
            patch.object(app, "trigger_quote_for", lambda *a, **k: None),
            patch.object(app, "trigger_nav_for", lambda *a, **k: None),
            patch.object(app, "trigger_history_for", lambda *a, **k: None),
            patch.object(app, "trigger_profile_for", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _kind(self, code, user_id=1):
        c = sqlite3.connect(self.path)
        row = c.execute(
            "SELECT kind, hold_amount FROM holding WHERE fund_code=? AND user_id=?",
            (code, user_id)).fetchone()
        c.close()
        return row  # (kind, hold_amount)

    def _id(self, code, user_id=1):
        c = sqlite3.connect(self.path)
        row = c.execute(
            "SELECT id FROM holding WHERE fund_code=? AND user_id=?", (code, user_id)).fetchone()
        c.close()
        return row[0]

    # ---- _kind_of 纯逻辑 ----
    def test_kind_of_explicit_wins(self):
        self.assertEqual(_kind_of("hold", None), "hold")   # 显式 hold 即使无金额
        self.assertEqual(_kind_of("watch", 10000.0), "watch")  # 显式 watch 即使有金额

    def test_kind_of_inference(self):
        self.assertEqual(_kind_of(None, 10000.0), "hold")   # 有金额 → 持有
        self.assertEqual(_kind_of(None, None), "watch")      # 无金额 → 自选
        self.assertEqual(_kind_of("", None), "watch")        # 非法值按推断

    # ---- add_holding ----
    def test_add_explicit_watch(self):
        add_holding({"fund_code": "020608", "kind": "watch"}, 1)
        kind, amt = self._kind("020608")
        self.assertEqual(kind, "watch")
        self.assertIsNone(amt)

    def test_add_explicit_hold_with_amount(self):
        add_holding({"fund_code": "005827", "kind": "hold", "hold_amount": "12000"}, 1)
        kind, amt = self._kind("005827")
        self.assertEqual(kind, "hold")
        self.assertEqual(amt, 12000.0)

    def test_add_infers_hold_from_amount(self):
        # 未传 kind 但录了金额 → 推断为持有
        add_holding({"fund_code": "003505", "hold_amount": "8000"}, 1)
        self.assertEqual(self._kind("003505")[0], "hold")

    # ---- list_holdings summary 分组 ----
    def test_list_summary_splits_counts(self):
        add_holding({"fund_code": "020608", "kind": "watch"}, 1)
        add_holding({"fund_code": "005827", "kind": "hold", "hold_amount": "10000"}, 1)
        data = list_holdings(1)
        self.assertEqual(data["summary"]["hold_count"], 1)
        self.assertEqual(data["summary"]["watch_count"], 1)
        kinds = {it["fund_code"]: it["kind"] for it in data["items"]}
        self.assertEqual(kinds["020608"], "watch")
        self.assertEqual(kinds["005827"], "hold")

    # ---- 转持有 / 转自选 往返 ----
    def test_convert_watch_to_hold(self):
        add_holding({"fund_code": "020608", "kind": "watch"}, 1)
        hid = self._id("020608")
        update_holding(hid, {"fund_code": "020608", "kind": "hold", "hold_amount": "9000"}, 1)
        kind, amt = self._kind("020608")
        self.assertEqual(kind, "hold")
        self.assertEqual(amt, 9000.0)

    def test_convert_hold_to_watch_clears_amount(self):
        add_holding({"fund_code": "005827", "kind": "hold", "hold_amount": "10000"}, 1)
        hid = self._id("005827")
        # 转自选:显式 watch + 清空金额
        update_holding(hid, {"fund_code": "005827", "kind": "watch",
                             "hold_amount": "", "cost_amount": ""}, 1)
        kind, amt = self._kind("005827")
        self.assertEqual(kind, "watch")
        self.assertIsNone(amt)
        # 汇总里不再算作持有
        data = list_holdings(1)
        self.assertEqual(data["summary"]["hold_count"], 0)
        self.assertEqual(data["summary"]["watch_count"], 1)

    # ---- 去重幂等:持有 ⊆ 自选,加只能加自选,同一 (user, code) 只保留一行 ----
    def _count(self, code, user_id=1):
        c = sqlite3.connect(self.path)
        n = c.execute(
            "SELECT COUNT(*) FROM holding WHERE fund_code=? AND user_id=?",
            (code, user_id)).fetchone()[0]
        c.close()
        return n

    def test_add_watch_twice_is_idempotent(self):
        # 连续两次加自选(同 user/code) → 只 1 行,不产生重复
        add_holding({"fund_code": "020608", "kind": "watch"}, 1)
        add_holding({"fund_code": "020608", "kind": "watch"}, 1)
        self.assertEqual(self._count("020608"), 1)
        self.assertEqual(self._kind("020608"), ("watch", None))

    def test_readd_watch_does_not_downgrade_hold(self):
        # 已是持有(有金额)后再"加自选"(无金额) → 金额与 kind 不被清空/降级
        add_holding({"fund_code": "005827", "kind": "hold", "hold_amount": "12000"}, 1)
        add_holding({"fund_code": "005827", "kind": "watch"}, 1)  # 详情页/搜索的加自选
        self.assertEqual(self._count("005827"), 1)
        kind, amt = self._kind("005827")
        self.assertEqual(kind, "hold")
        self.assertEqual(amt, 12000.0)

    def test_add_with_amount_upgrades_existing_watch(self):
        # 已是自选,再带金额 add → 原地升级为持有(不新增行)
        add_holding({"fund_code": "003505", "kind": "watch"}, 1)
        add_holding({"fund_code": "003505", "kind": "hold", "hold_amount": "8000"}, 1)
        self.assertEqual(self._count("003505"), 1)
        kind, amt = self._kind("003505")
        self.assertEqual(kind, "hold")
        self.assertEqual(amt, 8000.0)

    def test_dedup_scoped_per_user(self):
        # 去重仅限同一用户,不同用户各自保留
        add_holding({"fund_code": "020608", "kind": "watch"}, 1)
        add_holding({"fund_code": "020608", "kind": "watch"}, 2)
        self.assertEqual(self._count("020608", 1), 1)
        self.assertEqual(self._count("020608", 2), 1)


if __name__ == "__main__":
    unittest.main()
