# -*- coding: utf-8 -*-
"""AI 分析引擎 —— provider 抽象 + Messages API tool-calling 循环（纯标准库）。

延续本项目「零第三方依赖 / 抓取层是唯一对外接口 / 优雅降级」三条约束：
- 只用标准库（urllib / json / ssl），不引入 anthropic / openai SDK。
- 手搓 tool-loop：模型请求调工具 → 进程内 backend.mcp.tools.call_tool 执行 → 回填 →
  继续，直到模型出最终答复或达轮数上限（MAX_TOOL_ROUNDS，钉死外部请求与成本）。
- provider 可切换、默认不定死：沿用 vision_ocr 的 key/provider 环境变量思路。
- 无 API key 时 is_configured()=False，上层据此优雅降级（按钮置灰），主功能不受影响。

环境变量（全部可选）：
  FUNDSIGHT_AI_API_KEY   密钥（未设时回退读 ANTHROPIC_API_KEY，与截图识别共用）
  FUNDSIGHT_AI_PROVIDER  anthropic（默认）| openai（含 OpenAI 兼容端点，如国产模型）
  FUNDSIGHT_AI_ENDPOINT  接口地址（不设按 provider 取默认）
  FUNDSIGHT_AI_MODEL     模型名（不设按 provider 取默认）

合规：分析仅基于本地已缓存的公开数据；结尾强制免责，「预期未来」只作情景/风险提示，
不给确定性买卖点或价格目标。
"""
import json
import os
import ssl
import urllib.request

from backend.mcp import tools

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

MAX_TOOL_ROUNDS = 6      # tool-loop 硬上限：钉死外部请求次数与 token 成本
MAX_TOKENS = 1500
TIMEOUT = 60

DISCLAIMER = (
    "本分析由 AI 基于公开数据自动生成，仅供参考，不构成任何投资建议；"
    "市场有风险，投资需谨慎。"
)

SYSTEM_PROMPT = (
    "你是「盈见 FundSight」的基金分析助手，服务于自用私享的基金看板。\n"
    "工作方式：\n"
    "1. 当用户询问某只基金时，主动调用工具获取数据——先 get_fund_overview 看概况，"
    "再按需 get_fund_holdings 看重仓股、get_fund_nav_trend 看走势、"
    "get_fund_peer_compare 看同类对比。基金以 6 位代码标识。\n"
    "2. 汇总时覆盖：近期涨幅表现、重仓股与赛道暴露、相对同类/大盘强弱、波动与回撤，"
    "最后给出「近况小结」和「未来展望」。\n"
    "3. 「未来展望」只能是情景分析与风险提示，严禁给出确定性的买/卖建议、"
    "点位预测或收益承诺。\n"
    "4. 每次给出基金分析结论时，务必在结尾附上一行免责声明："
    + DISCLAIMER + "\n"
    "5. 数据来自本地缓存的公开信息，可能非最新；个股/赛道的宏观判断若来自你的知识，"
    "需说明「非实时、仅供参考」。用简体中文、条理清晰地回答。"
)


def _api_key():
    return os.environ.get("FUNDSIGHT_AI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""


def _provider():
    p = (os.environ.get("FUNDSIGHT_AI_PROVIDER") or "anthropic").strip().lower()
    return p if p in _DEFAULTS else "anthropic"


def _endpoint(provider):
    return os.environ.get("FUNDSIGHT_AI_ENDPOINT") or _DEFAULTS[provider]["endpoint"]


def _model(provider):
    return os.environ.get("FUNDSIGHT_AI_MODEL") or _DEFAULTS[provider]["model"]


def is_configured():
    return bool(_api_key())


def status():
    return {"configured": is_configured(), "provider": _provider()}


# --------------------------------------------------------------------------- #
# provider 适配：请求体构建 / 响应归一化 / 工具结果回填
# 归一化响应形态：{"text": str, "tool_calls": [{"id","name","input"}], "done": bool}
# --------------------------------------------------------------------------- #
def _anthropic_tools():
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in tools.TOOLS
    ]


