// 「我的基金」页 —— 搜索 + 自选(全集) / 持有(有金额子集) 双 Tab。
// 由 app.js 的路由在 #/portfolio 时调用 registerPage 的渲染函数。
// cls / scls / sign / $ 来自 app.js(全局)。
//
// 数据模型:GET /api/holdings 返回 items(每条带 kind:'hold'|'watch') + summary(hold_count/watch_count)。
//   持有 ⊆ 自选:有金额(kind==='hold' 或 hold_amount!=null)即「持有」,进组合盈亏/估值/止盈止损。
//   自选 Tab 展示全集(所有跟踪的基金),持有项打「持有」徽标;持有 Tab 只展示子集。
//   「加」只能加自选(不带金额);补金额(转持有/编辑)才成持有。

let _pfTimer = null;
let editingId = null;   // null=新增,非空=编辑该持仓 id
let _pfTab = "hold";    // 当前分区 tab:'hold'(持有) / 'watch'(自选),跨刷新保持

// 是否为「持有」:与后端 summarize 口径一致(kind==='hold' 或已录持仓金额)
function isHold(it) {
  return it.kind === "hold" || it.hold_amount != null;
}

// 切换持有/自选 tab
function switchPfTab(tab) {
  _pfTab = tab;
  $("#tab-btn-hold").classList.toggle("active", tab === "hold");
  $("#tab-btn-watch").classList.toggle("active", tab === "watch");
  $("#tab-hold").hidden = tab !== "hold";
  $("#tab-watch").hidden = tab !== "watch";
}

function renderPortfolio(view) {
  view.innerHTML = `
    <div id="index-bar" class="index-bar"></div>
    <div class="search-box">
      <input id="q" placeholder="搜索基金：代码 / 名称 / 拼音（如 020608、机器人、jqr）" autocomplete="off">
      <div id="results"></div>
    </div>
    <div class="ocr-entry">
      <button onclick="openOcrImport()">📷 上传截图识别持仓</button>
      <span class="ocr-privacy">支付宝/天天基金等持仓截图，自动识别基金与金额，核对后一键导入</span>
    </div>
    <div class="pf-tabs">
      <button id="tab-btn-hold" class="pf-tab active" onclick="switchPfTab('hold')">💰 持有 <span id="tab-n-hold" class="pf-tab-n"></span></button>
      <button id="tab-btn-watch" class="pf-tab" onclick="switchPfTab('watch')">⭐ 自选 <span id="tab-n-watch" class="pf-tab-n"></span></button>
    </div>
    <div id="tab-hold" class="pf-panel">
      <div id="summary" class="summary"></div>
      <div id="pf-allocation" class="summary"></div>
      <div id="hold-list"></div>
    </div>
    <div id="tab-watch" class="pf-panel" hidden>
      <div id="watch-list"></div>
    </div>
    <dialog id="dlg">
      <h3 id="dlg-title">添加自选</h3>
      <p class="dlg-hint" id="dlg-hint">自选只是加入关注列表，不计入组合盈亏。想跟踪真实收益，加入后在自选卡片点「转持有」补录金额。</p>
      <input type="hidden" id="d-code">
      <div id="d-hold-fields" hidden>
        <label>持仓金额（元）</label>
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
      </div>
      <div class="btns">
        <button class="ghost" onclick="document.getElementById('dlg').close()">取消</button>
        <button class="primary" id="dlg-submit" onclick="submitHolding()">加入自选</button>
      </div>
    </dialog>`;

  if (typeof startIndexBar === "function") startIndexBar("index-bar");

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
        `<div role="button" tabindex="0" aria-label="添加 ${f.name}"
              onclick='openDlg(${JSON.stringify(f).replace(/'/g, "&#39;")})'>
           ${f.name}<span class="code">${f.fund_code}</span>
           <span class="type">${f.fund_type || ""}</span></div>`).join("")
        || `<div class="results-none">无匹配结果</div>`;
      results.style.display = "block";
    }, 200);
  });
  document.addEventListener("click", e => {
    if (!e.target.closest(".search-box")) { const r = $("#results"); if (r) r.style.display = "none"; }
  });
  load();
}

// 弹窗字段显隐:金额区仅编辑/转持有时展开;纯新增自选隐藏(加只能加自选)。
// kind 不再显式选择,由后端按「是否录金额」推断(_kind_of)。
function _dlgShowAmount(show) {
  $("#d-hold-fields").hidden = !show;
}

