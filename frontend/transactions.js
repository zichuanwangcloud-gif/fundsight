// 「交易记录」—— 持仓卡片内的入口 + 动作感知录入表单(加仓/减仓/转换) + 流水列表(弹层)。
// 不是独立整页，不覆盖 registerPage("portfolio")；通过 MutationObserver 监听
// portfolio.js 渲染出的 #list，在每张持仓卡片上叠加一个「交易记录」入口。
// $ / $$ / getJSON / showAuth 来自 app.js / auth.js（全局）。
//
// 加仓/减仓 会计上就是 buy/sell(金额优先录入，净值自动算份额)；转换走
// POST /api/transactions/convert 成对写入。流水标签(建仓/加仓/减仓/清仓/转出/转入)
// 由后端 label 字段直出。

let _txCode = null;
let _txAction = "buy";   // buy=加仓 / sell=减仓 / convert=转换
let _txConvTo = null;    // 转换目标基金 {fund_code, name}
let _txSearchTimer = null;

function _txEnsureDialog() {
  if ($("#tx-dlg")) return;
  const dlg = document.createElement("dialog");
  dlg.id = "tx-dlg";
  dlg.innerHTML = `
    <h3 id="tx-dlg-title">交易记录</h3>
    <div id="tx-position" class="tx-position"></div>
    <div id="tx-list" class="tx-list"></div>

    <div class="tx-actions">
      <button type="button" class="tx-act buy active" data-act="buy" onclick="setTxAction('buy')">加仓</button>
      <button type="button" class="tx-act sell" data-act="sell" onclick="setTxAction('sell')">减仓</button>
      <button type="button" class="tx-act conv" data-act="convert" onclick="setTxAction('convert')">转换</button>
    </div>

    <!-- 加仓/减仓：金额优先 -->
    <div id="tx-form-trade" class="tx-form">
      <label>金额（元）</label>
      <input id="tx-amount" type="number" step="0.01" placeholder="如 2000">
      <label>净值（元，用于折算份额）</label>
      <input id="tx-price" type="number" step="0.0001" placeholder="如 1.2345">
      <label>交易日期</label>
      <input id="tx-date" type="date">
      <div class="tx-hint" id="tx-shares-hint"></div>
      <div class="btns">
        <button class="ghost" onclick="document.getElementById('tx-dlg').close()">关闭</button>
        <button class="primary" id="tx-submit-trade" onclick="submitTransaction()">记一笔</button>
      </div>
    </div>

    <!-- 转换：转出本基金 A → 转入基金 B -->
    <div id="tx-form-convert" class="tx-form" hidden>
      <label>转入基金</label>
      <div class="tx-search">
        <input id="tx-conv-q" type="text" placeholder="搜索转入基金（名称/代码）" autocomplete="off">
        <div id="tx-conv-results" class="tx-conv-results"></div>
      </div>
      <div id="tx-conv-picked" class="tx-conv-picked" hidden></div>
      <label>转出份额</label>
      <input id="tx-conv-out-shares" type="number" step="0.01" placeholder="转出 A 的份额">
      <label>转出净值（A 当日净值）</label>
      <input id="tx-conv-out-nav" type="number" step="0.0001" placeholder="如 1.5000">
      <label>转入净值（B 当日净值）</label>
      <input id="tx-conv-in-nav" type="number" step="0.0001" placeholder="如 2.0000">
      <label>转换费（元，可选）</label>
      <input id="tx-conv-fee" type="number" step="0.01" placeholder="赎回费+申购补差，默认 0">
      <label>交易日期</label>
      <input id="tx-conv-date" type="date">
      <div class="tx-hint" id="tx-conv-hint"></div>
      <div class="btns">
        <button class="ghost" onclick="document.getElementById('tx-dlg').close()">关闭</button>
        <button class="primary" id="tx-submit-convert" onclick="submitConversion()">记一笔转换</button>
      </div>
    </div>`;
  document.body.appendChild(dlg);
  _txWireConvertSearch();
  _txWireHints();
}

async function openTransactions(code) {
  _txCode = code;
  _txConvTo = null;
  _txEnsureDialog();
  $("#tx-dlg-title").textContent = "交易记录 · " + code;
  $("#tx-amount").value = "";
  $("#tx-price").value = "";
  $("#tx-date").value = _todayLocal();
  $("#tx-conv-q").value = "";
  $("#tx-conv-out-shares").value = "";
  $("#tx-conv-out-nav").value = "";
  $("#tx-conv-in-nav").value = "";
  $("#tx-conv-fee").value = "";
  $("#tx-conv-date").value = _todayLocal();
  $("#tx-conv-results").innerHTML = "";
  $("#tx-conv-picked").hidden = true;
  $("#tx-shares-hint").textContent = "";
  $("#tx-conv-hint").textContent = "";
  setTxAction("buy");
  await _txPrefillNav();          // 净值预填(A 最新净值)
  await loadTransactions();
  $("#tx-dlg").showModal();
}