def _openai_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools.TOOLS
    ]


def _post(url, headers, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    raw = urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX).read().decode("utf-8")
    return json.loads(raw)


def _call_anthropic(key, model, messages):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "tools": _anthropic_tools(),
        "messages": messages,
    }
    resp = _post(_endpoint("anthropic"), headers, payload)
    text_parts, tool_calls = [], []
    for block in resp.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {"id": block.get("id"), "name": block.get("name"), "input": block.get("input") or {}})
    done = resp.get("stop_reason") != "tool_use"
    return {
        "raw_assistant": {"role": "assistant", "content": resp.get("content", [])},
        "text": "".join(text_parts),
        "tool_calls": tool_calls,
        "done": done,
    }


def _append_anthropic_results(messages, norm, results):
    messages.append(norm["raw_assistant"])
    messages.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tc["id"], "content": text}
            for tc, text in results
        ],
    })


def _call_openai(key, model, messages):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "tools": _openai_tools(),
        "messages": messages,
    }
    resp = _post(_endpoint("openai"), headers, payload)
    choice = (resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        tool_calls.append({"id": tc.get("id"), "name": fn.get("name"), "input": args})
    done = choice.get("finish_reason") != "tool_calls"
    return {
        "raw_assistant": message,
        "text": message.get("content") or "",
        "tool_calls": tool_calls,
        "done": done,
    }


def _append_openai_results(messages, norm, results):
    messages.append(norm["raw_assistant"])
    for tc, text in results:
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": text})


_ADAPTERS = {
    "anthropic": (_call_anthropic, _append_anthropic_results),
    "openai": (_call_openai, _append_openai_results),
}


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #
def run_chat(messages):
    """跑一轮多轮对话 + tool-loop。

    messages: [{"role": "user"|"assistant", "content": "..."}]（provider 无关的纯文本历史）。
    返回：{"ok": True, "reply": str, "tool_calls": [name...], "disclaimer": DISCLAIMER}
         或 {"ok": False, "error": str}。
    """
    if not is_configured():
        return {"ok": False, "error": "未配置 AI 服务（缺少 API key）"}
    provider = _provider()
    call, append_results = _ADAPTERS[provider]
    key, model = _api_key(), _model(provider)
    convo = [{"role": m.get("role", "user"), "content": m.get("content", "")}
             for m in messages if m.get("content")]
    if not convo:
        return {"ok": False, "error": "空对话"}

    used_tools = []
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            norm = call(key, model, convo)
            if norm["done"] or not norm["tool_calls"]:
                return {
                    "ok": True,
                    "reply": norm["text"].strip(),
                    "tool_calls": used_tools,
                    "disclaimer": DISCLAIMER,
                }
            results = []
            for tc in norm["tool_calls"]:
                used_tools.append(tc["name"])
                results.append((tc, tools.call_tool_text(tc["name"], tc["input"])))
            append_results(convo, norm, results)
        # 达轮数上限仍在调工具：再要一次纯文本收尾
        return {
            "ok": True,
            "reply": "（分析步骤较多已达上限，基于已获取的数据给出小结）\n"
                     "请就已知数据总结，或缩小问题范围后再问。",
            "tool_calls": used_tools,
            "disclaimer": DISCLAIMER,
        }
    except Exception as e:  # noqa: BLE001 —— 优雅降级，绝不让分析失败拖垮服务
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def analyze_fund(code, fund_name=None):
    """详情页「一键分析」：预置一条强提示，模型据 code 自动跑工具链出分析卡。"""
    label = f"{fund_name}（{code}）" if fund_name else code
    prompt = (
        f"请分析基金 {label} 的近况与未来展望。"
        "调用工具获取它的概况、近期涨幅走势、前十大重仓股、同类与沪深300 对比，"
        "然后汇总成：① 近况小结（近期表现/重仓股与赛道/相对强弱/波动回撤）"
        "② 未来展望（情景与风险提示，不给确定性买卖建议）。"
    )
    return run_chat([{"role": "user", "content": prompt}])
