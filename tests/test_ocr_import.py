# -*- coding: utf-8 -*-
"""截图识别持仓测试 —— 覆盖 vision_ocr 解析、is_configured、模糊匹配、
recognize 未配置降级、import 批量写入 + 成本反推 + 用户隔离。

沿用 test_transactions.py 的「临时 DB 文件 + monkeypatch db.DB_PATH」手法。
全程不发真实网络请求：识别层用离线样本 / monkeypatch，不打外部接口。
"""
import os
import tempfile
import unittest

from backend.models import db
from backend import scheduler
from backend.datasource import vision_ocr
from backend.api import ocr_import
from backend.api._router import Ctx


# ---- 离线样本响应体(两种 provider) ----
_MODEL_TEXT = (
    '[{"name":"易方达蓝筹精选混合","code":"005827","hold_amount":"12,345.67",'
    '"profit":345.67,"profit_rate":2.9},'
    '{"name":"招商中证白酒指数","code":null,"hold_amount":8000,"profit":-500,"profit_rate":-5.9}]'
)
_ANTHROPIC_RESP = {"content": [{"type": "text", "text": _MODEL_TEXT}]}
_OPENAI_RESP = {"choices": [{"message": {"content": "```json\n" + _MODEL_TEXT + "\n```"}}]}


class OcrTestBase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        self._tmp_path = path
        self._orig_path = db.DB_PATH
        db.DB_PATH = path
        db.init_db(with_seed=False)
        conn = db.get_conn()
        conn.executemany(
            "INSERT INTO fund_list(fund_code,name,pinyin,fund_type,synced_at) "
            "VALUES (?,?,?,?,datetime('now','localtime'))",
            [
                ("005827", "易方达蓝筹精选混合", "yfdlcjxhh", "混合"),
                ("161725", "招商中证白酒指数", "zszzbjzs", "指数"),
                ("020608", "南方中证机器人ETF发起联接C", "nf", "指数"),
            ],
        )
        conn.commit()
        conn.close()
        # 存量导入会 best-effort 触发后台估值拉取(真实网络)；单测里 stub 掉，
        # 保证「全程不发真实网络」，也避免后台线程在临时库销毁后写只读库报噪声。
        self._orig_trig_q = scheduler.trigger_quote_for
        self._orig_trig_h = scheduler.trigger_history_for
        scheduler.trigger_quote_for = lambda *a, **k: None
        scheduler.trigger_history_for = lambda *a, **k: None
        # 保存并清理识别相关环境变量，避免宿主机真实密钥干扰
        self._saved_env = {k: os.environ.pop(k, None) for k in (
            "FUNDSIGHT_VISION_API_KEY", "ANTHROPIC_API_KEY",
            "FUNDSIGHT_VISION_PROVIDER", "FUNDSIGHT_VISION_ENDPOINT",
            "FUNDSIGHT_VISION_MODEL",
        )}

    def tearDown(self):
        scheduler.trigger_quote_for = self._orig_trig_q
        scheduler.trigger_history_for = self._orig_trig_h
        db.DB_PATH = self._orig_path
        if os.path.exists(self._tmp_path):
            os.remove(self._tmp_path)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestParseModelJson(OcrTestBase):
    def test_plain_array(self):
        rows = vision_ocr._parse_model_json(_MODEL_TEXT)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["code"], "005827")
        self.assertEqual(rows[0]["hold_amount"], 12345.67)  # 去千分位逗号
        self.assertIsNone(rows[1]["code"])                  # null → None

    def test_strips_code_fence(self):
        fenced = "```json\n" + _MODEL_TEXT + "\n```"
        self.assertEqual(len(vision_ocr._parse_model_json(fenced)), 2)

    def test_tolerates_surrounding_text(self):
        noisy = "好的，识别结果如下：\n" + _MODEL_TEXT + "\n希望有帮助。"
        self.assertEqual(len(vision_ocr._parse_model_json(noisy)), 2)

    def test_garbage_returns_empty(self):
        self.assertEqual(vision_ocr._parse_model_json("抱歉无法识别"), [])
        self.assertEqual(vision_ocr._parse_model_json(""), [])

    def test_extract_text_both_providers(self):
        self.assertEqual(vision_ocr._extract_text("anthropic", _ANTHROPIC_RESP), _MODEL_TEXT)
        self.assertIn(_MODEL_TEXT, vision_ocr._extract_text("openai", _OPENAI_RESP))


