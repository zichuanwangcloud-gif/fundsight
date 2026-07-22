// 截图识别持仓 —— 上传理财 App 截图 → 识别 + 匹配 → 确认页核对 → 批量导入。
// 入口按钮在「我的持仓」页(portfolio.js)调用 openOcrImport()。
// 复用 app.js 的 $ / getJSON / showAuth；导入成功后调 portfolio.js 的 load() 刷新。

let _ocrFileInput = null;

function _ensureOcrFileInput() {
  if (_ocrFileInput) return _ocrFileInput;
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "image/*";
  inp.style.display = "none";
  inp.addEventListener("change", _onOcrFileChosen);
  document.body.appendChild(inp);
  _ocrFileInput = inp;
  return inp;
}

async function openOcrImport() {
  // 先探测识别服务是否配置：未配置直接给出可操作的提示，不打开文件选择。
  let configured = false;
  try {
    const r = await fetch("/api/ocr/status", { credentials: "same-origin" });
    if (r.status === 401) return showAuth();
    configured = (await r.json()).configured;
  } catch (e) { /* 网络异常按未配置处理 */ }

  if (!configured) {
    _ocrOverlay(`
      <h3>截图识别未启用</h3>
      <p class="ocr-note">该功能需配置一个视觉大模型识别服务（自用私享，密钥不入库）。
      在服务端设置环境变量后重启即可启用：</p>
      <pre class="ocr-env">export ANTHROPIC_API_KEY=你的密钥
# 或指向 OpenAI 兼容/自建服务：
export FUNDSIGHT_VISION_PROVIDER=openai
export FUNDSIGHT_VISION_API_KEY=...
export FUNDSIGHT_VISION_ENDPOINT=https://your-host/v1/chat/completions
export FUNDSIGHT_VISION_MODEL=your-vision-model</pre>
      <p class="ocr-note">未配置时可继续用上方搜索手动录入持仓，不受影响。</p>
      <div class="ocr-btns"><button class="ghost" onclick="_ocrClose()">知道了</button></div>`);
    return;
  }
  _ensureOcrFileInput().click();
}

async function _onOcrFileChosen(e) {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";  // 允许连续选同一文件
  if (!file) return;

  _ocrOverlay(`<h3>识别中…</h3>
    <p class="ocr-note">📷 截图正发送至已配置的识别服务，仅内存处理、不留存，请稍候。</p>
    <div class="ocr-spin">解析持仓中…</div>`);

  let dataUrl;
  try {
    dataUrl = await _readAsDataURL(file);
  } catch (err) {
    return _ocrError("图片读取失败");
  }

  try {
    const r = await fetch("/api/ocr/recognize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ image: dataUrl }),
    });
    if (r.status === 401) { _ocrClose(); return showAuth(); }
    const data = await r.json();
    if (data.configured === false) return _ocrError("识别服务未配置");
    if (data.error) return _ocrError(data.error);
    const rows = data.rows || [];
    if (!rows.length) return _ocrError("未从截图中识别到基金，请换一张更清晰的持仓截图");
    _renderConfirm(rows);
  } catch (err) {
    _ocrError("识别请求失败，请重试");
  }
}

function _readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(fr.result);
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(file);
  });
}

// 确认页：每行可核对/改正 基金代码(带识别候选下拉) + 持仓金额 + 成本 + 是否导入。
function _renderConfirm(rows) {
  const body = rows.map((r, i) => {
    const cand = (r.candidates || []);
    const opts = cand.map(c =>
      `<option value="${c.fund_code}" ${c.fund_code === r.matched_code ? "selected" : ""}>${c.name}（${c.fund_code}）</option>`
    ).join("");
    const codeVal = r.matched_code || r.code || "";
    const matchCls = r.matched_code ? "" : "ocr-unmatched";
    return `<div class="ocr-row ${matchCls}" data-i="${i}">
      <div class="ocr-cell ocr-name" title="${_esc(r.name || "")}">
        <label><input type="checkbox" class="ocr-inc" ${codeVal ? "checked" : ""}> ${_esc(r.name || "(未识别名称)")}</label>
        ${r.profit_rate != null ? `<span class="ocr-rate">识别收益率 ${r.profit_rate}%</span>` : ""}
      </div>
      <div class="ocr-cell">
        <input class="ocr-code" placeholder="基金代码" value="${codeVal}">
        ${cand.length ? `<select class="ocr-cand" onchange="this.closest('.ocr-row').querySelector('.ocr-code').value=this.value">
          <option value="">选候选…</option>${opts}</select>` : ""}
        ${r.matched_code ? "" : `<span class="ocr-warn">未匹配到，请核对代码</span>`}
      </div>
      <div class="ocr-cell"><input class="ocr-hold" type="number" step="0.01" placeholder="持仓金额" value="${r.hold_amount ?? ""}"></div>
      <div class="ocr-cell"><input class="ocr-cost" type="number" step="0.01" placeholder="成本" value="${r.cost_amount ?? ""}"></div>
    </div>`;
  }).join("");

  _ocrOverlay(`
    <h3>核对识别结果 · ${rows.length} 只</h3>
    <p class="ocr-note">识别可能有误，请核对基金代码与金额后再导入。未匹配的行请手动补代码。</p>
    <div class="ocr-table">
      <div class="ocr-row ocr-head">
        <div class="ocr-cell">基金 / 导入</div><div class="ocr-cell">代码</div>
        <div class="ocr-cell">持仓金额</div><div class="ocr-cell">成本</div>
      </div>
      ${body}
    </div>
    <div class="ocr-btns">
      <button class="ghost" onclick="_ocrClose()">取消</button>
      <button class="primary" onclick="_ocrDoImport()">导入勾选项</button>
    </div>`);
}

async function _ocrDoImport() {
  const rows = [];
  document.querySelectorAll(".ocr-row[data-i]").forEach(el => {
    if (!el.querySelector(".ocr-inc").checked) return;
    const code = el.querySelector(".ocr-code").value.trim();
    if (!code) return;
    rows.push({
      fund_code: code,
      hold_amount: el.querySelector(".ocr-hold").value,
      cost_amount: el.querySelector(".ocr-cost").value,
    });
  });
  if (!rows.length) return _ocrError("请至少勾选一行、并确保有基金代码");

  try {
    const r = await fetch("/api/ocr/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ rows }),
    });
    if (r.status === 401) { _ocrClose(); return showAuth(); }
    const data = await r.json();
    _ocrClose();
    if (typeof load === "function") load();  // 刷新持仓列表
    alert(`已导入 ${data.imported || 0} 只基金到持仓`);
  } catch (err) {
    _ocrError("导入失败，请重试");
  }
}

// ---- 极简 overlay(零依赖) ----
function _ocrOverlay(html) {
  let ov = document.getElementById("ocr-overlay");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "ocr-overlay";
    document.body.appendChild(ov);
  }
  ov.innerHTML = `<div class="ocr-modal">${html}</div>`;
  ov.style.display = "flex";
}
function _ocrClose() {
  const ov = document.getElementById("ocr-overlay");
  if (ov) ov.style.display = "none";
}
function _ocrError(msg) {
  _ocrOverlay(`<h3>识别未成功</h3>
    <p class="ocr-note">${_esc(msg)}</p>
    <div class="ocr-btns"><button class="ghost" onclick="_ocrClose()">关闭</button></div>`);
}
function _esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
