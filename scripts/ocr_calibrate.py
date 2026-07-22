# -*- coding: utf-8 -*-
"""固定模板 OCR 一次性校准脚本 —— 从一张样图 + 真值捕获数字字形模板。

固定模板通道(FUNDSIGHT_VISION_PROVIDER=template)需要一份校准配置
data/ocr_template.json：卡片几何、各字段区域坐标、以及数字字形模板(glyphs)。
本脚本负责生成它 —— 你只需提供一张样图 PNG 和一个描述版式+首卡真值的输入 JSON。

用法：
  python3 scripts/ocr_calibrate.py 样图.png calib_input.json [-o data/ocr_template.json]

calib_input.json 示例(坐标均为样图像素；regions 的 x/y 相对「卡片顶」)：
{
  "ref_width": 1080,
  "norm": [16, 24],
  "dark_mode": false,
  "match_tol": 0.28,
  "min_gap": 1,
  "card": {"first_top": 320, "pitch": 210, "max_cards": 12},
  "regions": {
    "name":   [150, 20, 520, 60],
    "amount": [700, 20, 320, 60],
    "profit": [700, 90, 320, 50],
    "rate":   [700, 150, 300, 50]
  },
  "truth": {"amount": "12,345.67", "profit": "+345.67", "rate": "+2.9%"}
}

脚本裁首卡的 amount/profit/rate 区域 → 切字符格 → 与 truth 中的字符逐一配对 →
归一化后存为 glyphs 模板；name 不入模板(始终裁图交确认页人工选)。生成后：
  export FUNDSIGHT_VISION_PROVIDER=template   # 启用固定模板通道
"""
import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.datasource import png_decode, template_ocr  # noqa: E402


def _capture_glyphs(rows, calib):
    """按 truth 逐区域切格配对，返回 {字符: base64 模板}。"""
    nw, nh = calib.get("norm", [16, 24])
    dark = bool(calib.get("dark_mode"))
    min_gap = int(calib.get("min_gap", 1))
    card = calib["card"]
    regions = calib["regions"]
    truth = calib.get("truth", {})
    first_top = card["first_top"]

    glyphs = {}
    for key, text in truth.items():
        reg_def = regions.get(key)
        if not reg_def or not text:
            continue
        x, y, w, h = reg_def
        region = template_ocr._crop(rows, x, first_top + y, w, h)
        binary = template_ocr._binarize(region, dark)
        cells = template_ocr._segment_cells(binary, min_gap)
        if len(cells) != len(text):
            print(f"  ⚠ 区域 {key}: 切出 {len(cells)} 格，但真值 '{text}' 有 "
                  f"{len(text)} 字符 —— 按较短长度配对，建议核对区域框/间隙。")
        for (c0, c1), ch in zip(cells, text):
            if ch in glyphs:
                continue  # 同字符已捕获，保留首个
            block = template_ocr._tight_rows(binary, c0, c1)
            norm = template_ocr._resize_bin(block, nw, nh)
            flat = bytes(norm[j][i] for j in range(nh) for i in range(nw))
            glyphs[ch] = base64.b64encode(flat).decode("ascii")
    return glyphs


def main():
    ap = argparse.ArgumentParser(description="固定模板 OCR 校准：捕获数字字形模板")
    ap.add_argument("sample", help="样图 PNG 路径")
    ap.add_argument("calib", help="版式+真值输入 JSON 路径")
    ap.add_argument("-o", "--out", default=None, help="输出模板路径(默认 data/ocr_template.json)")
    args = ap.parse_args()

    with open(args.sample, "rb") as f:
        image = f.read()
    with open(args.calib, "r", encoding="utf-8") as f:
        calib = json.load(f)

    w, h, rows = png_decode.decode_png(image)
    print(f"样图解码成功: {w}x{h}")
    if calib.get("ref_width") and calib["ref_width"] != w:
        print(f"  ⚠ ref_width={calib['ref_width']} 与样图宽 {w} 不一致；"
              f"建议把 ref_width 设为样图实际宽度。")

    glyphs = _capture_glyphs(rows, calib)
    if not glyphs:
        print("✗ 未捕获到任何字形，请检查 regions 坐标与 truth。")
        sys.exit(1)
    print(f"已捕获字形: {''.join(sorted(glyphs))}  (共 {len(glyphs)} 个)")

    out_cfg = {
        "ref_width": calib.get("ref_width", w),
        "norm": calib.get("norm", [16, 24]),
        "dark_mode": bool(calib.get("dark_mode")),
        "match_tol": calib.get("match_tol", 0.28),
        "min_gap": int(calib.get("min_gap", 1)),
        "card": calib["card"],
        "regions": calib["regions"],
        "glyphs": glyphs,
    }
    out_path = os.path.abspath(args.out or template_ocr._DEFAULT_CFG)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_cfg, f, ensure_ascii=False, indent=2)
    print(f"✓ 模板已写入 {out_path}")
    print("  启用: export FUNDSIGHT_VISION_PROVIDER=template")


if __name__ == "__main__":
    main()
