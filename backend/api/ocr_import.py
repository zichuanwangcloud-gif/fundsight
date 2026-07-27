# -*- coding: utf-8 -*-
"""截图识别持仓 —— API 线路：上传截图 → 识别 → 模糊匹配 → 确认 → 批量导入。

接口:
  GET  /api/ocr/status      识别服务是否已配置(前端据此决定入口/给配置提示)
  POST /api/ocr/recognize   上传截图(base64) → 识别 + 匹配本地 fund_list,返回可编辑确认行
                            (只识别不写库;未配置则回 configured=False 优雅降级)
  POST /api/ocr/import      接收确认后的行 → 批量写入 holding(按 user_id 隔离)

设计要点:
- 截图走 base64-in-JSON(前端 FileReader.readAsDataURL),复用现有 JSON body 路由,
  无需给 app.py 加 multipart 解析。
- recognize 阶段绝不写库:识别不可能 100% 准,必须经用户在确认页核对/改正。
- 外部识别调用收敛在 datasource/vision_ocr.py(唯一对外接口),本模块只做匹配与落库。
"""
import base64
import difflib
import re

from backend.models.db import get_conn
from backend.datasource import vision_ocr

# 基金全称里"可有可无"的成立方式/结构词。App 简称常省略,官方注册名却带着,
# 导致同一只基金两边名称对不齐(如「…ETF发起式联接C」vs「…ETF联接C」)。
# 匹配前两边都抹掉这些噪声词,让模糊比对聚焦真正区分基金的主体名。
_NOISE_WORDS = (
    "发起式", "发起", "定期开放", "定开", "持有期",
    "(LOF)", "（LOF）", "(QDII)", "（QDII）", "(FOF)", "（FOF）",
)
# 相似度阈值:≥ SUGGEST 才作为"疑似"给出预选(仍需用户确认);
# 只有 ≥ FLOOR 的候选才进下拉,避免塞入明显不相关项。
_FUZZY_SUGGEST = 0.55
_FUZZY_FLOOR = 0.34


def _norm_name(s):
    """归一化基金名用于模糊比对:去噪声词、去空白/括号/间隔号,统一大写。"""
    s = (s or "").strip()
    for w in _NOISE_WORDS:
        s = s.replace(w, "")
    s = re.sub(r"[\s()（）·・]", "", s)
    return s.upper()

# 上传体量上限(解码后字节)。8MB 足够手机截图,防超大 body 拖垮内存。
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _decode_data_url(image):
    """'data:image/png;base64,xxxx' 或纯 base64 → (bytes, mime)。失败返回 (None, None)。"""
    if not image or not isinstance(image, str):
        return None, None
    mime = "image/png"
    b64 = image
    m = re.match(r"^data:(image/[a-zA-Z.+-]+);base64,(.*)$", image, re.DOTALL)
    if m:
        mime = m.group(1)
        b64 = m.group(2)
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception:  # noqa: BLE001
        return None, None
    return raw, mime


def _fuzzy_candidates(conn, name):
    """名称对不齐(如插入了"发起式")时的兜底:去噪归一化 + 相似度排序。

    先用归一名的前几字做锚点 LIKE 召回同系基金(避免全表扫描),再用
    difflib 比相似度排序。返回按相似度降序的 [(score, row_dict), ...]。
    """
    target = _norm_name(name)
    if len(target) < 3:
        return []
    anchor = target[:4]  # 通常含公司+主题起始,足够召回同系基金
    rows = conn.execute(
        "SELECT fund_code,name,fund_type FROM fund_list WHERE name LIKE ? LIMIT 200",
        (f"%{anchor}%",),
    ).fetchall()
    scored = []
    for r in rows:
        score = difflib.SequenceMatcher(None, target, _norm_name(r["name"])).ratio()
        if score >= _FUZZY_FLOOR:
            scored.append((score, dict(r)))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


def _match_fund(name, code):
    """按识别到的 name/code 查本地 fund_list,返回 (matched_code, candidates, match_type)。

    优先级:code 精确 → name 原样子串精确 → 去噪相似度模糊(兜底)。
    match_type ∈ {"exact","fuzzy",None}:fuzzy 为"疑似",预选但需用户在确认页核对。
    candidates 供前端下拉纠正。纯读本地表(参照 app.search_funds),不触外部接口。
    """
    conn = get_conn()
    matched = None
    match_type = None
    candidates = []
    try:
        code = (code or "").strip()
        if code:
            row = conn.execute(
                "SELECT fund_code,name,fund_type FROM fund_list WHERE fund_code=?", (code,)
            ).fetchone()
            if row:
                matched = row["fund_code"]
                match_type = "exact"
                candidates.append(dict(row))
        name = (name or "").strip()
        if name:
            like = f"%{name}%"
            rows = conn.execute(
                "SELECT fund_code,name,fund_type FROM fund_list WHERE name LIKE ? LIMIT 5",
                (like,),
            ).fetchall()
            for r in rows:
                d = dict(r)
                if d["fund_code"] not in [c["fund_code"] for c in candidates]:
                    candidates.append(d)
            if rows and matched is None:
                matched = rows[0]["fund_code"]
                match_type = "exact"
            # 原样子串匹配不到 → 去噪相似度兜底(处理"发起式"插入、OCR 错字等)
            if matched is None:
                scored = _fuzzy_candidates(conn, name)
                for _, d in scored:
                    if d["fund_code"] not in [c["fund_code"] for c in candidates]:
                        candidates.append(d)
                if scored and scored[0][0] >= _FUZZY_SUGGEST:
                    matched = scored[0][1]["fund_code"]
                    match_type = "fuzzy"
    finally:
        conn.close()
    return matched, candidates[:5], match_type


