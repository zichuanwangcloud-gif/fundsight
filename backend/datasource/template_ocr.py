# -*- coding: utf-8 -*-
"""固定模板纯代码识别通道(provider=template)—— 零模型、零第三方依赖。

适用「同一手机 + 同一 App + 同一主题」的固定版式截图。原理：模板固定 → 各字段
坐标已知，直接裁固定区域；数字字段(金额/收益/收益率)字母表只有 0-9 , . % + − 约
15 个字形，用「固定字体数字模板匹配」纯代码读出；基金名是上千汉字不适合模板匹配，
改为裁剪名称区域图片交确认页由用户搜索选定。

校准配置(data/ocr_template.json，由 scripts/ocr_calibrate.py 生成)：
  ref_width           校准样图宽度(px)；识别时按 目标图宽/ref_width 等比缩放坐标
  norm                [NW, NH] 字符归一化尺寸(默认 [16,24])
  dark_mode           深色主题(文字比背景亮)时 true，默认 false
  match_tol           单字符最大不匹配像素比(默认 0.28)，超出记 '?'
  card                {first_top, pitch, max_cards} 卡片几何(ref 像素)
  regions             {name/amount/profit/rate: [x,y,w,h]}(相对卡片顶，ref 像素)
  glyphs              {字符: base64(NH*NW 个 0/1 字节)} 归一化二值字形模板

识别不可能全对，结果一律经确认页人工核对/改正后才入库(延续「识别必经确认」约定)。
"""
import base64
import json
import os

from backend.datasource import png_decode

_DEFAULT_CFG = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "ocr_template.json")
)


def config_path():
    return os.path.abspath(os.environ.get("FUNDSIGHT_OCR_TEMPLATE") or _DEFAULT_CFG)


def load_config():
    """读校准配置；不存在或无法解析返回 None。"""
    path = config_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def is_available():
    """配置存在且含必要字段(有 glyphs 与 regions)即视为就绪。"""
    cfg = load_config()
    return bool(cfg and cfg.get("glyphs") and cfg.get("regions"))


# ---------- 像素工具(纯 Python 列表) ----------

def _crop(rows, x, y, w, h):
    """从灰度二维列表裁 (x,y,w,h)，越界自动收敛到图内。"""
    H = len(rows)
    W = len(rows[0]) if H else 0
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    return [row[x0:x1] for row in rows[y0:y1]]


def _binarize(region, dark_mode):
    """灰度区域 → 0/1 前景矩阵。阈值取 (min+max)/2；

    dark_mode 时前景=比阈值亮(文字亮)，否则前景=比阈值暗(文字暗)。
    """
    flat = [v for row in region for v in row]
    if not flat:
        return []
    lo, hi = min(flat), max(flat)
    thr = (lo + hi) / 2.0
    if dark_mode:
        return [[1 if v > thr else 0 for v in row] for row in region]
    return [[1 if v < thr else 0 for v in row] for row in region]


def _ink(binary):
    return sum(sum(row) for row in binary)


def _segment_cells(binary, min_gap=1):
    """按列投影把二值区切成字符格。返回 [(c0, c1), ...] 列区间(左闭右开)。

    连续「有前景」的列成一格；连续 >= min_gap 的空列作为分隔。
    """
    if not binary:
        return []
    h = len(binary)
    w = len(binary[0])
    col_has = [any(binary[y][x] for y in range(h)) for x in range(w)]
    cells = []
    start = None
    gap = 0
    for x in range(w):
        if col_has[x]:
            if start is None:
                start = x
            gap = 0
        else:
            if start is not None:
                gap += 1
                if gap >= min_gap:
                    cells.append((start, x - gap + 1))
                    start = None
    if start is not None:
        cells.append((start, w))
    # 过滤宽度为 0 的噪声
    return [(a, b) for a, b in cells if b > a]


def _tight_rows(cell, c0, c1):
    """取某列区间内、去掉上下空白行后的紧致二值子块。"""
    sub = [[cell[y][x] for x in range(c0, c1)] for y in range(len(cell))]
    rows_has = [i for i, row in enumerate(sub) if any(row)]
    if not rows_has:
        return sub
    top, bot = rows_has[0], rows_has[-1] + 1
    return sub[top:bot]


