# -*- coding: utf-8 -*-
"""截图识别持仓 —— 唯一对外接口：调多模态视觉大模型抽取持仓结构化数据。

延续本项目「抓取层是唯一对外接口」+「零第三方依赖」+「优雅降级」三条约束：
- 只用标准库（urllib / json / base64 / ssl），不引入任何 pip 依赖。
- 识别引擎可配置：默认走 Claude（anthropic），也可指向任意 OpenAI 兼容 / 自建视觉服务。
- 未配置密钥 / 任何异常都返回结构化失败，绝不抛出让服务崩溃（参照 fundgz.py）。

环境变量（全部可选，未配则功能降级为手动录入）：
  FUNDSIGHT_VISION_API_KEY   识别服务密钥（未设时回退读 ANTHROPIC_API_KEY）
  FUNDSIGHT_VISION_PROVIDER  anthropic（默认）| openai | local
  FUNDSIGHT_VISION_ENDPOINT  接口地址（不设时按 provider 取默认；local 不用）
  FUNDSIGHT_VISION_MODEL     模型名（不设时按 provider 取默认；local 不用）

provider=local：不调云端大模型，改用本地 OCR 引擎（RapidOCR，纯 pip 可选依赖）。
截图不出本机，合规更稳；代价是「版面解析」靠启发式（名字↔金额/收益的关联），
比大模型直接吐 JSON 脆，故识别结果一律经确认页由用户核对/改正后才入库。

合规提醒：anthropic/openai 通道会把截图（含金额/收益等财务数据）发送到已配置的
外部服务，仅自用私享场景由用户自行知情授权；local 通道全程本地处理不出网。
截图仅在内存处理，不落库、不留存。
"""
import base64
import importlib
import importlib.util
import json
import os
import re
import ssl
import urllib.request

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_DEFAULTS = {
    "anthropic": {
        "endpoint": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-5",
    },
    "openai": {
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
    },
}

# 提示词：要求模型只吐严格 JSON 数组，字段固定，便于机器解析。
_PROMPT = (
    "你是基金持仓截图识别助手。这是一张理财 App（如支付宝/天天基金/微信理财通）"
    "的持仓截图。请识别其中每一只基金/理财产品，输出一个严格的 JSON 数组，"
    "不要输出任何解释或 Markdown 代码围栏。每个元素的字段：\n"
    '  name        基金/产品名称（字符串，按截图原文）\n'
    '  code         6 位基金代码（字符串，截图没有则设为 null）\n'
    '  hold_amount 当前持仓金额/市值（数字，人民币元，无则 null）\n'
    '  profit       持有收益/盈亏（数字，亏损为负，无则 null）\n'
    '  profit_rate 持有收益率百分比数字，如 12.3 表示 12.3%（无则 null）\n'
    "金额去掉千分位逗号和「元」字，只保留数字。若截图中没有任何基金，返回 []。"
)

_ALLOWED_KEYS = ("name", "code", "hold_amount", "profit", "profit_rate", "cost")


def _api_key():
    return os.environ.get("FUNDSIGHT_VISION_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""


def _provider():
    p = (os.environ.get("FUNDSIGHT_VISION_PROVIDER") or "anthropic").strip().lower()
    return p if p in _DEFAULTS else "anthropic"


def _provider():
    p = (os.environ.get("FUNDSIGHT_VISION_PROVIDER") or "anthropic").strip().lower()
    if p == "local":
        return "local"
    return p if p in _DEFAULTS else "anthropic"


# 本地 OCR 引擎候选包名（新版重命名为 rapidocr，旧版为 rapidocr_onnxruntime）。
_RAPIDOCR_PKGS = ("rapidocr_onnxruntime", "rapidocr")


def _rapidocr_available():
    """RapidOCR 是否可导入（不实际构造引擎，避免加载模型的开销）。"""
    return any(importlib.util.find_spec(p) is not None for p in _RAPIDOCR_PKGS)


def is_configured():
    """当前 provider 是否就绪：local 看 RapidOCR 是否装好，云端看是否有密钥。"""
    if _provider() == "local":
        return _rapidocr_available()
    return bool(_api_key())


def provider_name():
    """当前识别通道名（anthropic | openai | local），供前端区分本地/云端提示。"""
    return _provider()


def _endpoint(provider):
    return os.environ.get("FUNDSIGHT_VISION_ENDPOINT") or _DEFAULTS[provider]["endpoint"]


def _model(provider):
    return os.environ.get("FUNDSIGHT_VISION_MODEL") or _DEFAULTS[provider]["model"]


def _build_request(provider, key, model, b64, mime):
    """按 provider 拼请求（url, headers, body-bytes）。"""
    if provider == "openai":
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {"type": "image_url",
                         "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
            "max_tokens": 2048,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }
    else:  # anthropic
        payload = {
            "model": model,
            "max_tokens": 2048,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {"type": "image",
                         "source": {"type": "base64", "media_type": mime, "data": b64}},
                    ],
                }
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
    body = json.dumps(payload).encode("utf-8")
    return _endpoint(provider), headers, body


