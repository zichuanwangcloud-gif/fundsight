# -*- coding: utf-8 -*-
"""PRD-06 同类百分位端点单元测试。"""
import os
import sqlite3
import tempfile
import unittest

from backend.api import benchmark
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

    def _seed(self, code, pct, rank, total):
        c = sqlite3.connect(self.path)
        c.execute(
            "INSERT INTO fund_profile(fund_code,name,peer_percentile,peer_rank,"
            "peer_total,updated_at) VALUES(?,?,?,?,?,datetime('now'))",
            (code, "X", pct, rank, total),
        )
        c.commit()
        c.close()


class TestPeerRank(_T):
    def test_returns_cached(self):
        self._seed("020608", 39.45, 1667, 2753)
        r = benchmark.get_peer_rank(Ctx(params={"code": "020608"}))
        self.assertEqual(r["peer_percentile"], 39.45)
        self.assertEqual(r["peer_rank"], 1667)
        self.assertEqual(r["peer_total"], 2753)
        self.assertIsNone(r["alpha"])
        self.assertIsNotNone(r["note"])

    def test_missing_code(self):
        code, _ = benchmark.get_peer_rank(Ctx(params={}))
        self.assertEqual(code, 400)

    def test_no_cache_404(self):
        code, _ = benchmark.get_peer_rank(Ctx(params={"code": "Z"}))
        self.assertEqual(code, 404)

    def test_registered_in_routes(self):
        self.assertTrue(
            any(m == "GET" and p == "/api/fund/{code}/peer-rank"
                for m, p, _ in benchmark.ROUTES)
        )


if __name__ == "__main__":
    unittest.main()