def _resize_bin(block, nw, nh):
    """最近邻把二值块缩放到 nw x nh。"""
    h = len(block)
    w = len(block[0]) if h else 0
    if not h or not w:
        return [[0] * nw for _ in range(nh)]
    out = []
    for j in range(nh):
        sy = min(h - 1, j * h // nh)
        row = block[sy]
        out.append([row[min(w - 1, i * w // nw)] for i in range(nw)])
    return out


def _decode_glyph(b64, nw, nh):
    """base64(NH*NW 个 0/1 字节) → 二值矩阵。"""
    raw = base64.b64decode(b64)
    return [[1 if raw[j * nw + i] else 0 for i in range(nw)] for j in range(nh)]


def _match(cell_block, glyphs, nw, nh, tol):
    """归一化字符块 → 最匹配字符；不匹配比超 tol 记 '?'。"""
    norm = _resize_bin(cell_block, nw, nh)
    total = nw * nh
    best_ch, best_diff = "?", total + 1
    for ch, tmpl in glyphs.items():
        diff = 0
        for j in range(nh):
            cr, tr = norm[j], tmpl[j]
            for i in range(nw):
                if cr[i] != tr[i]:
                    diff += 1
        if diff < best_diff:
            best_diff, best_ch = diff, ch
    if best_diff > tol * total:
        return "?"
    return best_ch


def _read_number(region, glyphs, nw, nh, dark_mode, tol, min_gap=1):
    """数字区域灰度块 → 识别字符串(可能含 ? / 符号 / 逗号 / 百分号)。"""
    binary = _binarize(region, dark_mode)
    if _ink(binary) == 0:
        return ""
    cells = _segment_cells(binary, min_gap)
    chars = []
    for c0, c1 in cells:
        block = _tight_rows(binary, c0, c1)
        chars.append(_match(block, glyphs, nw, nh, tol))
    return "".join(chars)


def parse_number(s):
    """识别字符串 → (value, signed)。含 '?' 或无数字返回 (None, False)。"""
    if not s or "?" in s:
        return None, False
    signed = s[0] in "+-＋－"
    neg = s[0] in "-－"
    cleaned = s.replace(",", "").replace("，", "").replace("%", "").replace("＋", "").replace("－", "")
    cleaned = cleaned.replace("+", "").replace("-", "").replace("¥", "").replace("￥", "").strip()
    try:
        val = float(cleaned)
    except ValueError:
        return None, False
    return (-val if neg else val), signed


# ---------- 主识别 ----------

def recognize(image_bytes):
    """PNG 截图字节 → {"ok": True, "rows": [...]} 或 {"ok": False, "error": ...}。

    每行含 hold_amount / profit / profit_rate(纯代码读出) 与 name_image(名称裁图 base64)。
    全程本地、不出网；任何异常兜底为结构化错误。
    """
    cfg = load_config()
    if not cfg:
        return {"ok": False, "error": "未配置识别模板(先运行 scripts/ocr_calibrate.py 校准)"}
    try:
        w, h, rows = png_decode.decode_png(image_bytes)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"图片解码失败: {type(e).__name__}"}

    try:
        nw, nh = cfg.get("norm", [16, 24])
        dark = bool(cfg.get("dark_mode"))
        tol = float(cfg.get("match_tol", 0.28))
        min_gap = int(cfg.get("min_gap", 1))
        card = cfg["card"]
        regions = cfg["regions"]
        glyphs = {ch: _decode_glyph(b, nw, nh) for ch, b in cfg["glyphs"].items()}

        scale = w / float(cfg.get("ref_width") or w)

        def sc(v):
            return int(round(v * scale))

        first_top = sc(card["first_top"])
        pitch = sc(card["pitch"])
        max_cards = int(card.get("max_cards", 20))
        rname = regions.get("name")
        ramt = regions["amount"]

        out_rows = []
        for i in range(max_cards):
            card_top = first_top + i * pitch
            # 金额区裁块判空：空白说明列表到底，停止。
            amt_reg = _crop(rows, sc(ramt[0]), card_top + sc(ramt[1]), sc(ramt[2]), sc(ramt[3]))
            if not amt_reg or card_top + sc(ramt[1]) >= h:
                break
            if _ink(_binarize(amt_reg, dark)) == 0:
                break

            row = {"name": None, "code": None,
                   "hold_amount": None, "profit": None, "profit_rate": None}
            amt_str = _read_number(amt_reg, glyphs, nw, nh, dark, tol, min_gap)
            row["hold_amount"], _ = parse_number(amt_str)
            for key, field in (("profit", "profit"), ("rate", "profit_rate")):
                reg_def = regions.get(key)
                if not reg_def:
                    continue
                reg = _crop(rows, sc(reg_def[0]), card_top + sc(reg_def[1]),
                            sc(reg_def[2]), sc(reg_def[3]))
                val, _ = parse_number(_read_number(reg, glyphs, nw, nh, dark, tol, min_gap))
                row[field] = val
            # 名称区裁图 → base64 PNG，交确认页人工识读选码。
            if rname:
                ncrop = _crop(rows, sc(rname[0]), card_top + sc(rname[1]),
                              sc(rname[2]), sc(rname[3]))
                if ncrop and ncrop[0]:
                    row["name_image"] = "data:image/png;base64," + base64.b64encode(
                        png_decode.encode_gray_png(ncrop)).decode("ascii")
            out_rows.append(row)
        return {"ok": True, "rows": out_rows}
    except Exception as e:  # noqa: BLE001 —— 模板/配置异常兜底降级
        return {"ok": False, "error": f"模板识别失败: {type(e).__name__}: {e}"}
