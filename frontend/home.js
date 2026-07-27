// 首页概览「今天」—— 把组合盈亏 / 离目标进度 / 今日提醒 / AI 早晚报收口到第一屏。
// 由 app.js 路由在 #/home 调用;复用全局 $ / cls / scls / sign / getJSON / startIndexBar。
// 红线:进度用进度条/百分比呈现,不画连续走势曲线;AI 早晚报点开才生成。

function _esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function _fmtMoney(v) {
  if (v == null) return "—";
  return (v > 0 ? "+" : "") + v.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function renderHome(view) {
  view.innerHTML = `
    <div id="index-bar" class="index-bar"></div>
    <div id="home-hero" class="home-hero"><div class="home-loading">加载今日概览中…</div></div>
    <div id="home-goals"></div>
    <div id="home-alerts"></div>
    <div id="home-ai" class="home-ai"></div>`;
  if (typeof startIndexBar === "function") startIndexBar("index-bar");
  loadHome();
}

async function loadHome() {
  let ov;
  try {
    const r = await fetch("/api/home/overview", { credentials: "same-origin" });
    if (r.status === 401) return (typeof showAuth === "function") && showAuth();
    ov = await r.json();
  } catch (e) {
    $("#home-hero").innerHTML = `<div class="home-err">概览加载失败,请稍后重试。</div>`;
    return;
  }
  renderHero(ov);
  renderGoals(ov.goals || []);
  renderAlerts(ov);
  renderAiSlot();
}

function renderHero(ov) {
  const s = ov.summary || {};
  const tv = s.total_market_value || 0;
  const todayPl = ov.today_pl, todayPct = ov.today_pl_pct;
  const empty = !ov.holdings || !ov.holdings.length;
  if (empty) {
    $("#home-hero").innerHTML = `
      <div class="hero-empty">
        <div class="hero-empty-title">还没有持仓</div>
        <div class="hero-empty-sub">去「持仓」搜索基金、录入成本与预期目标,这里就会每天告诉你<br>今天赚亏多少、离目标还差多远。</div>
        <button class="primary" onclick="location.hash='#/portfolio'">去添加持仓 →</button>
      </div>`;
    return;
  }
  // 今日盈亏大数 + 情绪色
  const spark = (ov.spark || []).map(h => {
    const z = h.today_rate;
    const w = Math.min(Math.abs(z || 0) * 6, 100);
    return `<div class="spark-row" role="button" tabindex="0" aria-label="查看 ${_esc(h.name)}"
                 onclick="location.hash='#/fund/${_esc(h.fund_code)}'">
       <span class="spark-name">${_esc(h.name)}</span>
       <span class="spark-bar"><i class="${cls(z)}" style="width:${w}%"></i></span>
       <span class="spark-val ${cls(z)}">${z == null ? "—" : sign(z) + "%"}</span>
     </div>`;
  }).join("");

  const realized = s.total_realized_pnl;
  $("#home-hero").innerHTML = `
    <div class="hero-card">
      <div class="hero-today">
        <div class="hero-label">今日盈亏</div>
        <div class="hero-num ${scls(todayPl)}">${_fmtMoney(todayPl)}<span class="hero-pct">${todayPct == null ? "" : " " + sign(todayPct) + "%"}</span></div>
      </div>
      <div class="hero-meta">
        <div>总市值<b>¥${tv.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}</b></div>
        <div>累计收益率<b class="${scls(s.total_return_pct)}">${s.total_return_pct == null ? "—" : sign(s.total_return_pct) + "%"}</b></div>
        <div>浮动盈亏<b class="${scls(s.total_pnl)}">${_fmtMoney(s.total_pnl)}</b></div>
        ${realized != null ? `<div>已落袋<b class="${scls(realized)}">${_fmtMoney(realized)}</b></div>` : ""}
        <div>持有<b>${s.holdings_count || 0} 只</b></div>
      </div>
    </div>
    <div class="hero-spark">
      <div class="block-title">今日涨跌</div>
      ${spark || `<div class="home-muted">暂无行情</div>`}
    </div>`;
}

function renderGoals(goals) {
  if (!goals.length) { $("#home-goals").innerHTML = ""; return; }
  const rows = goals.map(g => {
    let bar = "", tail = "";
    if (g.target_progress_pct != null) {
      bar = `<span class="goal-bar"><i style="width:${g.target_progress_pct}%"></i></span>`;
      tail = `<span class="goal-tail">目标进度 ${g.target_progress_pct}%</span>`;
    } else if (g.recovery_progress_pct != null) {
      bar = `<span class="goal-bar recover"><i style="width:${g.recovery_progress_pct}%"></i></span>`;
      tail = `<span class="goal-tail">回本进度 ${g.recovery_progress_pct}%</span>`;
    }
    let dist = "";
    if (g.dist_to_stop_profit != null) {
      dist = g.dist_to_stop_profit > 0
        ? `<span class="goal-dist">距止盈还差 ${g.dist_to_stop_profit}%</span>`
        : `<span class="goal-dist hit">已达止盈线</span>`;
    }
    return `<div class="goal-row" role="button" tabindex="0" aria-label="查看 ${_esc(g.name)}"
                 onclick="location.hash='#/fund/${_esc(g.fund_code)}'">
      <div class="goal-head">
        <span class="goal-name">${_esc(g.name)}</span>
        <span class="goal-ret ${scls(g.current_return_pct)}">${g.current_return_pct == null ? "—" : sign(g.current_return_pct) + "%"}</span>
      </div>
      ${bar}
      <div class="goal-foot">${tail}${dist}</div>
    </div>`;
  }).join("");
  $("#home-goals").innerHTML = `<div class="block"><div class="block-title">离目标还差多少</div>${rows}</div>`;
}

function renderAlerts(ov) {
  const alerts = ov.alerts || [];
  const icon = { stop_profit: "🎯", stop_loss: "⚠️", trailing_stop: "📉", dca_due: "📅" };
  let inner = "";
  if (!alerts.length) {
    inner = `<div class="home-muted">今天没有触发的提醒 ✨</div>`;
  } else {
    inner = alerts.map(a => `
      <div class="alert-row sev-${_esc(a.severity)}" role="button" tabindex="0"
           aria-label="查看 ${_esc(a.name)}" onclick="location.hash='#/fund/${_esc(a.fund_code)}'">
        <span class="alert-ico">${icon[a.kind] || "🔔"}</span>
        <span class="alert-name">${_esc(a.name)}</span>
        <span class="alert-msg">${_esc(a.message)}</span>
      </div>`).join("");
  }
  const unread = ov.unread_notifications || 0;
  const unreadLine = unread
    ? `<div class="alert-more" role="button" tabindex="0" onclick="showNotifs && showNotifs()">另有 ${unread} 条未读通知 →</div>`
    : "";
  $("#home-alerts").innerHTML = `<div class="block"><div class="block-title">今天要看</div>${inner}${unreadLine}</div>`;
}

function renderAiSlot() {
  $("#home-ai").innerHTML = `
    <div class="block">
      <div class="block-title">AI 早晚报
        <button class="ai-gen-btn" type="button" onclick="genBriefing()">生成 ✨</button>
      </div>
      <div id="ai-brief" class="ai-brief home-muted">点「生成」让 AI 用一句话点评今日组合(仅供参考,不构成投资建议)。</div>
    </div>`;
}

async function genBriefing() {
  const box = $("#ai-brief");
  const btn = document.querySelector(".ai-gen-btn");
  box.classList.remove("home-muted");
  box.textContent = "生成中…";
  if (btn) btn.disabled = true;
  try {
    const r = await fetch("/api/home/briefing", { credentials: "same-origin" });
    if (r.status === 401) return (typeof showAuth === "function") && showAuth();
    const d = await r.json();
    box.innerHTML = `<div class="ai-brief-text">${_esc(d.text)}</div>` +
      (d.disclaimer ? `<div class="ai-brief-foot">${_esc(d.disclaimer)}</div>` :
        `<div class="ai-brief-foot">本内容由 AI 基于公开数据自动生成,仅供参考,不构成投资建议;市场有风险,投资需谨慎。</div>`);
  } catch (e) {
    box.textContent = "生成失败,请稍后重试。";
  } finally {
    if (btn) btn.disabled = false;
  }
}

if (typeof registerPage === "function") registerPage("home", renderHome);
