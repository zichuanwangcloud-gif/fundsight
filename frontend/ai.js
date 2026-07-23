// AI 分析助手 —— 全局悬浮聊天窗 + 详情页「一键分析」。零依赖，复用 app.js 的 $ / getJSON。
//
// - 悬浮球固定右下角，点开为对话面板；支持多轮对话与「@某基金」快捷提问。
// - 详情页「🤖 AI 分析」按钮调 /api/ai/analyze/{code}，把结果推入聊天窗展示。
// - 无条件免责页脚：不依赖模型自觉，前端始终渲染，双保险。
// - 未配置 AI（无 API key）时优雅降级：面板提示未开启，不报错。

const AI_DISCLAIMER =
  "本分析由 AI 基于公开数据自动生成，仅供参考，不构成投资建议；市场有风险，投资需谨慎。";

let _aiConfigured = null;   // null 未知 / true / false
let _aiHistory = [];        // [{role, content}] 供多轮上下文
let _aiBusy = false;

function _aiEl(id) { return document.getElementById(id); }

// ---- 挂载悬浮球 + 面板（幂等，只挂一次）----
function mountAiWidget() {
  if (_aiEl("ai-fab")) return;
  const wrap = document.createElement("div");
  wrap.innerHTML = `
    <button id="ai-fab" class="ai-fab" title="AI 分析助手" onclick="toggleAiPanel()">🤖</button>
    <div id="ai-panel" class="ai-panel" style="display:none">
      <div class="ai-head">
        <span class="ai-title">🤖 AI 分析助手</span>
        <button class="ai-close" onclick="toggleAiPanel()">×</button>
      </div>
      <div id="ai-msgs" class="ai-msgs"></div>
      <div class="ai-disclaimer">${AI_DISCLAIMER}</div>
      <div class="ai-input">
        <textarea id="ai-text" rows="1" placeholder="问我某只基金，如：分析 020608"
                  onkeydown="aiOnKey(event)"></textarea>
        <button id="ai-send" class="ai-send" onclick="aiSend()">发送</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  _aiCheckConfigured();
}

async function _aiCheckConfigured() {
  try {
    const s = await getJSON("/api/ai/status");
    _aiConfigured = !!s.configured;
  } catch (e) { _aiConfigured = false; }
  if (_aiConfigured === false && _aiEl("ai-msgs") && !_aiHistory.length) {
    _aiSysNote("AI 分析尚未开启（管理员未配置模型密钥）。基金看板其余功能不受影响。");
    const send = _aiEl("ai-send"); if (send) send.disabled = true;
    const txt = _aiEl("ai-text"); if (txt) txt.disabled = true;
  }
}

function toggleAiPanel() {
  const p = _aiEl("ai-panel");
  if (!p) return;
  const show = p.style.display === "none";
  p.style.display = show ? "" : "none";
  if (show && !_aiHistory.length && _aiConfigured !== false) {
    _aiSysNote("你好，我是基金分析助手。发给我基金代码（如 020608），"
      + "我会看它的近期涨幅、重仓股、同类对比后汇总分析。");
  }
}

// ---- 消息渲染 ----
function _aiPush(role, text) {
  const box = _aiEl("ai-msgs");
  if (!box) return;
  const div = document.createElement("div");
  div.className = "ai-msg ai-" + role;
  div.innerHTML = `<div class="ai-bubble"></div>`;
  div.querySelector(".ai-bubble").textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}
function _aiSysNote(text) {
  const box = _aiEl("ai-msgs");
  if (!box) return;
  const div = document.createElement("div");
  div.className = "ai-note";
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function aiOnKey(e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); aiSend(); }
}

// ---- 发送对话 ----
async function aiSend() {
  const txt = _aiEl("ai-text");
  if (!txt || _aiBusy) return;
  const content = txt.value.trim();
  if (!content) return;
  txt.value = "";
  _aiPush("user", content);
  _aiHistory.push({ role: "user", content });
  await _aiRequest("/api/ai/chat", { messages: _aiHistory });
}

// 详情页「一键分析」调用：打开面板并请求 analyze
async function aiAnalyzeFund(code, name) {
  if (!code) return;
  mountAiWidget();
  const p = _aiEl("ai-panel");
  if (p) p.style.display = "";
  if (_aiConfigured === false) { _aiSysNote("AI 分析尚未开启（未配置模型密钥）。"); return; }
  const label = name ? `${name}（${code}）` : code;
  _aiPush("user", `分析基金 ${label}`);
  _aiHistory.push({ role: "user", content: `分析基金 ${label}` });
  await _aiRequest("/api/ai/analyze/" + encodeURIComponent(code), {});
}

async function _aiRequest(url, body) {
  if (_aiBusy) return;
  _aiBusy = true;
  const send = _aiEl("ai-send"); if (send) send.disabled = true;
  const thinking = _aiPush("assistant", "分析中…（读取持仓/走势/同类对比）");
  try {
    const r = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (r.status === 401) {
      thinking.querySelector(".ai-bubble").textContent = "请先登录后再使用 AI 分析。";
      return;
    }
    const data = await r.json();
    if (data.ok) {
      const reply = data.reply || "（无输出）";
      thinking.querySelector(".ai-bubble").textContent = reply;
      _aiHistory.push({ role: "assistant", content: reply });
    } else {
      thinking.querySelector(".ai-bubble").textContent =
        "分析失败：" + (data.error || "未知错误");
    }
  } catch (e) {
    thinking.querySelector(".ai-bubble").textContent = "网络错误，请稍后重试。";
  } finally {
    _aiBusy = false;
    if (send) send.disabled = _aiConfigured === false;
  }
}