function openDlg(f) {
  const results = $("#results"); if (results) results.style.display = "none";
  const q = $("#q"); if (q) q.value = "";
  editingId = null;
  $("#dlg-title").textContent = "加入自选 · " + (f.name || f.fund_code);
  $("#d-code").value = f.fund_code;
  $("#d-hold").value = $("#d-cost").value = $("#d-target").value = "";
  $("#d-target-rate").value = $("#d-profit").value = $("#d-loss").value = "";
  $("#dlg-hint").textContent = "自选只是加入关注列表，不计入组合盈亏。想跟踪真实收益，加入后在自选卡片点「转持有」补录金额。";
  $("#dlg-submit").textContent = "加入自选";
  _dlgShowAmount(false);   // 新增只加自选:隐藏金额区
  $("#dlg").showModal();
}

function editHolding(it) {
  editingId = it.id;
  $("#dlg-title").textContent = "编辑 · " + (it.name || it.fund_code);
  $("#d-code").value = it.fund_code;
  $("#d-hold").value = it.hold_amount ?? "";
  $("#d-cost").value = it.cost_amount ?? "";
  $("#d-target").value = it.target_price ?? "";
  $("#d-target-rate").value = it.target_rate ?? "";
  $("#d-profit").value = it.stop_profit ?? "";
  $("#d-loss").value = it.stop_loss ?? "";
  $("#dlg-hint").textContent = "填入持仓金额即计入组合(持有);清空金额则回落为纯自选。";
  $("#dlg-submit").textContent = "保存";
  _dlgShowAmount(true);
  $("#dlg").showModal();
}

// 自选 → 持有:打开弹窗展开金额区,预填代码待录金额
function convertToHold(it) {
  editingId = it.id;
  $("#dlg-title").textContent = "转持有 · " + (it.name || it.fund_code);
  $("#d-code").value = it.fund_code;
  $("#d-hold").value = it.hold_amount ?? "";
  $("#d-cost").value = it.cost_amount ?? "";
  $("#d-target").value = it.target_price ?? "";
  $("#d-target-rate").value = it.target_rate ?? "";
  $("#d-profit").value = it.stop_profit ?? "";
  $("#d-loss").value = it.stop_loss ?? "";
  $("#dlg-hint").textContent = "记持有会按金额算今日估值、收盘真实盈亏与止盈止损，并计入组合总览。";
  $("#dlg-submit").textContent = "加入持有";
  _dlgShowAmount(true);
  $("#dlg").showModal();
}

// 持有 → 自选:清空金额,降级为纯关注(确认后 PUT)
async function convertToWatch(it) {
  if (!(await confirmDialog("转为自选后将清除持仓金额与成本，仅保留关注。确定?", { okText: "转自选" }))) return;
  const body = JSON.stringify({
    fund_code: it.fund_code, kind: "watch",
    hold_amount: "", cost_amount: "",
    target_price: it.target_price ?? "", target_rate: it.target_rate ?? "",
    stop_profit: "", stop_loss: "",
  });
  const r = await fetch("/api/holdings/" + it.id, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body,
  });
  if (r.status === 401) return showAuth();
  load();
}

async function submitHolding() {
  // 金额区隐藏 = 纯新增自选(加只能加自选);展开 = 编辑/转持有,照发金额,kind 由后端按金额推断
  const watchMode = $("#d-hold-fields").hidden;
  const body = JSON.stringify({
    fund_code: $("#d-code").value,
    // 纯自选不提交金额字段(留空 → 后端存 null,推断为 watch),避免误算盈亏
    kind: watchMode ? "watch" : undefined,
    hold_amount: watchMode ? "" : $("#d-hold").value,
    cost_amount: watchMode ? "" : $("#d-cost").value,
    target_price: watchMode ? "" : $("#d-target").value,
    target_rate: watchMode ? "" : $("#d-target-rate").value,
    stop_profit: watchMode ? "" : $("#d-profit").value,
    stop_loss: watchMode ? "" : $("#d-loss").value,
  });
  const url = editingId ? "/api/holdings/" + editingId : "/api/holdings";
  const method = editingId ? "PUT" : "POST";
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, credentials: "same-origin", body });
  if (r.status === 401) { $("#dlg").close(); return showAuth(); }
  editingId = null;
  $("#dlg").close(); load();
}

