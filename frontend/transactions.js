// 「交易记录」—— 持仓卡片内的入口 + 流水录入表单 + 列表(弹层)。
// 不是独立整页，不覆盖 registerPage("portfolio")；通过 MutationObserver 监听
// portfolio.js 渲染出的 #list，在每张持仓卡片上叠加一个「交易记录」入口。
// $ / $$ / getJSON / showAuth 来自 app.js / auth.js（全局）。

let _txCode = null;

function _txEnsureDialog() {
  if ($("#tx-dlg")) return;
  const dlg = document.createElement("dialog");
  dlg.id = "tx-dlg";
  dlg.innerHTML = `
    <h3 id="tx-dlg-title">交易记录</h3>
    <div id="tx-position" class="tx-position"></div>
    <div id="tx-list" class="tx-list"></div>
    <div class="tx-form">
      <label>方向</label>
      <select id="tx-action">
        <option value="buy">买入</option>
        <option value="sell">卖出</option>
      </select>
      <label>份额</label>
      <input id="tx-shares" type="number" step="0.01" placeholder="如 1000">
      <label>价格（净值，元）</label>
      <input id="tx-price" type="number" step="0.0001" placeholder="如 1.2345">
      <label>交易日期</label>
      <input id="tx-date" type="date">
      <div class="btns">
        <button class="ghost" onclick="document.getElementById('tx-dlg').close()">关闭</button>
        <button class="primary" onclick="submitTransaction()">记一笔</button>
      </div>
    </div>`;
  document.body.appendChild(dlg);
}

async function openTransactions(code) {
  _txCode = code;
  _txEnsureDialog();
  $("#tx-dlg-title").textContent = "交易记录 · " + code;
  $("#tx-shares").value = "";
  $("#tx-price").value = "";
  $("#tx-date").value = _todayLocal();  // 默认今天(本地时区,避免 UTC 偏差)
  await loadTransactions();
  $("#tx-dlg").showModal();
}

// 本地时区的今天 YYYY-MM-DD(toISOString 取的是 UTC,晚间会偏成昨天)。
function _todayLocal() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function _txNum(n) { return n == null ? "—" : Number(n).toLocaleString(); }

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
    ? items.map(it => `
      <div class="tx-row">
        <span class="tx-tag ${it.action === "buy" ? "buy" : "sell"}">${it.action === "buy" ? "买入" : "卖出"}</span>
        <span>${it.trade_date || ""}</span>
        <span>${it.shares} 份 @ ${it.price ?? "—"}</span>
        <span>${_txNum(it.amount)} 元</span>
        <span class="tx-del" role="button" tabindex="0" aria-label="删除交易流水" onclick="deleteTransaction(${it.id})">删除</span>
      </div>`).join("")
    : `<div class="empty">还没有交易记录</div>`;
}

async function submitTransaction() {
  const body = JSON.stringify({
    fund_code: _txCode,
    action: $("#tx-action").value,
    shares: $("#tx-shares").value,
    price: $("#tx-price").value,
    trade_date: $("#tx-date").value,
  });
  const r = await fetch("/api/transactions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body,
  });
  if (r.status === 401) return showAuth();
  if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.error || "提交失败"); return; }
  $("#tx-shares").value = "";
  $("#tx-price").value = "";
  loadTransactions();
}

async function deleteTransaction(id) {
  if (!(await confirmDialog("删除这笔交易流水?", { okText: "删除", danger: true }))) return;
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
