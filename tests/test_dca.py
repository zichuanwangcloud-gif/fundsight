# -*- coding: utf-8 -*-
"""PRD-04 定投模拟器单元测试(DCA 回测 + 一次性对比)。

覆盖:
  - 单调上涨:DCA 收益 < 一次性(diff<0)
  - V 型(微笑曲线):DCA > 一次性(diff>0)
  - 节假日顺延:invest_day 无净值 → 顺延下一有净值日,份额正确
  - 无数据 → null + note
  - 缺 code / 缺 start-end → 400
  - 路由注册
"""
import os
import sqlite3
import tempfile
import unittest

from backend.api import dca
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

    def _seed(self, code, navs_adj, start="2024-01-01"):
        """按月份序列插 fund_nav_history,nav_adj=给定值,nav 同值。"""
        from datetime import date, timedelta
        base = date.fromisoformat(start)
        c = sqlite3.connect(self.path)
        rows = []
        for i, nv in enumerate(navs_adj):
            d = (base + timedelta(days=i * 31)).strftime("%Y-%m-%d")  # 每月约隔31天
            rows.append((code, d, nv, nv))  # nav=nav_adj
        c.executemany(
            "INSERT INTO fund_nav_history(fund_code,nav_date,nav,nav_adj) "
            "VALUES (?,?,?,?)", rows,
        )
        c.commit()
        c.close()


class TestDcaSimulate(_T):
    def test_monotonic_up_dca_underperforms_lump(self):
        # 单调上涨:越买越贵,DCA 成本高于起点,收益 < 一次性
        self._seed("X", [1.0, 1.1, 1.2, 1.3, 1.4, 1.5])  # 6 个月
        ctx = Ctx(params={"code": "X"}, query={
            "start": ["2024-01-01"], "end": ["2024-06-15"],
            "amount": ["1000"], "freq": ["monthly"], "invest_day": ["1"],
        })
        r = dca.get_dca_simulate(ctx)
        self.assertEqual(r["total_invested"], 6000.0)
        self.assertIsNotNone(r["dca_return_pct"])
        self.assertIsNotNone(r["lump_return_pct"])
        # 一次性 50%,DCA < 50%
        self.assertAlmostEqual(r["lump_return_pct"], 50.0)
        self.assertLess(r["dca_return_pct"], r["lump_return_pct"])
        self.assertLess(r["diff"], 0)

    def test_v_shape_dca_outperforms_lump(self):
        # 微笑曲线:先跌后涨回到起点,DCA 在低点多买
        self._seed("X", [1.0, 0.8, 0.7, 0.8, 0.9, 1.0])
        ctx = Ctx(params={"code": "X"}, query={
            "start": ["2024-01-01"], "end": ["2024-06-15"],
            "amount": ["1000"], "freq": ["monthly"], "invest_day": ["1"],
        })
        r = dca.get_dca_simulate(ctx)
        # 一次性:start==end → 0%
        self.assertAlmostEqual(r["lump_return_pct"], 0.0)
        # DCA 在低点 0.7 多买,正收益
        self.assertGreater(r["dca_return_pct"], 0)
        self.assertGreater(r["diff"], 0)
        # 低点/高点点状
        self.assertEqual(r["nav_low"]["value"], 0.7)
        self.assertEqual(r["nav_high"]["value"], 1.0)

    def test_holiday_rollforward(self):
        # 2024-03 定投日(1号)无数据,2号有 → 顺延到 2号
        from datetime import date, timedelta
        base = date(2024, 1, 1)
        c = sqlite3.connect(self.path)
        rows = []
        navs = [1.0, 1.0, None, 1.0, 1.0, 1.0]  # 第3期(3月)1号无,2号有
        for i, nv in enumerate(navs):
            d = (base + timedelta(days=i * 31)).strftime("%Y-%m-%d")
            if nv is not None:
                rows.append(("X", d, nv, nv))
        # 3月1号无,插 3月2号
        rows.append(("X", "2024-03-02", 1.0, 1.0))
        c.executemany(
            "INSERT INTO fund_nav_history(fund_code,nav_date,nav,nav_adj) VALUES(?,?,?,?)",
            rows,
        )
        c.commit()
        c.close()
        ctx = Ctx(params={"code": "X"}, query={
            "start": ["2024-01-01"], "end": ["2024-06-15"],
            "amount": ["1000"], "freq": ["monthly"], "invest_day": ["1"],
        })
        r = dca.get_dca_simulate(ctx)
        # 6 期都投了(3月顺延到2号)
        self.assertEqual(r["periods"], 6)
        self.assertEqual(r["total_invested"], 6000.0)

    def test_no_data_returns_null(self):
        ctx = Ctx(params={"code": "Z"}, query={
            "start": ["2024-01-01"], "end": ["2024-06-15"],
        })
        r = dca.get_dca_simulate(ctx)
        self.assertIsNone(r["dca_return_pct"])
        self.assertEqual(r["note"], "区间无净值数据")

    def test_missing_code(self):
        code, _ = dca.get_dca_simulate(Ctx(params={}, query={"start": ["2024-01-01"], "end": ["2024-06-15"]}))
        self.assertEqual(code, 400)

    def test_missing_start_end(self):
        code, _ = dca.get_dca_simulate(Ctx(params={"code": "X"}, query={}))
        self.assertEqual(code, 400)

    def test_registered_in_routes(self):
        self.assertTrue(
            any(m == "GET" and p == "/api/fund/{code}/dca-simulate" for m, p, _ in dca.ROUTES)
        )


if __name__ == "__main__":
    unittest.main()
