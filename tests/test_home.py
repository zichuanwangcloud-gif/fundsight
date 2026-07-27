# -*- coding: utf-8 -*-
"""首页概览「今天」聚合层单测(方向 A)。

覆盖:
  - 空仓:overview 不报错,today_pl/None,goals/alerts 空
  - 组合今日盈亏汇总 + spark 按今日涨跌排序
  - 目标进度:target_progress / 距止盈 / 浮亏回本进度
  - 今天要看:止盈触发 / 止损触发 / 定投到点 / 未读通知数
  - 流水优先:有流水时收益率以流水成本为准
  - AI 早晚报:无 key 优雅降级
  - 鉴权:未登录 401;路由注册
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from backend.api import home
from backend.api._router import Ctx
from backend.models import db as db_mod


class _T(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._o = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._o
        os.unlink(self.path)

    def _c(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def _fund(self, code, name, ftype="混合型"):
        c = self._c()
        c.execute("INSERT OR REPLACE INTO fund_list(fund_code,name,pinyin,fund_type,synced_at) "
                  "VALUES (?,?,?,?,datetime('now'))", (code, name, code, ftype))
        c.commit(); c.close()

    def _quote(self, code, dwjz, gsz, gszzl=None, nav=None):
        c = self._c()
        c.execute("INSERT OR REPLACE INTO fund_quote(fund_code,name,dwjz,gsz,gszzl,nav,updated_at) "
                  "VALUES (?,?,?,?,?,?,datetime('now'))", (code, code, dwjz, gsz, gszzl, nav))
        c.commit(); c.close()

    def _hold(self, uid, code, hold_amount, cost_amount, **kw):
        cols = "user_id,fund_code,hold_amount,cost_amount,created_at"
        vals = [uid, code, hold_amount, cost_amount]
        extra = ""
        for k in ("target_rate", "stop_profit", "stop_loss", "trailing_stop_pct", "peak_nav"):
            if k in kw:
                cols += "," + k
                extra += ",?"
                vals.append(kw[k])
        c = self._c()
        c.execute("INSERT INTO holding(%s) VALUES (?,?,?,?,datetime('now')%s)" % (cols, extra), vals)
        c.commit(); c.close()

    def _tx(self, uid, code, action, shares, amount, d):
        c = self._c()
        c.execute("INSERT INTO fund_transaction(user_id,fund_code,action,shares,price,amount,"
                  "trade_date,created_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
                  (uid, code, action, shares, amount / shares, amount, d))
        c.commit(); c.close()


class TestOverview(_T):
    def test_empty(self):
        c = db_mod.get_conn()
        ov = home._build_overview(c, 1)
        c.close()
        self.assertIsNone(ov["today_pl"])
        self.assertEqual(ov["goals"], [])
        self.assertEqual(ov["alerts"], [])
        self.assertEqual(ov["unread_notifications"], 0)

    def test_today_pl_and_spark(self):
        self._fund("A", "涨基"); self._fund("B", "跌基")
        self._quote("A", dwjz=1.0, gsz=1.1, gszzl=10.0)   # +10%
        self._quote("B", dwjz=1.0, gsz=0.95, gszzl=-5.0)  # -5%
        self._hold(1, "A", hold_amount=1000, cost_amount=1000)  # 1000 份
        self._hold(1, "B", hold_amount=1000, cost_amount=1000)
        c = db_mod.get_conn()
        ov = home._build_overview(c, 1)
        c.close()
        # A 今日 +100,B 今日 -50 → 合计 +50
        self.assertAlmostEqual(ov["today_pl"], 50.0, places=2)
        # spark 升序:跌基在前,涨基在后
        self.assertEqual(ov["spark"][0]["name"], "跌基")
        self.assertEqual(ov["spark"][-1]["name"], "涨基")

    def test_goal_progress_and_stop_profit_alert(self):
        self._fund("A", "白酒")
        self._quote("A", dwjz=1.0, gsz=1.2, gszzl=20.0)  # 现价1.2
        # 成本1000,市值1200 → 收益率 20%
        self._hold(1, "A", hold_amount=1000, cost_amount=1000, target_rate=40, stop_profit=10)
        c = db_mod.get_conn()
        ov = home._build_overview(c, 1)
        c.close()
        g = ov["goals"][0]
        self.assertEqual(g["target_progress_pct"], 50.0)      # 20/40
        self.assertEqual(g["dist_to_stop_profit"], -10.0)     # 10-20,已超止盈
        # 收益率20% >= 止盈10% → 触发 stop_profit alert
        kinds = {a["kind"] for a in ov["alerts"]}
        self.assertIn("stop_profit", kinds)

    def test_recovery_progress_on_loss(self):
        self._fund("A", "套牢")
        self._quote("A", dwjz=1.0, gsz=0.8, gszzl=-20.0)  # 现价0.8
        self._hold(1, "A", hold_amount=1000, cost_amount=1000, stop_loss=-15)
        c = db_mod.get_conn()
        ov = home._build_overview(c, 1)
        c.close()
        g = ov["goals"][0]
        self.assertTrue(g["in_loss"])
        self.assertEqual(g["recovery_progress_pct"], 80.0)  # 800/1000
        kinds = {a["kind"] for a in ov["alerts"]}
        self.assertIn("stop_loss", kinds)  # -20 <= -15

    def test_dca_due_alert(self):
        self._fund("A", "沪深300")
        c = self._c()
        yest = (date.today() - timedelta(days=1)).isoformat()
        c.execute("INSERT INTO dca_plan(user_id,fund_code,per_amount,freq,invest_day,next_date,"
                  "active,created_at) VALUES (1,'A',500,'monthly',1,?,1,datetime('now'))", (yest,))
        c.commit(); c.close()
        c = db_mod.get_conn()
        ov = home._build_overview(c, 1)
        c.close()
        kinds = {a["kind"] for a in ov["alerts"]}
        self.assertIn("dca_due", kinds)

    def test_transaction_source_return(self):
        self._fund("A", "流水基")
        self._quote("A", dwjz=1.0, gsz=1.5, gszzl=50.0)  # 现价1.5
        # 流水:买1000份花1000(均价1.0);现价1.5 → 市值1500 → 收益率50%
        self._tx(1, "A", "buy", 1000, 1000, "2026-01-01")
        c = db_mod.get_conn()
        ov = home._build_overview(c, 1)
        c.close()
        h = ov["holdings"][0]
        self.assertAlmostEqual(h["current_return_pct"], 50.0, places=1)

    def test_unread_notifications(self):
        c = self._c()
        c.execute("INSERT INTO notification(user_id,fund_code,kind,message,created_at) "
                  "VALUES (1,'A','x','hi',datetime('now'))")
        c.commit(); c.close()
        c = db_mod.get_conn()
        ov = home._build_overview(c, 1)
        c.close()
        self.assertEqual(ov["unread_notifications"], 1)


class TestRoutesAndBriefing(_T):
    def test_overview_requires_login(self):
        code, _ = home.get_overview(Ctx(user_id=None))
        self.assertEqual(code, 401)

    def test_briefing_degrades_without_key(self):
        # 清掉可能存在的 key,确保降级路径
        saved = {k: os.environ.pop(k, None) for k in
                 ("FUNDSIGHT_AI_API_KEY", "ANTHROPIC_API_KEY")}
        try:
            code, body = home.get_briefing(Ctx(user_id=1))
            self.assertEqual(code, 200)
            self.assertFalse(body["ok"])
            self.assertFalse(body["configured"])
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_routes_registered(self):
        paths = {p for _, p, _ in home.ROUTES}
        self.assertIn("/api/home/overview", paths)
        self.assertIn("/api/home/briefing", paths)


if __name__ == "__main__":
    unittest.main()