class TestIsConfigured(OcrTestBase):
    def test_toggles_with_env(self):
        self.assertFalse(vision_ocr.is_configured())
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        self.assertTrue(vision_ocr.is_configured())
        os.environ.pop("ANTHROPIC_API_KEY")
        os.environ["FUNDSIGHT_VISION_API_KEY"] = "k"
        self.assertTrue(vision_ocr.is_configured())


class TestMatchFund(OcrTestBase):
    def test_name_fuzzy_match(self):
        matched, cands = ocr_import._match_fund("易方达蓝筹", None)
        self.assertEqual(matched, "005827")
        self.assertTrue(any(c["fund_code"] == "005827" for c in cands))

    def test_code_takes_priority(self):
        # name 指向白酒，但 code 精确给机器人 → 以 code 命中为准
        matched, _ = ocr_import._match_fund("招商中证白酒指数", "020608")
        self.assertEqual(matched, "020608")

    def test_no_match(self):
        matched, cands = ocr_import._match_fund("查无此基金xyz", None)
        self.assertIsNone(matched)
        self.assertEqual(cands, [])


class TestRecognizeGraceful(OcrTestBase):
    def test_not_configured_no_network(self):
        # 未配置密钥：直接降级返回 configured=False，且不触网
        out = ocr_import.recognize("data:image/png;base64,QUJD", user_id=1)
        self.assertEqual(out, {"configured": False, "rows": []})

    def test_configured_uses_offline_recognizer(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        # monkeypatch 识别层，避免真实网络
        orig = vision_ocr.recognize_holdings
        vision_ocr.recognize_holdings = lambda b, m="image/png": {
            "ok": True, "rows": vision_ocr._parse_model_json(_MODEL_TEXT)}
        try:
            out = ocr_import.recognize("data:image/png;base64,QUJD", user_id=1)
        finally:
            vision_ocr.recognize_holdings = orig
        self.assertTrue(out["configured"])
        self.assertEqual(len(out["rows"]), 2)
        r0 = out["rows"][0]
        self.assertEqual(r0["matched_code"], "005827")
        # 成本反推：12345.67 - 345.67 = 12000.0
        self.assertEqual(r0["cost_amount"], 12000.0)

    def test_oversize_image_rejected(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        import base64
        big = base64.b64encode(b"x" * (ocr_import.MAX_IMAGE_BYTES + 10)).decode()
        out = ocr_import.recognize("data:image/png;base64," + big, user_id=1)
        self.assertIn("error", out)


class TestImportHoldings(OcrTestBase):
    def _holdings(self, user_id):
        conn = db.get_conn()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM holding WHERE user_id=? ORDER BY id", (user_id,)).fetchall()]
        conn.close()
        return rows

    def test_bulk_insert_and_cost_derivation(self):
        rows = [
            {"fund_code": "005827", "hold_amount": "12345.67", "cost_amount": "12000"},
            {"fund_code": "161725", "hold_amount": "8000", "profit": "-500"},  # 无成本→反推
            {"fund_code": "", "hold_amount": "999"},  # 无代码→跳过
        ]
        n = ocr_import.import_holdings(rows, user_id=7)
        self.assertEqual(n, 2)
        hs = self._holdings(7)
        self.assertEqual(len(hs), 2)
        self.assertEqual(hs[0]["fund_code"], "005827")
        self.assertEqual(hs[0]["cost_amount"], 12000.0)
        # 成本反推：8000 - (-500) = 8500
        self.assertEqual(hs[1]["cost_amount"], 8500.0)

    def test_user_isolation(self):
        ocr_import.import_holdings([{"fund_code": "005827", "hold_amount": "100"}], user_id=1)
        self.assertEqual(len(self._holdings(1)), 1)
        self.assertEqual(len(self._holdings(2)), 0)

    def test_handler_requires_auth(self):
        self.assertEqual(ocr_import._h_import(Ctx(body={"rows": []}, user_id=None))[0], 401)
        self.assertEqual(ocr_import._h_recognize(Ctx(body={}, user_id=None))[0], 401)
        self.assertEqual(ocr_import._h_status(Ctx(user_id=None))[0], 401)

    def test_status_handler(self):
        out = ocr_import._h_status(Ctx(user_id=1))
        self.assertFalse(out["configured"])
        self.assertEqual(out["provider"], "anthropic")


def _box(x0, y0, x1, y1):
    """左上/右下 → RapidOCR 的 4 点框。"""
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _alipay_like_ocr():
    """合成一张支付宝式持仓截图的 OCR 结果（两只基金），不依赖真实 RapidOCR。

    每只基金：名称行（含 6 位代码）→ 持有收益行（带正负号 + 收益率%）→ 持仓金额行。
    """
    return [
        # 基金 1
        [_box(40, 90, 240, 118), "易方达蓝筹精选混合", 0.99],
        [_box(280, 92, 360, 116), "005827", 0.98],
        [_box(40, 140, 130, 166), "持有收益", 0.97],
        [_box(150, 140, 250, 166), "+345.67", 0.98],
        [_box(270, 140, 330, 166), "收益率", 0.97],
        [_box(340, 140, 420, 166), "+2.9%", 0.98],
        [_box(40, 182, 130, 208), "持仓金额", 0.97],
        [_box(150, 182, 280, 208), "12,345.67", 0.98],
        # 基金 2
        [_box(40, 290, 240, 318), "招商中证白酒指数", 0.99],
        [_box(40, 340, 130, 366), "持有收益", 0.97],
        [_box(150, 340, 250, 366), "-500.00", 0.98],
        [_box(270, 340, 330, 366), "收益率", 0.97],
        [_box(340, 340, 420, 366), "-5.90%", 0.98],
        [_box(40, 382, 130, 408), "持仓金额", 0.97],
        [_box(150, 382, 280, 408), "8,000.00", 0.98],
    ]


class TestLocalLayoutParser(OcrTestBase):
    """本地 OCR 版面启发式解析（纯函数，不需安装 rapidocr）。"""

    def test_parse_two_funds(self):
        rows = vision_ocr._parse_ocr_result(_alipay_like_ocr())
        self.assertEqual(len(rows), 2)
        f1, f2 = rows
        self.assertEqual(f1["name"], "易方达蓝筹精选混合")
        self.assertEqual(f1["code"], "005827")
        self.assertEqual(f1["hold_amount"], 12345.67)   # 无符号金额取最大 → 持仓金额
        self.assertEqual(f1["profit"], 345.67)          # 带 + 号 → 收益
        self.assertEqual(f1["profit_rate"], 2.9)
        self.assertEqual(f2["name"], "招商中证白酒指数")
        self.assertEqual(f2["hold_amount"], 8000.0)
        self.assertEqual(f2["profit"], -500.0)          # 带 - 号 → 收益(负)
        self.assertEqual(f2["profit_rate"], -5.9)

    def test_labels_not_taken_as_name(self):
        # 「持有收益」「持仓金额」等纯标签行不应被当成基金名而新开记录。
        rows = vision_ocr._parse_ocr_result(_alipay_like_ocr())
        self.assertEqual([r["name"] for r in rows],
                         ["易方达蓝筹精选混合", "招商中证白酒指数"])

    def test_empty_and_garbage(self):
        self.assertEqual(vision_ocr._parse_ocr_result([]), [])
        self.assertEqual(vision_ocr._parse_ocr_result([[None, "", 0.1]]), [])
        # 只有数字没有基金名 → 无记录
        self.assertEqual(vision_ocr._parse_ocr_result([[_box(0, 0, 50, 20), "123.45", 0.9]]), [])

    def test_amount_value_signed_and_clean(self):
        self.assertEqual(vision_ocr._amount_value("+345.67"), (345.67, True))
        self.assertEqual(vision_ocr._amount_value("-500.00"), (-500.0, True))
        self.assertEqual(vision_ocr._amount_value("12,345.67"), (12345.67, False))
        self.assertEqual(vision_ocr._amount_value("¥8,000"), (8000.0, False))


class TestLocalProviderDegrade(OcrTestBase):
    """provider=local 且未安装 RapidOCR 时的优雅降级（不触网、不崩）。"""

    def setUp(self):
        super().setUp()
        os.environ["FUNDSIGHT_VISION_PROVIDER"] = "local"

    def test_is_configured_reflects_engine_availability(self):
        # 测试环境未装 rapidocr → local 视为未就绪。
        expected = vision_ocr._rapidocr_available()
        self.assertEqual(vision_ocr.is_configured(), expected)

    def test_recognize_holdings_graceful_when_missing(self):
        if vision_ocr._rapidocr_available():
            self.skipTest("已安装 rapidocr，跳过缺依赖降级用例")
        out = vision_ocr.recognize_holdings(b"\x89PNG fake", "image/png")
        self.assertFalse(out["ok"])
        self.assertIn("OCR", out["error"])

    def test_import_recognize_returns_not_configured_when_missing(self):
        if vision_ocr._rapidocr_available():
            self.skipTest("已安装 rapidocr，跳过缺依赖降级用例")
        out = ocr_import.recognize("data:image/png;base64,QUJD", user_id=1)
        self.assertEqual(out, {"configured": False, "rows": []})


if __name__ == "__main__":
    unittest.main()
