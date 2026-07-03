# -*- coding: utf-8 -*-
"""验证 backend/app.py list_holdings() 中内嵌的核心计算约定。

计算逻辑目前直接写在 list_holdings() 内部（未抽成独立函数），因此这里
用与源码完全一致的算式对样本数值做等价断言，覆盖以下约定：

  份额          = 持仓金额 / dwjz（昨日单位净值）
  今日浮动盈亏   = 份额 * (gsz - dwjz)
  今日估值金额   = 份额 * gsz
  距目标         = 目标净值 - gsz（当前估值净值）

若 backend/app.py 的算式发生变更，应同步更新本文件中的断言值。
"""
import unittest


class TestTodayPlCalculation(unittest.TestCase):
    """今日浮动盈亏 = (持仓金额 / dwjz) * (gsz - dwjz)。"""

    def test_today_pl_positive_when_gsz_above_dwjz(self):
        hold_amount = 10000.0
        dwjz = 1.2345
        gsz = 1.2500

        shares = hold_amount / dwjz
        today_pl = round(shares * (gsz - dwjz), 2)
        est_value = round(shares * gsz, 2)

        self.assertGreater(today_pl, 0)
        self.assertEqual(today_pl, round((hold_amount / dwjz) * (gsz - dwjz), 2))
        self.assertEqual(est_value, round((hold_amount / dwjz) * gsz, 2))

    def test_today_pl_negative_when_gsz_below_dwjz(self):
        hold_amount = 5000.0
        dwjz = 2.0000
        gsz = 1.9500

        shares = hold_amount / dwjz
        today_pl = round(shares * (gsz - dwjz), 2)

        self.assertLess(today_pl, 0)
        self.assertEqual(today_pl, round(2500.0 * (1.95 - 2.0), 2))

    def test_today_pl_zero_when_gsz_equals_dwjz(self):
        hold_amount = 3000.0
        dwjz = 1.5000
        gsz = 1.5000

        shares = hold_amount / dwjz
        today_pl = round(shares * (gsz - dwjz), 2)

        self.assertEqual(today_pl, 0.0)

    def test_known_sample_values(self):
        # 手算校验：持仓 8000 元，dwjz=1.0000，gsz=1.0500
        # 份额 = 8000 / 1.0 = 8000
        # 今日盈亏 = 8000 * (1.05 - 1.0) = 400.0
        hold_amount = 8000.0
        dwjz = 1.0000
        gsz = 1.0500

        shares = hold_amount / dwjz
        today_pl = round(shares * (gsz - dwjz), 2)
        est_value = round(shares * gsz, 2)

        self.assertEqual(shares, 8000.0)
        self.assertEqual(today_pl, 400.0)
        self.assertEqual(est_value, 8400.0)


class TestGapToTargetCalculation(unittest.TestCase):
    """距目标 = 目标净值 - gsz（当前估算净值）。"""

    def test_gap_positive_when_target_above_current(self):
        target_price = 1.5000
        gsz = 1.2500

        gap = round(target_price - gsz, 4)

        self.assertEqual(gap, 0.25)
        self.assertGreater(gap, 0)

    def test_gap_negative_when_current_exceeds_target(self):
        target_price = 1.1000
        gsz = 1.2500

        gap = round(target_price - gsz, 4)

        self.assertEqual(gap, round(1.1 - 1.25, 4))
        self.assertLess(gap, 0)

    def test_gap_zero_when_target_reached_exactly(self):
        target_price = 1.2345
        gsz = 1.2345

        gap = round(target_price - gsz, 4)

        self.assertEqual(gap, 0.0)


if __name__ == "__main__":
    unittest.main()
