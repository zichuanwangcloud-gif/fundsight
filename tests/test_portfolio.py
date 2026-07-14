# -*- coding: utf-8 -*-
"""PRD-03 组合层汇总单元测试(资产配置占比 + 持仓集中度)。

覆盖:
  - 空仓返回零结构不报错
  - 单只持仓:市值/盈亏/收益率/配置归类/集中度 100% 预警
  - 多只持仓:8 大类配置占比、Σ amount == total、CR3、TOP1 预警
  - 按 user_id 隔离(越权不可见)
  - 未登录 401、路由注册
"""
import os
import sqlite3
import tempfile
import unittest

from backend.api import portfolio
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

    def _quote(self, code, gsz, dwjz, nav=None):
        c = self._c()
        c.execute(
            "INSERT OR REPLACE INTO fund_quote(fund_code,name,gsz,dwjz,nav,updated_at) "
            "VALUES (?,?,?,?,?,datetime('now'))",
            (code, code, gsz, dwjz, nav),
        )
        c.commit()
        c.close()

    def _fund_type(self, code, fund_type):
        c = self._c()
        c.execute(
            "INSERT OR REPLACE INTO fund_list(fund_code,name,pinyin,fund_type,synced_at) "
            "VALUES (?,?,?,?,datetime('now'))", (code, code, code, fund_type),
        )
        c.commit()
        c.close()

    def _hold(self, user_id, code, hold_amount, cost_amount):
        c = self._c()
        c.execute(
            "INSERT INTO holding(user_id,fund_code,hold_amount,cost_amount,created_at) "
            "VALUES (?,?,?,?,datetime('now'))", (user_id, code, hold_amount, cost_amount),
        )
        c.commit()
        c.close()


class TestComputeSummary(_T):
    def test_empty_returns_zero(self):
        c = db_mod.get_conn()
        s = portfolio._compute_summary(c, 1)
        c.close()
        self.assertEqual(s["holdings_count"], 0)
        self.assertEqual(s["total_market_value"], 0.0)
        self.assertIsNone(s["total_pnl"])
        self.assertIsNone(s["total_return_pct"])
        self.assertIsNone(s["concentration"]["top1_fund_code"])
        self.assertFalse(s["concentration"]["warn"])

    def test_single_holding(self):
        self._fund_type("A", "混合型")
        self._quote("A", gsz=1.1, dwjz=1.0, nav=None)
        self._hold(1, "A", hold_amount=1000, cost_amount=1000)
        c = db_mod.get_conn()
        s = portfolio._compute_summary(c, 1)
        c.close()
        # shares=1000/1.0=1000, value=1000*1.1=1100(无 nav 用 gsz)
        self.assertEqual(s["total_market_value"], 1100.0)
        self.assertEqual(s["total_cost"], 1000.0)
        self.assertEqual(s["total_pnl"], 100.0)
        self.assertEqual(s["total_return_pct"], 10.0)
        self.assertEqual(s["holdings_count"], 1)
        # 配置归到"混合"
        mix = [a for a in s["allocation"] if a["cat"] == "混合"][0]
        self.assertEqual(mix["amount"], 1100.0)
        self.assertEqual(mix["ratio"], 1.0)
        # 集中度 100% 预警
        self.assertEqual(s["concentration"]["top1_fund_code"], "A")
        self.assertEqual(s["concentration"]["top1_ratio"], 1.0)
        self.assertTrue(s["concentration"]["warn"])

    def test_multi_holding_allocation_and_concentration(self):
        self._fund_type("A", "混合型")
        self._fund_type("B", "指数型-股票")
        self._fund_type("C", "QDII-指数")
        self._quote("A", gsz=1.1, dwjz=1.0)
        self._quote("B", gsz=2.2, dwjz=2.0)
        self._quote("C", gsz=3.3, dwjz=3.0)
        self._hold(1, "A", 1000, 1000)   # value=1100
        self._hold(1, "B", 1000, 1000)   # shares=500 value=1100
        self._hold(1, "C", 3000, 3000)   # shares=1000 value=3300
        c = db_mod.get_conn()
        s = portfolio._compute_summary(c, 1)
        c.close()
        self.assertEqual(s["total_market_value"], 5500.0)
        # 配置占比:A混合 1100(0.2)、B指数 1100(0.2)、C QDII 3300(0.6)
        by_cat = {a["cat"]: a for a in s["allocation"]}
        self.assertEqual(by_cat["混合"]["amount"], 1100.0)
        self.assertEqual(by_cat["指数"]["amount"], 1100.0)
        self.assertEqual(by_cat["QDII"]["amount"], 3300.0)
        self.assertAlmostEqual(by_cat["QDII"]["ratio"], 0.6)
        # Σ allocation amount == total
        self.assertAlmostEqual(sum(a["amount"] for a in s["allocation"]), 5500.0)
        # 集中度:TOP1=C 占 0.6 预警,CR3=1.0
        self.assertEqual(s["concentration"]["top1_fund_code"], "C")
        self.assertAlmostEqual(s["concentration"]["top1_ratio"], 0.6)
        self.assertTrue(s["concentration"]["warn"])
        self.assertAlmostEqual(s["concentration"]["cr3"], 1.0)

    def test_prefers_nav_over_gsz(self):
        # nav(收盘官方)优先于 gsz(盘中估值)
        self._fund_type("A", "股票型")
        self._quote("A", gsz=1.1, dwjz=1.0, nav=1.2)
        self._hold(1, "A", 1000, 1000)
        c = db_mod.get_conn()
        s = portfolio._compute_summary(c, 1)
        c.close()
        # shares=1000, value=1000*1.2=1200(nav 优先)
        self.assertEqual(s["total_market_value"], 1200.0)

    def test_user_isolation(self):
        self._fund_type("A", "混合型")
        self._quote("A", gsz=1.1, dwjz=1.0)
        self._hold(1, "A", 1000, 1000)
        self._hold(2, "A", 2000, 2000)
        c = db_mod.get_conn()
        s1 = portfolio._compute_summary(c, 1)
        s2 = portfolio._compute_summary(c, 2)
        c.close()
        # user1 value=1100, user2 value=2200,互不可见
        self.assertEqual(s1["total_market_value"], 1100.0)
        self.assertEqual(s2["total_market_value"], 2200.0)
        self.assertEqual(s1["holdings_count"], 1)
        self.assertEqual(s2["holdings_count"], 1)


class TestApi(_T):
    def test_unauthorized_401(self):
        code, _ = portfolio.get_portfolio_summary(Ctx(user_id=None))
        self.assertEqual(code, 401)

    def test_summary_endpoint(self):
        self._fund_type("A", "混合型")
        self._quote("A", gsz=1.1, dwjz=1.0)
        self._hold(5, "A", 1000, 1000)
        r = portfolio.get_portfolio_summary(Ctx(user_id=5))
        self.assertEqual(r["total_market_value"], 1100.0)
        self.assertEqual(r["holdings_count"], 1)

    def test_registered_in_routes(self):
        self.assertTrue(
            any(m == "GET" and p == "/api/portfolio/summary" for m, p, _ in portfolio.ROUTES)
        )


if __name__ == "__main__":
    unittest.main()
