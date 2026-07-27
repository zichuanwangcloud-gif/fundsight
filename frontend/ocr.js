// 截图识别持仓 —— 上传理财 App 截图 → 识别 + 匹配 → 确认页核对 → 批量导入。
// 入口按钮在「我的持仓」页(portfolio.js)调用 openOcrImport()。
// 复用 app.js 的 $ / getJSON / showAuth；导入成功后调 portfolio.js 的 load() 刷新。

let _ocrFileInput = null;
let _ocrProvider = null;   // 当前识别通道：local=本机识别，其余=云端服务

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
    const s = await r.json();
    configured = s.configured;
    _ocrProvider = s.provider;
  } catch (e) { /* 网络异常按未配置处理 */ }

  if (!configured) {
    _ocrOverlay(`
      <h3>截图识别未启用</h3>
      <p class="ocr-note">该功能需一个识别引擎（自用私享，密钥/截图不入库）。二选一，配好后重启服务：</p>
      <p class="ocr-note"><b>方案 A · 本地 OCR（截图不出本机，合规更稳）</b></p>
      <pre class="ocr-env">pip install rapidocr_onnxruntime
export FUNDSIGHT_VISION_PROVIDER=local</pre>
      <p class="ocr-note"><b>方案 B · 云端视觉大模型（识别更准，截图会发往所配服务）</b></p>
      <pre class="ocr-env">export ANTHROPIC_API_KEY=你的密钥
# 或指向 OpenAI 兼容/自建服务：
export FUNDSIGHT_VISION_PROVIDER=openai
export FUNDSIGHT_VISION_API_KEY=...
export FUNDSIGHT_VISION_ENDPOINT=https://your-host/v1/chat/completions
export FUNDSIGHT_VISION_MODEL=your-vision-model</pre>
      <p class="ocr-note"><b>方案 C · 固定模板纯代码（零依赖、零成本，仅限固定版式 PNG）</b></p>
      <pre class="ocr-env"># 先用样图校准一次，生成 data/ocr_template.json：
python3 scripts/ocr_calibrate.py 样图.png calib_input.json
export FUNDSIGHT_VISION_PROVIDER=template</pre>
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

  const notice = (_ocrProvider === "local" || _ocrProvider === "template")
    ? "🖥️ 正在本机识别（截图不出本机、不留存），首次加载稍慢，请稍候。"
    : "📷 截图正发送至已配置的识别服务，仅内存处理、不留存，请稍候。";
  _ocrOverlay(`<h3>识别中…</h3>
    <p class="ocr-note">${notice}</p>
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

// 确认页某行按名称/代码搜索基金，点选后写入该行的代码框(模板通道无候选时用)。
let _ocrSearchTimer = null;
function _ocrRowSearch(inp) {
  const sug = inp_sibling(inp);
  clearTimeout(_ocrSearchTimer);
  const v = inp.value.trim();
  if (!v) { sug.style.display = "none"; return; }
  _ocrSearchTimer = setTimeout(async () => {
    try {
      const r = await fetch("/api/search?q=" + encodeURIComponent(v), { credentials: "same-origin" });
      if (r.status === 401) return showAuth();
      const funds = await r.json();
      sug.innerHTML = funds.map(f =>
        `<div onclick="_ocrPickCode(this,'${f.fund_code}')">${_esc(f.name)}<span>${f.fund_code}</span></div>`).join("")
        || `<div class="ocr-none">无匹配</div>`;
      sug.style.display = "block";
    } catch (e) { /* 静默 */ }
  }, 200);
}
function inp_sibling(inp) { return inp.parentNode.querySelector(".ocr-sug"); }
function _ocrPickCode(el, code) {
  const cell = el.closest(".ocr-cell");
  cell.querySelector(".ocr-code").value = code;
  cell.querySelector(".ocr-sug").style.display = "none";
  cell.querySelector(".ocr-search").value = code;
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
    // 固定模板通道读不出基金名，改显示名称裁图供用户核对后搜索选码。
    const nameCell = r.name_image
      ? `<img class="ocr-nameimg" src="${r.name_image}" alt="基金名截图">
         <span class="ocr-rate">按截图搜索选码 →</span>`
      : `${_esc(r.name || "(未识别名称)")}
         ${r.profit_rate != null ? `<span class="ocr-rate">识别收益率 ${r.profit_rate}%</span>` : ""}`;
    return `<div class="ocr-row ${matchCls}" data-i="${i}">
      <div class="ocr-cell ocr-name" title="${_esc(r.name || "")}">
        <label><input type="checkbox" class="ocr-inc" ${codeVal ? "checked" : ""}> ${nameCell}</label>
      </div>
      <div class="ocr-cell">
        <input class="ocr-code" placeholder="基金代码" value="${codeVal}">
        ${cand.length ? `<select class="ocr-cand" onchange="this.closest('.ocr-row').querySelector('.ocr-code').value=this.value">
          <option value="">选候选…</option>${opts}</select>` : ""}
        ${!cand.length ? `<input class="ocr-search" placeholder="搜名称/代码选码" oninput="_ocrRowSearch(this)" autocomplete="off">
          <div class="ocr-sug"></div>` : ""}
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
    toast(`已导入 ${data.imported || 0} 只基金到持仓`);
  } catch (err) {
    _ocrError("导入失败，请重试");
  }
}

// ---- 极简 overlay(零依赖):支持 Esc + 遮罩点击关闭,与原生 <dialog> 行为对齐 ----
function _ocrOverlay(html) {
  let ov = document.getElementById("ocr-overlay");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "ocr-overlay";
    ov.addEventListener("click", e => { if (e.target === ov) _ocrClose(); });
    document.body.appendChild(ov);
  }
  ov.innerHTML = `<div class="ocr-modal">${html}</div>`;
  ov.style.display = "flex";
  document.addEventListener("keydown", _ocrKeydown);
}
function _ocrClose() {
  const ov = document.getElementById("ocr-overlay");
  if (ov) ov.style.display = "none";
  document.removeEventListener("keydown", _ocrKeydown);
}
function _ocrKeydown(e) { if (e.key === "Escape") _ocrClose(); }
function _ocrError(msg) {
  _ocrOverlay(`<h3>识别未成功</h3>
    <p class="ocr-note">${_esc(msg)}</p>
    <div class="ocr-btns"><button class="ghost" onclick="_ocrClose()">关闭</button></div>`);
}
function _esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