async function del(id, isWatch) {
  const msg = isWatch ? "移除这只自选?" : "移除这只持有?（不影响已落袋的交易记录）";
  if (!(await confirmDialog(msg, { okText: "移除", danger: true }))) return;
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
  if (!s || !s.hold_count) { box.style.display = "none"; return; }
  const tot = s.total_pl, rate = s.total_return_rate;
  const now = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  // 今日盈亏头条:今日真实全部结算(today_settled)⟹ 用真实总额;否则用预估总额。
  const settled = !!s.today_settled;
  const todayPl = settled ? s.total_real_pl : s.total_today_pl;
  const todayTag = settled ? '<span class="tag nav">真实</span>' : '<span class="tag">预估</span>';
  box.innerHTML = `
    <div class="s-head">
      <span class="s-title">组合总览 · ${s.hold_count} 只持有</span>
      <span class="s-refresh">
        <span class="s-time">更新于 ${now}</span>
        <button onclick="load()" aria-label="刷新组合数据"><span class="s-refresh-ico">🔄</span> 刷新</button>
      </span>
    </div>
    <div class="s-grid">
      <div>总估值市值<b>${s.total_est_value.toLocaleString()}</b></div>
      ${todayPl != null ? `<div>今日盈亏${todayTag}<b class="${scls(todayPl)}">${sign(todayPl)}</b></div>` : ""}
      ${tot != null ? `<div>累计盈亏<b class="${scls(tot)}">${sign(tot)}</b></div>` : ""}
      ${rate != null ? `<div>总收益率<b class="${scls(rate)}">${sign(rate)}%</b></div>` : ""}
      ${!settled && s.total_real_pl != null ? `<div>收盘真实盈亏<b class="${scls(s.total_real_pl)}">${sign(s.total_real_pl)}</b></div>` : ""}
      ${s.total_realized_pnl != null ? `<div>已落袋<b class="${scls(s.total_realized_pnl)}">${sign(s.total_realized_pnl)}</b></div>` : ""}
    </div>
    ${tot != null && s.matched_count < s.hold_count ? `<div class="s-note">累计盈亏与收益率基于 ${s.matched_count} 笔有成本记录</div>` : ""}`;
  box.style.display = "block";
}

// 持有卡片:今日盈亏(预估↔真实互斥) + 止盈止损 + 走势(完整口径)
function renderHoldCard(it) {
  const z = it.gszzl;
  const pl = it.today_pl;
  const cr = it.cost_return_rate;
  const cardCls = ["card"];
  if (it.hit_stop_profit) cardCls.push("hit-profit");
  if (it.hit_stop_loss) cardCls.push("hit-loss");
  const badges = [];
  if (it.hit_stop_profit) badges.push(`<span class="badge profit">🎯 止盈</span>`);
  if (it.hit_stop_loss) badges.push(`<span class="badge loss">⚠️ 止损</span>`);
  // 今日盈亏头条:今日官方净值已出(real_is_today)⟹ 显示「今日真实盈亏」;
  // 否则显示「今日预估盈亏」(盘中/未结算)。二选一,真实已出即不再显示预估。
  const todayLine = it.real_is_today
    ? `<div>今日真实盈亏<span class="tag nav">${it.nav_date || "官方"}</span><b class="${cls(it.real_pl)}">${sign(it.real_pl)}</b></div>`
    : (pl != null ? `<div>今日预估盈亏<span class="tag">预估</span><b class="${cls(pl)}">${sign(pl)}</b></div>` : "");
  return `<div class="${cardCls.join(" ")}">
    <button type="button" class="del" aria-label="移除持有" onclick="del(${it.id}, false)">移除 ✕</button>
    <button type="button" class="edit" aria-label="编辑持有" onclick='editHolding(${JSON.stringify(it).replace(/'/g, "&#39;")})'>编辑 ✎</button>
    <div class="top">
      <div><span class="fname">${it.name || it.fund_code}</span>
           <span class="fcode">${it.fund_code}</span></div>
      <div class="zdf ${cls(z)}">${z == null ? "—" : sign(z) + "%"}</div>
    </div>
    <div class="detail-link"><a href="#/fund/${it.fund_code}" onclick="event.stopPropagation()">查看详情 →</a></div>
    <div class="metrics">
      ${it.hold_amount != null ? `<div>持仓金额<b>${it.hold_amount}</b></div>` : ""}
      ${todayLine}
      ${it.est_value != null ? `<div>估算市值<b>${it.est_value}</b></div>` : ""}
      ${cr != null ? `<div>持仓收益率<span class="tag">估算</span><b class="${cls(cr)}">${sign(cr)}%</b></div>` : ""}
      ${!it.real_is_today && it.real_pl != null ? `<div>收盘真实盈亏<span class="tag nav">${it.nav_date || "官方"}</span><b class="${cls(it.real_pl)}">${sign(it.real_pl)}</b></div>` : ""}
      ${it.real_return_rate != null ? `<div>真实收益率<b class="${cls(it.real_return_rate)}">${sign(it.real_return_rate)}%</b></div>` : ""}
      ${it.gap_to_target != null ? `<div>距目标净值<b>${sign(it.gap_to_target)}</b></div>` : ""}
    </div>
    <div class="spark" data-code="${it.fund_code}"></div>
    ${badges.length ? `<div class="badges">${badges.join("")}</div>` : ""}
    <div class="card-foot">
      <span class="gztime">${it.gztime ? ("估值时间 " + it.gztime) : ""}${staleHint(it.quote_updated_at)}</span>
      <button type="button" class="link-btn" onclick='convertToWatch(${JSON.stringify(it).replace(/'/g, "&#39;")})'>转自选</button>
    </div>
  </div>`;
}