def recognize(image, user_id):
    """识别 + 匹配。返回 dict(含 configured/rows 或 error),不写库。"""
    if not vision_ocr.is_configured():
        return {"configured": False, "rows": []}
    raw, mime = _decode_data_url(image)
    if raw is None:
        return {"configured": True, "error": "图片解析失败"}
    if len(raw) > MAX_IMAGE_BYTES:
        return {"configured": True, "error": "图片过大(上限 8MB)"}
    result = vision_ocr.recognize_holdings(raw, mime)
    if not result.get("ok"):
        return {"configured": True, "error": result.get("error") or "识别失败"}
    rows = []
    for r in result.get("rows", []):
        matched, candidates, match_type = _match_fund(r.get("name"), r.get("code"))
        # 成本反推:成本 = 持仓金额 - 收益(两者都有时)
        cost = r.get("cost")
        if cost is None and r.get("hold_amount") is not None and r.get("profit") is not None:
            cost = round(r["hold_amount"] - r["profit"], 2)
        row = {
            "name": r.get("name"),
            "code": r.get("code"),
            "hold_amount": r.get("hold_amount"),
            "profit": r.get("profit"),
            "profit_rate": r.get("profit_rate"),
            "cost_amount": cost,
            "matched_code": matched,
            "match_type": match_type,
            "candidates": candidates,
        }
        # 固定模板通道无法读基金名,改附名称裁图(base64),交确认页由用户核对搜索。
        if r.get("name_image"):
            row["name_image"] = r["name_image"]
        rows.append(row)
    return {"configured": True, "rows": rows}


def import_holdings(rows, user_id):
    """把确认后的行批量写入 holding(按 user_id)。返回成功写入条数。

    每行需带 fund_code(用户在确认页选定)与 hold_amount;cost_amount 未给时用
    hold_amount - profit 反推。插入语法与字段口径对齐 app.add_holding。
    """
    if not isinstance(rows, list):
        return 0
    inserted = 0
    codes = []
    conn = get_conn()
    try:
        for r in rows:
            if not isinstance(r, dict):
                continue
            code = (r.get("fund_code") or "").strip()
            if not code:
                continue
            hold = _num(r.get("hold_amount"))
            cost = _num(r.get("cost_amount"))
            profit = _num(r.get("profit"))
            if cost is None and hold is not None and profit is not None:
                cost = round(hold - profit, 2)
            conn.execute(
                "INSERT INTO holding(user_id,fund_code,hold_amount,cost_amount,target_rate,"
                "target_price,stop_profit,stop_loss,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))",
                (user_id, code, hold, cost,
                 _num(r.get("target_rate")), _num(r.get("target_price")),
                 _num(r.get("stop_profit")), _num(r.get("stop_loss"))),
            )
            inserted += 1
            codes.append(code)
        conn.commit()
    finally:
        conn.close()
    # 补空窗:新增持仓后后台拉一次估值,用户几秒内可见(失败由定时任务兜底)。
    # 懒加载 scheduler,避免测试环境无需起后台线程时的强依赖。
    if codes:
        try:
            from backend.scheduler import (
                trigger_quote_for, trigger_nav_for, trigger_history_for,
            )
            for c in set(codes):
                trigger_quote_for(c)
                trigger_nav_for(c)      # 官方净值(nav/nav_prev/名称):不依赖 fundgz,导入后真实盈亏即时可算
                trigger_history_for(c)
        except Exception:  # noqa: BLE001
            pass
    return inserted


# ---- 路由 handler ----

def _h_status(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    return {"configured": vision_ocr.is_configured(), "provider": vision_ocr.provider_name()}


def _h_recognize(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    return recognize(ctx.body.get("image"), ctx.user_id)


def _h_import(ctx):
    if ctx.user_id is None:
        return (401, {"error": "unauthorized"})
    n = import_holdings(ctx.body.get("rows"), ctx.user_id)
    return {"ok": True, "imported": n}


ROUTES = [
    ("GET", "/api/ocr/status", _h_status),
    ("POST", "/api/ocr/recognize", _h_recognize),
    ("POST", "/api/ocr/import", _h_import),
]