// 本地时区的今天 YYYY-MM-DD(toISOString 取的是 UTC,晚间会偏成昨天)。
function _todayLocal() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function _txNum(n) { return n == null ? "—" : Number(n).toLocaleString(); }

// 拉本基金最新净值,预填加仓/减仓与转出净值(便于金额→份额折算)。失败静默。
async function _txPrefillNav() {
  if (!_txCode) return;
  try {
    const r = await fetch("/api/fund/" + encodeURIComponent(_txCode), { credentials: "same-origin" });
    if (!r.ok) return;
    const d = await r.json();
    const series = d.series || [];
    const nav = series.length ? series[series.length - 1].nav : null;
    if (nav != null) {
      if (!$("#tx-price").value) $("#tx-price").value = nav;
      if (!$("#tx-conv-out-nav").value) $("#tx-conv-out-nav").value = nav;
    }
  } catch (_) { /* 预填失败不影响手动录入 */ }
}

function setTxAction(act) {
  _txAction = act;
  $$(".tx-act").forEach(b => b.classList.toggle("active", b.dataset.act === act));
  const isConvert = act === "convert";
  $("#tx-form-trade").hidden = isConvert;
  $("#tx-form-convert").hidden = !isConvert;
  if (!isConvert) _txUpdateSharesHint();
  else _txUpdateConvHint();
}

// 金额 ÷ 净值 = 份额,实时提示,让用户看清将记入的份额。
function _txWireHints() {
  ["#tx-amount", "#tx-price"].forEach(sel =>
    $(sel).addEventListener("input", _txUpdateSharesHint));
  ["#tx-conv-out-shares", "#tx-conv-out-nav", "#tx-conv-in-nav", "#tx-conv-fee"].forEach(sel =>
    $(sel).addEventListener("input", _txUpdateConvHint));
}

function _txUpdateSharesHint() {
  const amt = parseFloat($("#tx-amount").value);
  const price = parseFloat($("#tx-price").value);
  const verb = _txAction === "sell" ? "减仓" : "加仓";
  $("#tx-shares-hint").textContent = (amt > 0 && price > 0)
    ? `≈ ${verb} ${(amt / price).toLocaleString(undefined, { maximumFractionDigits: 2 })} 份`
    : "";
}

function _txUpdateConvHint() {
  const os = parseFloat($("#tx-conv-out-shares").value);
  const onav = parseFloat($("#tx-conv-out-nav").value);
  const inav = parseFloat($("#tx-conv-in-nav").value);
  const fee = parseFloat($("#tx-conv-fee").value) || 0;
  const box = $("#tx-conv-hint");
  if (os > 0 && onav > 0 && inav > 0) {
    const amountOut = os * onav;
    const amountIn = amountOut - fee;
    if (amountIn <= 0) { box.textContent = "转换费超过转出金额，无法转换"; return; }
    box.textContent = `转出市值 ${amountOut.toLocaleString(undefined, { maximumFractionDigits: 2 })} 元`
      + ` → 转入 ${(amountIn / inav).toLocaleString(undefined, { maximumFractionDigits: 2 })} 份`;
  } else {
    box.textContent = "";
  }
}

// 转换目标基金搜索(复用 /api/search)。
function _txWireConvertSearch() {
  const q = $("#tx-conv-q"), results = $("#tx-conv-results");
  q.addEventListener("input", () => {
    clearTimeout(_txSearchTimer);
    const v = q.value.trim();
    if (!v) { results.innerHTML = ""; return; }
    _txSearchTimer = setTimeout(async () => {
      const r = await fetch("/api/search?q=" + encodeURIComponent(v), { credentials: "same-origin" });
      if (r.status === 401) return showAuth();
      const funds = (await r.json()).filter(f => f.fund_code !== _txCode);  // 不能转到自己
      results.innerHTML = funds.map(f =>
        `<div role="button" tabindex="0"
              onclick='pickConvertTarget(${JSON.stringify(f).replace(/'/g, "&#39;")})'>
           ${f.name}<span class="code">${f.fund_code}</span></div>`).join("")
        || `<div class="tx-conv-none">无匹配结果</div>`;
    }, 200);
  });
}

function pickConvertTarget(f) {
  _txConvTo = f;
  $("#tx-conv-results").innerHTML = "";
  $("#tx-conv-q").value = "";
  const picked = $("#tx-conv-picked");
  picked.hidden = false;
  picked.innerHTML = `转入 → <b>${f.name}</b> <span class="code">${f.fund_code}</span>
    <span class="tx-conv-clear" role="button" onclick="clearConvertTarget()">✕</span>`;
}