// 自选卡片(全集):名称 + 当日涨幅 + 迷你走势。
// 持有(有金额)的:打「持有」徽标,动作为「编辑」;纯自选的:动作为「转持有」。
function renderWatchCard(it) {
  const z = it.gszzl;
  const held = isHold(it);
  const badge = held ? `<span class="w-badge">持有</span>` : "";
  const action = held
    ? `<button type="button" class="link-btn" onclick='editHolding(${JSON.stringify(it).replace(/'/g, "&#39;")})'>编辑</button>`
    : `<button type="button" class="link-btn" onclick='convertToHold(${JSON.stringify(it).replace(/'/g, "&#39;")})'>转持有</button>`;
  return `<div class="watch-card">
    <div class="w-main" role="button" tabindex="0" onclick="location.hash='#/fund/${it.fund_code}'">
      <div class="w-name"><span class="fname">${it.name || it.fund_code}</span>
           <span class="fcode">${it.fund_code}</span>${badge}</div>
      <div class="w-right">
        <span class="zdf ${cls(z)}">${z == null ? "—" : sign(z) + "%"}</span>
        <span class="spark w-spark" data-code="${it.fund_code}"></span>
      </div>
    </div>
    <div class="w-actions">
      ${action}
      <button type="button" class="link-btn danger" onclick="del(${it.id}, ${!held})">移除</button>
    </div>
  </div>`;
}

let _pfLoading = false;
let _pfPollTimer = null;   // 开盘时段 30s 自动刷新预估的定时器

// 盘中(market_open)且仍在持仓页 ⟹ 开 30s 轮询;否则清除(收盘/切页即停)。
function schedulePfPoll(open) {
  const onPage = location.hash.startsWith("#/portfolio");
  if (open && onPage) {
    if (!_pfPollTimer) _pfPollTimer = setInterval(pfPollTick, 30000);
  } else if (_pfPollTimer) {
    clearInterval(_pfPollTimer);
    _pfPollTimer = null;
  }
}

function pfPollTick() {
  // 页面已切走 ⟹ 停止轮询,防泄漏(路由销毁不显式回调,这里自守卫)。
  if (!location.hash.startsWith("#/portfolio")) {
    clearInterval(_pfPollTimer);
    _pfPollTimer = null;
    return;
  }
  load();   // 静默刷新:列表已有子节点,load 不弹「加载中」占位;_pfLoading 防抖并发。
}

