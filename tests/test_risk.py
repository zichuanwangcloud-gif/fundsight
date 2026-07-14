# -*- coding: utf-8 -*-
"""PRD-01 风险指标单元测试(波动率/最大回撤/夏普/索提诺/卡玛)。

基于 fund_nav_history 近1年序列纯计算,验证口径边界:
  - 数据不足 → 全 null + note
  - 单调上涨 → MDD=0、calmar None、波动率>0
  - V 型 → MDD<0、calmar>0
  - 缺 code → 400
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

    def _seed(self, code, ers):
        """按 er 序列(最早→最新,百分数)累乘生成 nav,近 len(ers) 个连续日。"""
        base = date.today() - timedelta(days=len(ers) - 1)
        c = sqlite3.connect(self.path)
        rows, nav = [], 1.0
        for i, er in enumerate(ers):
            d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append((code, d, round(nav, 4), er))
            nav = nav * (1 + er / 100)
        c.executemany(
            "INSERT INTO fund_nav_history(fund_code,nav_date,nav,equity_return) "
            "VALUES (?,?,?,?)", rows,
        )
        c.commit()
        c.close()


class TestComputeRisk(_T):
    def test_insufficient_data_all_null(self):
        self._seed("X", [0.5, 0.3, 0.4, 0.2, 0.1])  # 5 点 < 30
        c = db_mod.get_conn()
        r = returns._compute_risk(c, "X")
        c.close()
        self.assertIsNone(r["volatility"])
        self.assertIsNone(r["max_drawdown"])
        self.assertIsNone(r["sharpe"])
        self.assertEqual(r["sample_days"], 5)
        self.assertEqual(r["note"], "数据不足1年")

    def test_monotonic_up_no_drawdown(self):
        # 全正且有方差的 er → nav 单调增,MDD=0,波动率>0,calmar None
        pattern = [0.5, 0.3, 0.8, 0.2, 0.6, 0.1, 0.4, 0.9]
        self._seed("X", pattern * 5)  # 40 点
        c = db_mod.get_conn()
        r = returns._compute_risk(c, "X")
        c.close()
        self.assertEqual(r["sample_days"], 40)
        self.assertIsNotNone(r["volatility"])
        self.assertGreater(r["volatility"], 0)
        self.assertEqual(r["max_drawdown"], 0.0)
        self.assertIsNone(r["calmar"])          # MDD=0 不满足 <0
        self.assertIsNotNone(r["sharpe"])

    def test_v_shape_has_drawdown(self):
        # 前 20 日负、后 20 日正 → V 型,有回撤,calmar>0
        self._seed("X", [-0.5] * 20 + [0.8] * 20)
        c = db_mod.get_conn()
        r = returns._compute_risk(c, "X")
        c.close()
        self.assertEqual(r["sample_days"], 40)
        self.assertIsNotNone(r["volatility"])
        self.assertLess(r["max_drawdown"], 0)
        self.assertIsNotNone(r["max_drawdown_peak_date"])
        self.assertIsNotNone(r["max_drawdown_trough_date"])
        self.assertIsNotNone(r["calmar"])
        self.assertGreater(r["calmar"], 0)

    def test_uses_adj_nav_when_present(self):
        # 插入 nav 与 nav_adj 不一致,验证读 nav_adj(回落机制的相反侧)
        self._seed("X", [0.5] * 40)
        # 覆写:让 nav_adj 平滑(等比 1.0→1.2 线性),nav 故意跳跌
        c = sqlite3.connect(self.path)
        base = date.today() - timedelta(days=39)
        rows = []
        for i in range(40):
            d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(("X", d, 1.0 + i * 0.005))   # nav_adj 平滑递增
        c.executemany(
            "UPDATE fund_nav_history SET nav_adj=? WHERE fund_code='X' AND nav_date=?",
            [(r[2], r[1]) for r in rows],
        )
        c.commit()
        c.close()
        c = db_mod.get_conn()
        r = returns._compute_risk(c, "X")
        c.close()
        # 平滑递增 → MDD=0(nav_adj 口径)
        self.assertEqual(r["max_drawdown"], 0.0)


class TestApi(_T):
    def _ctx(self, code=None):
        return Ctx(params={"code": code} if code else {})

    def test_missing_code(self):
        code, _ = returns.get_risk(self._ctx())
        self.assertEqual(code, 400)

    def test_returns_risk(self):
        self._seed("X", [0.5, 0.3, 0.8, 0.2, 0.6] * 8)  # 40 点
        r = returns.get_risk(self._ctx("X"))
        self.assertEqual(r["fund_code"], "X")
        self.assertIsNotNone(r["volatility"])
        self.assertEqual(r["sample_days"], 40)
        self.assertIn("max_drawdown", r)
        self.assertIn("sharpe", r)

    def test_registered_in_routes(self):
        self.assertTrue(
            any(m == "GET" and p == "/api/fund/{code}/risk" for m, p, _ in returns.ROUTES)
        )


if __name__ == "__main__":
    unittest.main()
