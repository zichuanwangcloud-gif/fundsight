# -*- coding: utf-8 -*-
"""截图识别持仓 —— 唯一对外接口：调多模态视觉大模型抽取持仓结构化数据。

延续本项目「抓取层是唯一对外接口」+「零第三方依赖」+「优雅降级」三条约束：
- 只用标准库（urllib / json / base64 / ssl），不引入任何 pip 依赖。
- 识别引擎可配置：默认走 Claude（anthropic），也可指向任意 OpenAI 兼容 / 自建视觉服务。
- 未配置密钥 / 任何异常都返回结构化失败，绝不抛出让服务崩溃（参照 fundgz.py）。

环境变量（全部可选，未配则功能降级为手动录入）：
  FUNDSIGHT_VISION_API_KEY   识别服务密钥（未设时回退读 ANTHROPIC_API_KEY）
  FUNDSIGHT_VISION_PROVIDER  anthropic（默认）| openai
  FUNDSIGHT_VISION_ENDPOINT  接口地址（不设时按 provider 取默认）
  FUNDSIGHT_VISION_MODEL     模型名（不设时按 provider 取默认）

合规提醒：识别会把截图（含金额/收益等财务数据）发送到已配置的外部服务，
仅自用私享场景由用户自行知情授权；截图仅在内存处理，不落库、不留存。
"""
import base64
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


def is_configured():
    """是否已配好识别服务（有密钥即视为可用）。"""
    return bool(_api_key())


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

    唯一发起外部网络请求的函数。任何失败都被 try/except 兜底为结构化错误。
    """
    key = _api_key()
    if not key:
        return {"ok": False, "error": "未配置识别服务（缺少 API key）"}
    if not image_bytes:
        return {"ok": False, "error": "空图片"}
    provider = _provider()
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