async function load() {
  if (_pfLoading) return;                       // 防抖:加载中忽略重复点击(如连点刷新)
  _pfLoading = true;
  const refreshBtn = document.querySelector("#summary .s-refresh button");
  if (refreshBtn) { refreshBtn.disabled = true; refreshBtn.classList.add("spinning"); }
  const holdList = $("#hold-list");
  if (holdList && !holdList.children.length) holdList.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const r = await fetch("/api/holdings", { credentials: "same-origin" });
    if (r.status === 401) { showAuth(); return; }
    if (!r.ok) throw new Error("holdings " + r.status);
    const data = await r.json();
    const items = data.items || [];
    const holds = items.filter(isHold);

    renderSummary(data.summary);
    loadAllocation();

    // 持有分区(子集:有金额的)
    $("#hold-list").innerHTML = holds.length
      ? holds.map(renderHoldCard).join("")
      : `<div class="empty">还没有持有。在自选卡片点「转持有」录入金额，这里会算今日盈亏与真实收益。</div>`;

    // 自选分区(全集:所有跟踪的基金,持有 ⊆ 自选)
    $("#watch-list").innerHTML = items.length
      ? items.map(renderWatchCard).join("")
      : `<div class="empty">还没有自选。搜索基金加入关注，随时看涨跌 👆</div>`;

    // tab 计数:自选=全集,持有=子集 + 保持当前 tab
    $("#tab-n-hold").textContent = holds.length ? `(${holds.length})` : "";
    $("#tab-n-watch").textContent = items.length ? `(${items.length})` : "";
    switchPfTab(_pfTab);

    loadSparklines(items);
    schedulePfPoll(data.summary && data.summary.market_open);
  } catch (e) {
    if (holdList) holdList.innerHTML = `<div class="empty">加载失败，请检查网络后重试<br>
      <button class="ghost" style="margin-top:12px" onclick="load()">重试</button></div>`;
    if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.classList.remove("spinning"); }
  } finally {
    _pfLoading = false;
  }
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
  const color = up ? "#e5432f" : "#0f9d58";   // 与 theme.css --up / --down 一致
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

// 资产配置饼图 + 持仓集中度(PRD-03)。数据来自 GET /api/portfolio/summary。
const ALLOC_PALETTE = ["#2b5bd7","#e5432f","#0f9d58","#f59e0b","#8b5cf6","#06b6d4","#ec4899","#64748b","#a3a3a3"];

function allocationPieSvg(allocation) {
  const items = (allocation || []).filter(a => a.amount > 0);
  if (!items.length) return "";
  const r = 64, cx = 70, cy = 70;
  let angle = -Math.PI / 2;
  const slices = items.map((a, i) => {
    const sweep = a.ratio * 2 * Math.PI;
    const x1 = cx + r * Math.cos(angle), y1 = cy + r * Math.sin(angle);
    angle += sweep;
    const x2 = cx + r * Math.cos(angle), y2 = cy + r * Math.sin(angle);
    const large = sweep > Math.PI ? 1 : 0;
    const color = ALLOC_PALETTE[i % ALLOC_PALETTE.length];
    return `<path d="M${cx},${cy} L${x1.toFixed(1)},${y1.toFixed(1)} A${r},${r} 0 ${large} 1 ${x2.toFixed(1)},${y2.toFixed(1)} Z" fill="${color}" opacity="0.85"><title>${a.cat} ${(a.ratio*100).toFixed(1)}%</title></path>`;
  }).join("");
  return `<svg width="140" height="140" viewBox="0 0 140 140">${slices}</svg>`;
}

function renderAllocation(s) {
  const box = $("#pf-allocation");
  if (!box) return;
  if (!s || !s.holdings_count) { box.style.display = "none"; return; }
  const legend = (s.allocation || []).filter(a => a.amount > 0).map((a, i) =>
    `<span class="leg-item"><i style="background:${ALLOC_PALETTE[i % ALLOC_PALETTE.length]}"></i>${a.cat} ${(a.ratio*100).toFixed(0)}%</span>`).join("");
  const c = s.concentration || {};
  box.innerHTML = `
    <div class="s-head"><span class="s-title">资产配置 · ${s.holdings_count} 只持有</span></div>
    <div class="alloc-row">${allocationPieSvg(s.allocation)}<div class="alloc-legend">${legend}</div></div>
    ${c.top1_fund_code ? `<div class="s-note">持仓集中度:TOP1 ${c.top1_fund_code} ${(c.top1_ratio*100).toFixed(0)}%${c.warn ? ' · <b class="loss">集中度偏高(>40%)</b>' : ""} · 前3合计 ${(c.cr3*100).toFixed(0)}%</div>` : ""}`;
  box.style.display = "block";
}

async function loadAllocation() {
  const box = $("#pf-allocation");
  if (!box) return;
  try {
    const r = await fetch("/api/portfolio/summary", { credentials: "same-origin" });
    if (!r.ok) { box.style.display = "none"; return; }
    renderAllocation(await r.json());
  } catch { box.style.display = "none"; }
}

registerPage("portfolio", renderPortfolio);
