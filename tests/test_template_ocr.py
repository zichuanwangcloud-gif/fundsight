# -*- coding: utf-8 -*-
"""固定模板纯代码识别通道测试 —— png_decode / 数字模板匹配 / 优雅降级 / provider 路由。

全程零网络、零真实图片库：PNG 用标准库 zlib 手工编码，字形用手造二值位图，
不依赖任何字体或 Pillow。
"""
import base64
import json
import os
import struct
import tempfile
import unittest
import zlib

from backend.datasource import png_decode, template_ocr, vision_ocr

# 归一化字形尺寸(测试用小尺寸)。
NW, NH = 3, 5
CHARS = "0123456789.,%+-"


def _glyph_pattern(k):
    """第 k 个字符 → 5x3 二值位图：顶行全 1 + 左列全 1（保证列/行都非空、
    切格为单格），内部 8 格编码 k+1 保证各字符唯一。"""
    g = [[0, 0, 0] for _ in range(NH)]
    for c in range(NW):
        g[0][c] = 1          # 顶行
    for r in range(NH):
        g[r][0] = 1          # 左列
    bits = k + 1
    idx = 0
    for r in range(1, NH):
        for c in range(1, NW):
            g[r][c] = (bits >> idx) & 1
            idx += 1
    return g


_PAT = {ch: _glyph_pattern(i) for i, ch in enumerate(CHARS)}


def _glyphs_b64():
    """构造 config 用的 glyphs：{字符: base64(NH*NW 个 0/1 字节)}。"""
    out = {}
    for ch, g in _PAT.items():
        flat = bytes(g[j][i] for j in range(NH) for i in range(NW))
        out[ch] = base64.b64encode(flat).decode("ascii")
    return out


def _render_gray(text):
    """文本 → 灰度二维列表(前景=0 暗, 背景=255 亮)，字符间留 1 空列。"""
    rows = [[] for _ in range(NH)]
    for bi, ch in enumerate(text):
        blk = _PAT[ch]
        if bi > 0:
            for r in range(NH):
                rows[r].append(0)  # 空列(二值 0)
        for r in range(NH):
            rows[r].extend(blk[r])
    return [[0 if v else 255 for v in row] for row in rows]


def _make_png(width, height, rgb_rows, color_type=2):
    """标准库手工编码 PNG(filter 0)。rgb_rows: 每像素为 (r,g,b) 或灰度 int。"""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    raw = bytearray()
    for row in rgb_rows:
        raw.append(0)
        for px in row:
            if color_type == 2:
                raw += bytes(px)
            else:
                raw.append(px)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


class TestPngDecode(unittest.TestCase):
    def test_rgb_to_gray_luma(self):
        png = _make_png(2, 2, [[(0, 0, 0), (255, 255, 255)], [(255, 0, 0), (0, 0, 255)]])
        w, h, g = png_decode.decode_png(png)
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(g[0], [0, 255])
        self.assertEqual(g[1], [76, 29])   # 红/蓝的 luma

    def test_grayscale_passthrough(self):
        png = _make_png(3, 1, [[10, 128, 240]], color_type=0)
        self.assertEqual(png_decode.decode_png(png)[2], [[10, 128, 240]])

    def test_encode_decode_roundtrip(self):
        rows = [[0, 64, 128], [255, 200, 32]]
        png = png_decode.encode_gray_png(rows)
        w, h, g = png_decode.decode_png(png)
        self.assertEqual((w, h), (3, 2))
        self.assertEqual(g, rows)

    def test_rejects_non_png(self):
        with self.assertRaises(ValueError):
            png_decode.decode_png(b"\xff\xd8\xff\xe0 JFIF fake jpeg")


