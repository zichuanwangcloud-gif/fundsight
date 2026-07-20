# -*- coding: utf-8 -*-
"""backend.api.fund_detail / backend.datasource.fund_profile / db 迁移单元测试。

覆盖:
  - db.py: fund_nav_history.equity_return 幂等迁移 + fund_profile 建表
  - fund_profile.fetch_profile(): 离线样本 mock urlopen,解析各字段
  - fund_profile.refresh_profile(): 写入/幂等覆盖
  - api.fund_detail.get_fund_detail(): 读缓存合成 {profile, series},
    days 截断,正常路径不触发抓取;缓存全空时才低频按需抓取一次。
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from backend.api import fund_detail
from backend.api._router import Ctx
from backend.datasource import fund_profile
from backend.models import db as db_mod

# 精简的 pingzhongdata 样本：含基本面 + 净值走势(equityReturn)
SAMPLE_JS = (
    'var fS_name = "测试基金C";'
    'var fund_Rate="0.15";'
    'var syl_1n="31.81";'
    'var syl_6y="4.31";'
    'var syl_3y="15.59";'
    'var syl_1y="-5.76";'
    'var Data_currentFundManager =[{"id":"1","name":"张三","fundSize":"10亿"}] ;'
    'var Data_fluctuationScale = {"categories":["2025-12-31","2026-03-31"],'
    '"series":[{"y":8.5,"mom":"1%"},{"y":10.19,"mom":"5%"}]};'
    'var Data_assetAllocation = {"series":[{"name":"股票占净比","data":[95.11,90.0]},'
    '{"name":"债券占净比","data":[2.21,3.0]},{"name":"现金占净比","data":[4.34,5.0]}],'
    '"categories":["2025-06-30","2025-09-30"]};'
    'var Data_holderStructure = {"series":[{"name":"机构持有比例","data":[0,10]},'
    '{"name":"个人持有比例","data":[100,90]},{"name":"内部持有比例","data":[0.0007,0.0001]}],'
    '"categories":["2024-06-30","2025-06-30"]};'
    'var Data_rateInSimilarType = [{"x":1718208000000,"y":1667,"sc":"2753"}];'
    'var Data_rateInSimilarPersent = [[1718208000000,39.45]];'
    'var Data_netWorthTrend = ['
    '{"x":1710201600000,"y":1.0000,"equityReturn":0},'
    '{"x":1710288000000,"y":1.0200,"equityReturn":2.0},'
    '{"x":1710374400000,"y":1.0100,"equityReturn":-0.98}'
    '];'
)


def _mock_response(text):
    resp = MagicMock()
    resp.read.return_value = text.encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


class TestDbMigrationAndSchema(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def test_fund_profile_table_created(self):
        db_mod.init_db(with_seed=False)
        conn = db_mod.get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        conn.close()
        table_names = {r["name"] for r in rows}
        self.assertIn("fund_profile", table_names)

    def test_fund_nav_history_gains_equity_return_column(self):
        db_mod.init_db(with_seed=False)
        conn = db_mod.get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fund_nav_history)")}
        conn.close()
        self.assertIn("equity_return", cols)

    def test_migration_idempotent_on_pre_existing_db(self):
        # 模拟旧库:先建表(不含 equity_return),再跑 init_db 迁移
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE fund_nav_history (fund_code TEXT, nav_date TEXT, nav REAL, "
            "PRIMARY KEY(fund_code, nav_date))"
        )
        conn.commit()
        conn.close()
        db_mod.init_db(with_seed=False)
        db_mod.init_db(with_seed=False)  # 再跑一次,不应报错(幂等)
        conn = db_mod.get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fund_nav_history)")}
        conn.close()
        self.assertIn("equity_return", cols)

    def test_fund_profile_primary_key_upsert(self):
        db_mod.init_db(with_seed=False)
        conn = db_mod.get_conn()
        conn.execute(
            "INSERT INTO fund_profile(fund_code,name,updated_at) VALUES (?,?,datetime('now'))",
            ("020608", "旧名称"),
        )
        conn.execute(
            "INSERT INTO fund_profile(fund_code,name,updated_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(fund_code) DO UPDATE SET name=excluded.name",
            ("020608", "新名称"),
        )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM fund_profile WHERE fund_code='020608'").fetchone()[0]
        name = conn.execute("SELECT name FROM fund_profile WHERE fund_code='020608'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)
        self.assertEqual(name, "新名称")


class TestFetchProfile(unittest.TestCase):
    @patch("backend.datasource.fund_profile.urllib.request.urlopen")
    def test_parses_all_fields(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(SAMPLE_JS)
        d = fund_profile.fetch_profile("020608")
        self.assertIsNotNone(d)
        self.assertEqual(d["fund_code"], "020608")
        self.assertEqual(d["name"], "测试基金C")
        self.assertEqual(d["manager"], "张三")
        self.assertEqual(d["scale"], 10.19)  # series 最后一项 y
        self.assertEqual(d["rate"], "0.15")
        self.assertEqual(d["syl_1n"], 31.81)
        self.assertEqual(d["syl_3y"], 15.59)
        self.assertEqual(d["syl_6y"], 4.31)
        self.assertEqual(d["syl_1y"], -5.76)
        self.assertEqual(d["asset_alloc_stock"], 90.0)
        self.assertEqual(d["asset_alloc_bond"], 3.0)
        self.assertEqual(d["asset_alloc_cash"], 5.0)
        self.assertEqual(d["holder_inst"], 10.0)
        self.assertEqual(d["holder_retail"], 90.0)
        self.assertEqual(d["peer_percentile"], 39.45)
        self.assertEqual(d["peer_rank"], 1667)
        self.assertEqual(d["peer_total"], 2753)

    @patch("backend.datasource.fund_profile.urllib.request.urlopen")
    def test_returns_none_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("network unreachable")
        self.assertIsNone(fund_profile.fetch_profile("020608"))

    @patch("backend.datasource.fund_profile.urllib.request.urlopen")
    def test_returns_none_when_no_recognizable_fields(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response("var unrelated = 1;")
        self.assertIsNone(fund_profile.fetch_profile("020608"))


class TestRefreshProfile(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _row(self, code="020608"):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM fund_profile WHERE fund_code=?", (code,)).fetchone()
        conn.close()
        return row

    @patch("backend.datasource.fund_profile.fetch_profile")
    def test_writes_profile(self, mock_fetch):
        mock_fetch.return_value = {
            "fund_code": "020608", "name": "测试基金C", "manager": "张三",
            "scale": 10.19, "rate": "0.15", "syl_1n": 31.81, "syl_3y": 15.59,
            "syl_6y": 4.31, "syl_1y": -5.76,
            "asset_alloc_stock": 90.0, "asset_alloc_bond": 3.0, "asset_alloc_cash": 5.0,
            "holder_inst": 10.0, "holder_retail": 90.0,
            "peer_percentile": 39.45, "peer_rank": 1667, "peer_total": 2753,
        }
        conn = sqlite3.connect(self.path)
        n = fund_profile.refresh_profile(conn, ["020608"])
        conn.close()
        self.assertEqual(n, 1)
        row = self._row()
        self.assertEqual(row["name"], "测试基金C")
        self.assertEqual(row["manager"], "张三")

    @patch("backend.datasource.fund_profile.fetch_profile")
    def test_upsert_no_duplicate(self, mock_fetch):
        mock_fetch.return_value = {
            "fund_code": "020608", "name": "A", "manager": None,
            "scale": None, "rate": None, "syl_1n": None, "syl_3y": None,
            "syl_6y": None, "syl_1y": None,
            "asset_alloc_stock": None, "asset_alloc_bond": None, "asset_alloc_cash": None,
            "holder_inst": None, "holder_retail": None,
            "peer_percentile": None, "peer_rank": None, "peer_total": None,
        }
        conn = sqlite3.connect(self.path)
        fund_profile.refresh_profile(conn, ["020608"])
        mock_fetch.return_value["name"] = "B"
        fund_profile.refresh_profile(conn, ["020608"])
        conn.close()
        row = self._row()
        self.assertEqual(row["name"], "B")

    @patch("backend.datasource.fund_profile.fetch_profile")
    def test_skip_when_fetch_none(self, mock_fetch):
        mock_fetch.return_value = None
        conn = sqlite3.connect(self.path)
        n = fund_profile.refresh_profile(conn, ["020608"])
        conn.close()
        self.assertEqual(n, 0)
        self.assertIsNone(self._row())


class TestGetFundDetail(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig = db_mod.DB_PATH
        db_mod.DB_PATH = self.path
        db_mod.init_db(with_seed=False)
        # _ensure_intraday_seed 今日 tick 为空时会触发后台 trigger_quote_for → 真实网络,
        # 单测隔离:patch 掉,默认返回 None(无副作用)。
        self._tq = patch("backend.scheduler.trigger_quote_for")
        self._tq.start()
        self.addCleanup(self._tq.stop)

    def tearDown(self):
        db_mod.DB_PATH = self._orig
        os.unlink(self.path)

    def _seed(self, code="020608"):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO fund_profile(fund_code,name,manager,scale,rate,syl_1n,"
            "syl_3y,syl_6y,syl_1y,updated_at) VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
            (code, "测试基金C", "张三", 10.19, "0.15", 31.81, 15.59, 4.31, -5.76),
        )
        rows = [
            (code, f"2026-04-{d:02d}", 1.0 + d / 100, (d % 5) - 2)
            for d in range(1, 29)
        ]
        conn.executemany(
            "INSERT INTO fund_nav_history(fund_code,nav_date,nav,equity_return) "
            "VALUES (?,?,?,?)", rows,
        )
        conn.commit()
        conn.close()

    def test_returns_profile_and_series(self):
        self._seed()
        ctx = Ctx(params={"code": "020608"})
        result = fund_detail.get_fund_detail(ctx)
        self.assertEqual(result["profile"]["name"], "测试基金C")
        self.assertEqual(result["profile"]["manager"], "张三")
        self.assertEqual(len(result["series"]), 28)
        self.assertIn("equity_return", result["series"][0])
        self.assertIn("nav", result["series"][0])
        self.assertIn("date", result["series"][0])
        # 升序
        self.assertLess(result["series"][0]["date"], result["series"][-1]["date"])

    def test_days_query_param_limits_series(self):
        self._seed()
        ctx = Ctx(params={"code": "020608"}, query={"days": ["10"]})
        result = fund_detail.get_fund_detail(ctx)
        self.assertEqual(len(result["series"]), 10)
        self.assertEqual(result["series"][-1]["date"], "2026-04-28")

    def test_default_days_180(self):
        self._seed()
        ctx = Ctx(params={"code": "020608"})
        # 只有 28 天数据,180 上限不生效,应返回全部
        result = fund_detail.get_fund_detail(ctx)
        self.assertEqual(len(result["series"]), 28)

    def test_normal_path_does_not_trigger_fetch(self):
        # 核心红线:缓存已有数据时,绝不触发抓取
        self._seed()
        ctx = Ctx(params={"code": "020608"})
        with patch("backend.datasource.fund_profile.refresh_profile") as m1, \
             patch("backend.datasource.nav_history.refresh_nav_history") as m2:
            fund_detail.get_fund_detail(ctx)
        m1.assert_not_called()
        m2.assert_not_called()

    def test_cache_miss_triggers_one_time_fetch(self):
        # 缓存完全为空(profile 无记录 + series 无记录)→ 触发一次低频抓取入库
        ctx = Ctx(params={"code": "999999"})
        with patch("backend.datasource.fund_profile.refresh_profile") as m1, \
             patch("backend.datasource.nav_history.refresh_nav_history") as m2:
            fund_detail.get_fund_detail(ctx)
        m1.assert_called_once()
        m2.assert_called_once()

    def test_partial_cache_present_does_not_trigger_fetch(self):
        # 只要 profile 或 series 任一项已有数据,即不视为"完全缺失",不抓取
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO fund_nav_history(fund_code,nav_date,nav,equity_return) "
            "VALUES (?,?,?,?)", ("020608", "2026-04-01", 1.01, 0.5),
        )
        conn.commit()
        conn.close()
        ctx = Ctx(params={"code": "020608"})
        with patch("backend.datasource.fund_profile.refresh_profile") as m1, \
             patch("backend.datasource.nav_history.refresh_nav_history") as m2:
            fund_detail.get_fund_detail(ctx)
        m1.assert_not_called()
        m2.assert_not_called()

    def test_missing_code_returns_400(self):
        result = fund_detail.get_fund_detail(Ctx(params={}))
        self.assertEqual(result, (400, {"error": "缺少基金代码"}))

    def test_registered_in_routes(self):
        self.assertEqual(len(fund_detail.ROUTES), 1)
        method, pattern, handler = fund_detail.ROUTES[0]
        self.assertEqual(method, "GET")
        self.assertEqual(pattern, "/api/fund/{code}")
        self.assertIs(handler, fund_detail.get_fund_detail)


if __name__ == "__main__":
    unittest.main()