function clearConvertTarget() {
  _txConvTo = null;
  $("#tx-conv-picked").hidden = true;
  $("#tx-conv-picked").innerHTML = "";
}

function _txLabelClass(label) {
  if (["建仓", "加仓", "转入"].includes(label)) return "buy";
  if (["减仓", "清仓", "转出", "卖出"].includes(label)) return "sell";
  return "";
}

async function loadTransactions() {
  if (!_txCode) return;
  const r = await fetch("/api/transactions?code=" + encodeURIComponent(_txCode), { credentials: "same-origin" });
  if (r.status === 401) return showAuth();
  const data = await r.json();
  const pos = data.position;
  $("#tx-position").innerHTML = (pos && pos.shares > 0)
    ? `持仓 <b>${_txNum(pos.shares)}</b> 份 · 成本 <b>${_txNum(pos.cost_amount)}</b> 元 · 均价 <b>${pos.avg_cost}</b>`
    : `暂无持仓（由流水推导）`;
  const items = data.items || [];
  $("#tx-list").innerHTML = items.length
    ? items.map(it => {
      const label = it.label || (it.action === "buy" ? "买入" : "卖出");
      return `
      <div class="tx-row">
        <span class="tx-tag ${_txLabelClass(label)}">${label}</span>
        <span>${it.trade_date || ""}</span>
        <span>${_txNum(it.shares)} 份 @ ${it.price ?? "—"}</span>
        <span>${_txNum(it.amount)} 元</span>
        <span class="tx-del" role="button" tabindex="0" aria-label="删除交易流水" onclick="deleteTransaction(${it.id})">删除</span>
      </div>`;
    }).join("")
    : `<div class="empty">还没有交易记录</div>`;
}

async function submitTransaction() {
  const amount = $("#tx-amount").value;
  const price = $("#tx-price").value;
  if (!(parseFloat(amount) > 0)) { toast("请输入金额"); return; }
  const body = JSON.stringify({
    fund_code: _txCode,
    action: _txAction === "sell" ? "sell" : "buy",
    amount, price,
    trade_date: $("#tx-date").value,
  });
  const r = await fetch("/api/transactions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body,
  });
  if (r.status === 401) return showAuth();
  if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.error || "提交失败"); return; }
  $("#tx-amount").value = "";
  $("#tx-shares-hint").textContent = "";
  loadTransactions();
}

async function submitConversion() {
  if (!_txConvTo) { toast("请选择转入基金"); return; }
  const outShares = $("#tx-conv-out-shares").value;
  const outNav = $("#tx-conv-out-nav").value;
  const inNav = $("#tx-conv-in-nav").value;
  if (!(parseFloat(outShares) > 0) || !(parseFloat(outNav) > 0) || !(parseFloat(inNav) > 0)) {
    toast("请填写转出份额与两侧净值"); return;
  }
  const body = JSON.stringify({
    from_code: _txCode,
    to_code: _txConvTo.fund_code,
    out_shares: outShares,
    out_nav: outNav,
    in_nav: inNav,
    fee: $("#tx-conv-fee").value || 0,
    trade_date: $("#tx-conv-date").value,
  });
  const r = await fetch("/api/transactions/convert", {
    method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body,
  });
  if (r.status === 401) return showAuth();
  if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.error || "转换失败"); return; }
  toast("已记录转换");
  clearConvertTarget();
  $("#tx-conv-out-shares").value = "";
  $("#tx-conv-hint").textContent = "";
  loadTransactions();
}

async function deleteTransaction(id) {
  if (!(await confirmDialog("删除这笔交易流水?（若为转换会连带删除对应的另一腿）", { okText: "删除", danger: true }))) return;
  const r = await fetch("/api/transactions/" + id, { method: "DELETE", credentials: "same-origin" });
  if (r.status === 401) return showAuth();
  loadTransactions();
}

// ---- 在持仓卡片上叠加「交易记录」入口 ----
// portfolio.js 每次 load() 都会整体重写 #list.innerHTML，故用 MutationObserver
// 监听其结果，为每张卡片补一个入口（幂等：已存在则跳过），不改动 portfolio.js。
function _txInjectEntries() {
  $$(".card .spark[data-code]").forEach(spark => {
    const card = spark.closest(".card");
    if (!card || card.querySelector(".txn")) return;
    const code = spark.dataset.code;
    const entry = document.createElement("span");
    entry.className = "txn";
    entry.textContent = "交易记录";
    entry.onclick = () => openTransactions(code);
    card.appendChild(entry);
  });
}

(function _txWatch() {
  const start = () => {
    const view = $("#view");
    if (!view) { setTimeout(start, 300); return; }
    new MutationObserver(_txInjectEntries).observe(view, { childList: true, subtree: true });
    _txInjectEntries();
  };
  start();
})();
