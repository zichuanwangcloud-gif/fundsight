# -*- coding: utf-8 -*-
"""M10A 收益分析深化测试 —— 分批买入成本曲线 + 阶段收益归因。

基于 fund_transaction(只读,基金维度不按用户隔离) + fund_nav_history(只读)
计算,不落新表、不画连续走势(红线:只做点状/分批)。沿用 test_returns.py 的
「临时 DB 文件 + monkeypatch db.DB_PATH」手法,不起真实 HTTP。
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from backend.api import returns
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

    def _nav(self, code, days_ago, nav):
        d = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        c = sqlite3.connect(self.path)
        c.execute(
            "INSERT OR REPLACE INTO fund_nav_history(fund_code,nav_date,nav) VALUES(?,?,?)",
            (code, d, nav),
        )
        c.commit()
        c.close()

    def _buy(self, code, days_ago, shares, price, user_id=1):
        d = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        c = sqlite3.connect(self.path)
        c.execute(
            "INSERT INTO fund_transaction(user_id,fund_code,action,shares,price,amount,trade_date)"
            " VALUES(?,?,?,?,?,?,?)",
            (user_id, code, "buy", shares, price, shares * price, d),
        )
        c.commit()
        c.close()

    def _ctx(self, code, user_id=1):
        return Ctx(params={"code": code}, user_id=user_id)


class TestCostCurve(_T):
    def test_missing_code(self):
        code, _ = returns.get_cost_curve(self._ctx(""))
        self.assertEqual(code, 400)

    def test_unauth_401(self):
        code, _ = returns.get_cost_curve(Ctx(params={"code": "X"}, user_id=None))
        self.assertEqual(code, 401)

    def test_no_buy_tx_empty(self):
        # 无交易记录 → points 为空列表(前端不报错)
        r = returns.get_cost_curve(self._ctx("X"))
        self.assertEqual(r["fund_code"], "X")
        self.assertEqual(r["points"], [])

    def test_sells_ignored(self):
        # 只有卖出流水 → 视同无买入,points 为空
        c = sqlite3.connect(self.path)
        d = date.today().strftime("%Y-%m-%d")
        c.execute(
            "INSERT INTO fund_transaction(user_id,fund_code,action,shares,price,amount,trade_date)"
            " VALUES(?,?,?,?,?,?,?)",
            (1, "X", "sell", 50, 2.0, 100.0, d),
        )
        c.commit()
        c.close()
        r = returns.get_cost_curve(self._ctx("X"))
        self.assertEqual(r["points"], [])

    def test_full_data_weighted(self):
        # 买1: 100 份 @1.0=100; 买2: 100 份 @2.0=200 → 加权 1.5
        self._buy("X", 100, 100, 1.0)
        self._buy("X", 50, 100, 2.0)
        r = returns.get_cost_curve(self._ctx("X"))
        pts = r["points"]
        self.assertEqual(len(pts), 2)
        self.assertAlmostEqual(pts[0]["cost_basis"], 100.0)
        self.assertAlmostEqual(pts[0]["weighted_price"], 1.0)
        self.assertAlmostEqual(pts[0]["shares"], 100.0)
        self.assertAlmostEqual(pts[1]["cost_basis"], 300.0)
        self.assertAlmostEqual(pts[1]["weighted_price"], 1.5)
        self.assertAlmostEqual(pts[1]["shares"], 200.0)
        # 按日期升序
        self.assertLessEqual(pts[0]["date"], pts[1]["date"])


class TestAttribution(_T):
    def test_missing_code(self):
        code, _ = returns.get_returns_attribution(self._ctx(""))
        self.assertEqual(code, 400)

    def test_unauth_401(self):
        code, _ = returns.get_returns_attribution(Ctx(params={"code": "X"}, user_id=None))
        self.assertEqual(code, 401)

    def test_no_data_all_null(self):
        # 无交易、无净值 → 各阶段 null
        r = returns.get_returns_attribution(self._ctx("X"))
        self.assertEqual(r["fund_code"], "X")
        for k in ("m1", "m3", "ytd", "max"):
            self.assertIsNone(r["periods"][k], f"{k} 应为 null")

    def test_no_nav_periods_null(self):
        # 有买入但无净值起点 → m1/m3/ytd/max 全 null
        self._buy("X", 100, 100, 1.0)
        r = returns.get_returns_attribution(self._ctx("X"))
        for k in ("m1", "m3", "ytd", "max"):
            self.assertIsNone(r["periods"][k])

    def test_max_full_attribution(self):
        # 买1: 100@1.0; 买2: 100@2.0; 期末净值 3.0
        # batch1 贡献 = 100×(3-1)=200; batch2 = 100×(3-2)=100; total=300
        self._nav("X", 200, 1.0)   # 早期净值(供 m1/m3/ytd 起点不存在 → null,max 不依赖起点)
        self._buy("X", 100, 100, 1.0)
        self._buy("X", 50, 100, 2.0)
        self._nav("X", 0, 3.0)      # 期末净值
        r = returns.get_returns_attribution(self._ctx("X"))
        mx = r["periods"]["max"]
        self.assertIsNotNone(mx)
        self.assertAlmostEqual(mx["total"], 300.0)
        self.assertEqual(len(mx["batches"]), 2)
        # 贡献占比之和 ≈ 1
        ratios = [b["ratio"] for b in mx["batches"]]
        self.assertAlmostEqual(sum(ratios), 1.0, places=2)
        # batch1 贡献更大
        contribs = {b["date"]: b["contribution"] for b in mx["batches"]}
        self.assertTrue(any(abs(v - 200.0) < 1e-6 for v in contribs.values()))

    def test_period_nav_missing_null_but_max_ok(self):
        # 期末净值存在,但 m1/m3/ytd 起点缺失 → 这些阶段 null,max 有值
        self._buy("X", 5, 100, 1.0)
        self._nav("X", 0, 3.0)
        r = returns.get_returns_attribution(self._ctx("X"))
        self.assertIsNone(r["periods"]["m1"])
        self.assertIsNone(r["periods"]["m3"])
        self.assertIsNone(r["periods"]["ytd"])
        self.assertIsNotNone(r["periods"]["max"])

    def test_m1_window_excludes_recent_batch(self):
        # 远期批次(100天前)进入 m1;近期批次(5天前)被 m1 排除
        self._nav("X", 200, 1.0)   # 起点 nav 存在,使 m1 不因起点缺失而 null
        self._buy("X", 100, 100, 1.0)   # 100 天前 → 进入 m1
        self._buy("X", 5, 100, 2.0)     # 5 天前 → 不进入 m1
        self._nav("X", 0, 3.0)
        r = returns.get_returns_attribution(self._ctx("X"))
        m1 = r["periods"]["m1"]
        self.assertIsNotNone(m1)
        self.assertEqual(len(m1["batches"]), 1)
        self.assertAlmostEqual(m1["batches"][0]["contribution"], 200.0)
        # max 仍含两批
        self.assertEqual(len(r["periods"]["max"]["batches"]), 2)


if __name__ == "__main__":
    unittest.main()
