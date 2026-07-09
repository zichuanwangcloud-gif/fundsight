// 「我的持仓」页 —— 搜索加自选 + 持仓卡片(盈亏/止盈止损/走势图)。
// 由 app.js 的路由在 #/portfolio 时调用 registerPage 的渲染函数。
// cls / scls / sign / $ 来自 app.js(全局)。

let _pfTimer = null;
let editingId = null;   // null=新增，非空=编辑该持仓 id

function renderPortfolio(view) {
  view.innerHTML = `
    <div class="search-box">
      <input id="q" placeholder="搜索基金：代码 / 名称 / 拼音（如 020608、机器人、jqr）" autocomplete="off">
      <div id="results"></div>
    </div>
    <div id="summary" class="summary"></div>
    <div id="list"></div>
    <dialog id="dlg">
      <h3 id="dlg-title">添加自选</h3>
      <input type="hidden" id="d-code">
      <label>持仓金额（元，可留空）</label>
      <input id="d-hold" type="number" step="0.01" placeholder="如 10000">
      <label>买入成本（元，可留空）</label>
      <input id="d-cost" type="number" step="0.01" placeholder="如 8500">
      <label>目标净值（预期，可留空）</label>
      <input id="d-target" type="number" step="0.0001" placeholder="如 1.80">
      <label>目标收益率 %（可留空）</label>
      <input id="d-target-rate" type="number" step="0.01" placeholder="如 15">
      <label>止盈线 %（达到即提醒，可留空）</label>
      <input id="d-profit" type="number" step="0.01" placeholder="如 10">
      <label>止损线 %（跌破即提醒，通常为负，可留空）</label>
      <input id="d-loss" type="number" step="0.01" placeholder="如 -8">
      <div class="btns">
        <button class="ghost" onclick="document.getElementById('dlg').close()">取消</button>
        <button class="primary" onclick="submitHolding()">加入自选</button>
      </div>
    </dialog>`;

  const q = $("#q"), results = $("#results");
  q.addEventListener("input", () => {
    clearTimeout(_pfTimer);
    const v = q.value.trim();
    if (!v) { results.style.display = "none"; return; }
    _pfTimer = setTimeout(async () => {
      const r = await fetch("/api/search?q=" + encodeURIComponent(v), { credentials: "same-origin" });
      if (r.status === 401) return showAuth();
      const funds = await r.json();
      results.innerHTML = funds.map(f =>
        `<div onclick='openDlg(${JSON.stringify(f).replace(/'/g, "&#39;")})'>
           ${f.name}<span class="code">${f.fund_code}</span>
           <span class="type">${f.fund_type || ""}</span></div>`).join("")
        || `<div style="color:#a0a8b8">无匹配结果</div>`;
      results.style.display = "block";
    }, 200);
  });
  document.addEventListener("click", e => {
    if (!e.target.closest(".search-box")) { const r = $("#results"); if (r) r.style.display = "none"; }
  });
  load();
}

function openDlg(f) {
  const results = $("#results"); if (results) results.style.display = "none";
  const q = $("#q"); if (q) q.value = "";
  editingId = null;
  $("#dlg-title").textContent = "添加自选 · " + f.name;
  $("#d-code").value = f.fund_code;
  $("#d-hold").value = $("#d-cost").value = $("#d-target").value = "";
  $("#d-target-rate").value = $("#d-profit").value = $("#d-loss").value = "";
  $("#dlg").showModal();
}
function editHolding(it) {
  editingId = it.id;
  $("#dlg-title").textContent = "编辑持仓 · " + (it.name || it.fund_code);
  $("#d-code").value = it.fund_code;
  $("#d-hold").value = it.hold_amount ?? "";
  $("#d-cost").value = it.cost_amount ?? "";
  $("#d-target").value = it.target_price ?? "";
  $("#d-target-rate").value = it.target_rate ?? "";
  $("#d-profit").value = it.stop_profit ?? "";
  $("#d-loss").value = it.stop_loss ?? "";
  $("#dlg").showModal();
}
async function submitHolding() {
  const body = JSON.stringify({
    fund_code: $("#d-code").value,
    hold_amount: $("#d-hold").value,
    cost_amount: $("#d-cost").value,
    target_price: $("#d-target").value,
    target_rate: $("#d-target-rate").value,
    stop_profit: $("#d-profit").value,
    stop_loss: $("#d-loss").value,
  });
  const url = editingId ? "/api/holdings/" + editingId : "/api/holdings";
  const method = editingId ? "PUT" : "POST";
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, credentials: "same-origin", body });
  if (r.status === 401) { $("#dlg").close(); return showAuth(); }
  editingId = null;
  $("#dlg").close(); load();
}
async function del(id) {
  if (!confirm("移除这只自选？")) return;
  const r = await fetch("/api/holdings/" + id, { method: "DELETE", credentials: "same-origin" });
  if (r.status === 401) return showAuth();
  load();
}

// 估值数据是否偏旧:缓存写入时间距今 > 5 分钟则提示（盘中场景）
function staleHint(updatedAt) {
  if (!updatedAt) return "";
  const t = new Date(updatedAt.replace(/-/g, "/")).getTime();
  if (isNaN(t)) return "";
  const mins = (Date.now() - t) / 60000;
  return mins > 5 ? ` · <span class="stale">数据延迟 ${Math.round(mins)} 分钟</span>` : "";
}