def _extract_text(provider, resp):
    """从模型响应里取出文本内容（两种 provider 响应体形不同）。"""
    if provider == "openai":
        return resp["choices"][0]["message"]["content"]
    # anthropic: content 是块数组，取首个 text 块
    for block in resp.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    return ""


def _parse_model_json(text):
    """模型文本 → 持仓行列表。剥离代码围栏、截取首个 JSON 数组，容错解析。

    返回规整后的 rows（只保留白名单字段，数字字段尽量转 float）；无法解析返回 []。
    """
    if not text:
        return []
    s = text.strip()
    # 去掉 ```json ... ``` 或 ``` ... ``` 代码围栏
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    # 截取首个 [ ... ] 数组片段，容忍前后多余文字
    if not s.startswith("["):
        m = re.search(r"\[.*\]", s, re.DOTALL)
        if m:
            s = m.group(0)
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        row = {}
        for k in _ALLOWED_KEYS:
            if k not in item:
                continue
            v = item[k]
            if k in ("name", "code"):
                row[k] = str(v).strip() if v not in (None, "") else None
            else:
                row[k] = _num(v)
        if row.get("name") or row.get("code"):
            rows.append(row)
    return rows


def _num(v):
    """宽松数字转换：去千分位逗号 / 元 / % 等噪声。失败返回 None。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        cleaned = re.sub(r"[,，元%\s¥￥]", "", str(v))
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def recognize_holdings(image_bytes, mime="image/png"):
    """截图字节 → {"ok": True, "rows": [...]} 或 {"ok": False, "error": "..."}。

    唯一发起外部网络请求的函数（local 通道不出网）。任何失败都被 try/except
    兜底为结构化错误，绝不让识别失败拖垮服务。
    """
    if not image_bytes:
        return {"ok": False, "error": "空图片"}
    provider = _provider()
    if provider == "local":
        return _recognize_local(image_bytes)
    key = _api_key()
    if not key:
        return {"ok": False, "error": "未配置识别服务（缺少 API key）"}
    model = _model(provider)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    try:
        url, headers, body = _build_request(provider, key, model, b64, mime)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        raw = urllib.request.urlopen(req, timeout=60, context=_CTX).read().decode("utf-8")
        resp = json.loads(raw)
        text = _extract_text(provider, resp)
        rows = _parse_model_json(text)
        return {"ok": True, "rows": rows}
    except Exception as e:  # noqa: BLE001 —— 优雅降级，绝不让识别失败拖垮服务
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ==================== 本地 OCR 通道（provider=local） ====================

_local_engine = None  # RapidOCR 单例：构造会加载 ONNX 模型（~秒级），懒加载并复用。


def _get_local_engine():
    global _local_engine
    if _local_engine is not None:
        return _local_engine
    last_err = None
    for pkg in _RAPIDOCR_PKGS:
        try:
            mod = importlib.import_module(pkg)
            _local_engine = mod.RapidOCR()
            return _local_engine
        except Exception as e:  # noqa: BLE001 —— 换下一个候选包
            last_err = e
    raise RuntimeError(f"RapidOCR 不可用: {last_err}")


def _recognize_local(image_bytes):
    """本地 OCR：RapidOCR 抽字 → 版面启发式解析 → 持仓行。全程不出网。"""
    if not _rapidocr_available():
        return {"ok": False, "error": "未安装本地 OCR（pip install rapidocr_onnxruntime）"}
    try:
        engine = _get_local_engine()
        result, _elapse = engine(image_bytes)  # result: [[box, text, score], ...] 或 None
        rows = _parse_ocr_result(result or [])
        return {"ok": True, "rows": rows}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# 版面解析用的标签停用词：这些是 App 界面固定字样，不是基金名。
_LABEL_WORDS = (
    "持仓", "金额", "持有", "收益", "收益率", "市值", "昨日", "日涨幅", "涨幅",
    "累计", "成本", "份额", "净值", "总资产", "可用", "冻结", "参考", "总收益",
    "持有收益", "持有金额", "今日", "预估", "基金", "理财", "详情", "查看",
)
_CJK = r"一-鿿"
_RE_PERCENT = re.compile(r"[-+]?\d+(?:\.\d+)?\s*%")
_RE_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")
# 金额：可带 ¥/￥ 前缀、千分位逗号、正负号、小数；至少一位数字。
_RE_AMOUNT = re.compile(r"[-+]?\s*[¥￥]?\s*\d[\d,]*(?:\.\d+)?")


def _tok_geom(box):
    """OCR box（4 点）→ (cx, cy, top, bottom, height)。"""
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    top, bottom = min(ys), max(ys)
    return (sum(xs) / 4.0, (top + bottom) / 2.0, top, bottom, max(bottom - top, 1.0))


def _amount_value(raw):
    """金额文本 → (value, signed)。signed=True 表示原文带显式正负号（多为收益）。"""
    s = raw.strip()
    signed = s.startswith("+") or s.startswith("-") or s.startswith("＋") or s.startswith("－")
    cleaned = re.sub(r"[,，¥￥＋－\s]", "", s).replace("+", "").replace("-", "")
    try:
        val = float(cleaned)
    except ValueError:
        return None, False
    if s.startswith("-") or s.startswith("－"):
        val = -val
    return val, signed


def _looks_like_name(text):
    """是否像基金名：含 ≥2 个中文字、去掉标签词后仍有实体、不是纯标签。"""
    cjk = re.findall(f"[{_CJK}]", text)
    if len(cjk) < 2:
        return False
    stripped = text
    for w in _LABEL_WORDS:
        stripped = stripped.replace(w, "")
    return len(re.findall(f"[{_CJK}]", stripped)) >= 2


def _parse_ocr_result(items):
    """RapidOCR 结果 → 持仓行列表（版面启发式，纯函数便于单测）。

    items: [[box, text, score], ...]，box 为 4 个 [x, y] 点。
    策略：按 y 聚类成行 → 逐行自上而下扫描；遇到「像基金名」的行开一条新记录，
    其后的数字行归入当前记录，直到下一条基金名行。行内数字分类：
      - 百分比 → profit_rate（取首个）
      - 带显式正负号的金额 → profit（收益多带 +/−）
      - 其余金额取最大者 → hold_amount（持仓金额/市值通常最大）
      - 6 位纯数字 → code
    识别不可能全对，最终由确认页人工校正（延续「识别必经确认」约定）。
    """
    toks = []
    for it in items:
        try:
            box, text = it[0], it[1]
        except (IndexError, TypeError):
            continue
        text = (text or "").strip()
        if not text:
            continue
        cx, cy, top, bottom, h = _tok_geom(box)
        toks.append({"text": text, "cx": cx, "cy": cy, "h": h})
    if not toks:
        return []

    # 按 y 聚类成行：阈值取中位字高的 0.6 倍。
    heights = sorted(t["h"] for t in toks)
    med_h = heights[len(heights) // 2]
    y_tol = max(med_h * 0.6, 6.0)
    toks.sort(key=lambda t: (t["cy"], t["cx"]))
    lines = []
    for t in toks:
        if lines and abs(t["cy"] - lines[-1]["cy"]) <= y_tol:
            lines[-1]["toks"].append(t)
            n = len(lines[-1]["toks"])
            lines[-1]["cy"] = (lines[-1]["cy"] * (n - 1) + t["cy"]) / n
        else:
            lines.append({"cy": t["cy"], "toks": [t]})

    entries = []
    cur = None
    for ln in lines:
        ln["toks"].sort(key=lambda t: t["cx"])
        line_text = " ".join(t["text"] for t in ln["toks"])
        # 该行的名字候选：取最像基金名的 token（最长中文串）。
        name_tok = None
        for t in ln["toks"]:
            if _looks_like_name(t["text"]):
                if name_tok is None or len(t["text"]) > len(name_tok):
                    name_tok = t["text"]
        if name_tok:
            cur = {"name": name_tok, "code": None, "percents": [], "amounts": []}
            entries.append(cur)
        if cur is None:
            continue
        # 收集本行的 code / 百分比 / 金额（先扣掉百分比再找金额，避免把 2.9% 里的 2.9 当金额）。
        mcode = _RE_CODE.search(line_text)
        if mcode and cur["code"] is None:
            cur["code"] = mcode.group(0)
        no_pct = line_text
        for mp in _RE_PERCENT.finditer(line_text):
            cur["percents"].append(mp.group(0))
            no_pct = no_pct.replace(mp.group(0), " ")
        for ma in _RE_AMOUNT.finditer(no_pct):
            val, signed = _amount_value(ma.group(0))
            if val is None:
                continue
            # 跳过 6 位纯整数（基金代码，不是金额）。
            if re.fullmatch(r"\d{6}", re.sub(r"[,，¥￥\s]", "", ma.group(0))):
                continue
            cur["amounts"].append((val, signed))

    rows = []
    for e in entries:
        profit_rate = None
        if e["percents"]:
            v, _ = _amount_value(e["percents"][0].replace("%", ""))
            profit_rate = v
        hold_amount = profit = None
        signed_amts = [v for v, s in e["amounts"] if s]
        unsigned_amts = [v for v, s in e["amounts"] if not s]
        if signed_amts:
            profit = signed_amts[0]
        if unsigned_amts:
            hold_amount = max(unsigned_amts, key=abs)
        elif e["amounts"] and profit is None:
            # 没有无符号金额可选，退而取最大的一笔当持仓金额。
            hold_amount = max((v for v, _ in e["amounts"]), key=abs)
        rows.append({
            "name": e["name"],
            "code": e["code"],
            "hold_amount": hold_amount,
            "profit": profit,
            "profit_rate": profit_rate,
        })
    return rows