class TestReadNumber(unittest.TestCase):
    def setUp(self):
        self.glyphs = {ch: template_ocr._decode_glyph(b, NW, NH)
                       for ch, b in _glyphs_b64().items()}

    def _read(self, text):
        region = _render_gray(text)
        return template_ocr._read_number(region, self.glyphs, NW, NH,
                                         dark_mode=False, tol=0.28, min_gap=1)

    def test_reads_plain_digits(self):
        self.assertEqual(self._read("12345"), "12345")

    def test_reads_amount_with_comma_and_dot(self):
        s = self._read("12,345.67")
        self.assertEqual(s, "12,345.67")
        self.assertEqual(template_ocr.parse_number(s), (12345.67, False))

    def test_signed_profit(self):
        self.assertEqual(template_ocr.parse_number(self._read("+345.67")), (345.67, True))
        self.assertEqual(template_ocr.parse_number(self._read("-500.00")), (-500.0, True))

    def test_percent(self):
        self.assertEqual(template_ocr.parse_number(self._read("-5.9%")), (-5.9, True))

    def test_blank_region_empty(self):
        blank = [[255] * 9 for _ in range(NH)]
        self.assertEqual(template_ocr._read_number(blank, self.glyphs, NW, NH,
                                                   False, 0.28, 1), "")

    def test_parse_rejects_low_confidence(self):
        self.assertEqual(template_ocr.parse_number("12?4"), (None, False))


class TestConfigAndProvider(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)
        self._cfg_path = path
        self._saved = os.environ.pop("FUNDSIGHT_OCR_TEMPLATE", None)
        self._saved_prov = os.environ.pop("FUNDSIGHT_VISION_PROVIDER", None)
        os.environ["FUNDSIGHT_OCR_TEMPLATE"] = path

    def tearDown(self):
        if os.path.exists(self._cfg_path):
            os.remove(self._cfg_path)
        for k, v in (("FUNDSIGHT_OCR_TEMPLATE", self._saved),
                     ("FUNDSIGHT_VISION_PROVIDER", self._saved_prov)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_cfg(self, cfg):
        with open(self._cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def test_is_available_false_without_config(self):
        self.assertFalse(template_ocr.is_available())

    def test_is_available_true_with_config(self):
        self._write_cfg({"glyphs": _glyphs_b64(), "regions": {"amount": [0, 0, 9, 5]}})
        self.assertTrue(template_ocr.is_available())

    def test_provider_routing(self):
        os.environ["FUNDSIGHT_VISION_PROVIDER"] = "template"
        self.assertEqual(vision_ocr.provider_name(), "template")
        self.assertFalse(vision_ocr.is_configured())          # 无配置
        self._write_cfg({"glyphs": _glyphs_b64(), "regions": {"amount": [0, 0, 9, 5]}})
        self.assertTrue(vision_ocr.is_configured())

    def test_recognize_graceful_missing_config(self):
        out = template_ocr.recognize(b"whatever")
        self.assertFalse(out["ok"])

    def test_recognize_rejects_jpeg(self):
        self._write_cfg({"glyphs": _glyphs_b64(), "regions": {"amount": [0, 0, 9, 5]},
                         "card": {"first_top": 0, "pitch": 100, "max_cards": 1}})
        out = template_ocr.recognize(b"\xff\xd8\xff\xe0 fake jpeg")
        self.assertFalse(out["ok"])
        self.assertIn("PNG", out["error"])

    def test_end_to_end_reads_amount(self):
        # 构造一张 PNG：金额区 rows0-4 是 "123"，名称区 rows5-7 背景。
        amt = _render_gray("123")                  # 5 行 x 11 列灰度
        width = len(amt[0])
        rgb = [[(v, v, v) for v in row] for row in amt]        # 金额区
        rgb += [[(255, 255, 255)] * width for _ in range(3)]  # 名称区(背景)
        png = _make_png(width, 8, rgb)
        self._write_cfg({
            "ref_width": width, "norm": [NW, NH], "min_gap": 1, "match_tol": 0.28,
            "card": {"first_top": 0, "pitch": 100, "max_cards": 1},
            "regions": {"amount": [0, 0, width, 5], "name": [0, 5, width, 3]},
            "glyphs": _glyphs_b64(),
        })
        out = template_ocr.recognize(png)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["hold_amount"], 123.0)
        self.assertTrue(out["rows"][0]["name_image"].startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