function renderSummary(s) {
  const box = $("#summary");
  if (!s || !s.count) { box.style.display = "none"; return; }
  const pl = s.total_today_pl, tot = s.total_pl, rate = s.total_return_rate;
  const now = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  box.innerHTML = `
    <div class="s-head">
      <span class="s-title">组合总览 · ${s.count} 只自选</span>
      <span class="s-refresh">
        <span class="s-time">更新于 ${now}</span>
        <button onclick="load()">🔄 刷新</button>
      </span>
    </div>
    <div class="s-grid">
      <div>总估值市值<b>${s.total_est_value.toLocaleString()}</b></div>
      <div>今日盈亏<b class="${scls(pl)}">${sign(pl)}</b></div>
      ${tot != null ? `<div>累计盈亏<b class="${scls(tot)}">${sign(tot)}</b></div>` : ""}
      ${rate != null ? `<div>总收益率<b class="${scls(rate)}">${sign(rate)}%</b></div>` : ""}
      ${s.total_real_pl != null ? `<div>收盘真实盈亏<b class="${scls(s.total_real_pl)}">${sign(s.total_real_pl)}</b></div>` : ""}
    </div>
    ${tot != null && s.matched_count < s.count ? `<div class="s-note">累计盈亏与收益率基于 ${s.matched_count} 笔有成本记录</div>` : ""}`;
  box.style.display = "block";
}

async function load() {
  const r = await fetch("/api/holdings", { credentials: "same-origin" });
  if (r.status === 401) return showAuth();
  const data = await r.json();
  const items = data.items || [];
  renderSummary(data.summary);
  if (!items.length) { $("#list").innerHTML = `<div class="empty">还没有自选，搜索基金加入吧 👆</div>`; return; }
  $("#list").innerHTML = items.map(it => {
    const z = it.gszzl;
    const pl = it.today_pl;
    const cr = it.cost_return_rate;
    const cardCls = ["card"];
    if (it.hit_stop_profit) cardCls.push("hit-profit");
    if (it.hit_stop_loss) cardCls.push("hit-loss");
    const badges = [];
    if (it.hit_stop_profit) badges.push(`<span class="badge profit">🎯 止盈</span>`);
    if (it.hit_stop_loss) badges.push(`<span class="badge loss">⚠️ 止损</span>`);
    return `<div class="${cardCls.join(" ")}">
      <span class="del" onclick="del(${it.id})">移除 ✕</span>
      <span class="edit" onclick='editHolding(${JSON.stringify(it).replace(/'/g, "&#39;")})'>编辑 ✎</span>
      <div class="top">
        <div><span class="fname">${it.name || it.fund_code}</span>
             <span class="fcode">${it.fund_code}</span></div>
        <div class="zdf ${cls(z)}">${z == null ? "—" : sign(z) + "%"}</div>
      </div>
      <div class="detail-link"><a href="#/fund/${it.fund_code}" onclick="event.stopPropagation()">查看详情 →</a></div>
      <div class="metrics">
        ${it.hold_amount != null ? `<div>持仓金额<b>${it.hold_amount}</b></div>` : ""}
        ${pl != null ? `<div>今日盈亏<span class="tag">估算</span><b class="${cls(pl)}">${sign(pl)}</b></div>` : ""}
        ${it.est_value != null ? `<div>估算市值<b>${it.est_value}</b></div>` : ""}
        ${cr != null ? `<div>持仓收益率<span class="tag">估算</span><b class="${cls(cr)}">${sign(cr)}%</b></div>` : ""}
        ${it.real_pl != null ? `<div>收盘真实盈亏<span class="tag nav">${it.nav_date || "官方"}</span><b class="${cls(it.real_pl)}">${sign(it.real_pl)}</b></div>` : ""}
        ${it.real_return_rate != null ? `<div>真实收益率<b class="${cls(it.real_return_rate)}">${sign(it.real_return_rate)}%</b></div>` : ""}
        ${it.gap_to_target != null ? `<div>距目标净值<b>${sign(it.gap_to_target)}</b></div>` : ""}
      </div>
      <div class="spark" data-code="${it.fund_code}"></div>
      ${badges.length ? `<div class="badges">${badges.join("")}</div>` : ""}
      <div class="gztime">${it.gztime ? ("估值时间 " + it.gztime) : ""}${staleHint(it.quote_updated_at)}</div>
    </div>`;
  }).join("");
  loadSparklines(items);
}

// 净值序列 → 迷你 SVG 折线(零依赖)。points: [{d,v}]
function sparkline(points) {
  if (!points || points.length < 2) return "";
  const W = 130, H = 34, pad = 2;
  const vs = points.map(p => p.v);
  const min = Math.min(...vs), max = Math.max(...vs);
  const span = max - min || 1;
  const n = points.length;
  const xy = points.map((p, i) => {
    const x = pad + (W - 2 * pad) * i / (n - 1);
    const y = pad + (H - 2 * pad) * (1 - (p.v - min) / span);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const up = points[n - 1].v >= points[0].v;
  const color = up ? "#e0483d" : "#16a34a";
  const pct = (((points[n - 1].v - points[0].v) / points[0].v) * 100).toFixed(1);
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
      <polyline fill="none" stroke="${color}" stroke-width="1.5"
                points="${xy.join(" ")}" stroke-linejoin="round"/>
    </svg><span class="spark-lbl" style="color:${color}">近${n}日 ${up ? "+" : ""}${pct}%</span>`;
}

async function loadSparklines(items) {
  await Promise.all(items.map(async it => {
    const box = document.querySelector(`.spark[data-code="${it.fund_code}"]`);
    if (!box) return;
    try {
      const d = await getJSON("/api/nav_history?code=" + encodeURIComponent(it.fund_code) + "&days=90");
      const svg = sparkline(d.points);
      if (svg) box.innerHTML = svg; else box.style.display = "none";
    } catch { box.style.display = "none"; }
  }));
}

registerPage("portfolio", renderPortfolio);
