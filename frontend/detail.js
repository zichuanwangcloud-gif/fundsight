// 「基金详情」页 —— 基本面卡 + 净值折线 + 涨跌柱 SVG + 时间跨度切换 + 加自选。
// 路由 #/fund/020608 → registerPage("fund", ...)。归属「市场」Tab(见 app.js renderRoute)。
// cls / scls / sign / $ / getJSON 来自 app.js(全局)。

const DETAIL_SPANS = [
  { key: "30", label: "近30日", days: 30 },
  { key: "90", label: "近90日", days: 90 },
  { key: "180", label: "近180日", days: 180 },
  { key: "all", label: "全部", days: 3650 },
];

let _detailCode = null;
let _detailSpan = "90";

function renderDetail(view, code) {
  _detailCode = code;
  _detailSpan = "90";
  if (!code) {
    view.innerHTML = `<div class="placeholder">缺少基金代码</div>`;
    return;
  }
  view.innerHTML = `
    <div class="detail-head">
      <button class="ghost back" onclick="history.back()">← 返回</button>
      <button class="primary" id="d-add-btn" onclick="addToHoldings()">＋ 加自选</button>
    </div>
    <div id="d-profile" class="d-profile"><div class="d-loading">加载中…</div></div>
    <div class="d-chart-card">
      <div class="d-spans" id="d-spans">
        ${DETAIL_SPANS.map(s =>
          `<span class="d-span ${s.key === _detailSpan ? "active" : ""}" data-key="${s.key}"
                 onclick="switchSpan('${s.key}')">${s.label}</span>`).join("")}
      </div>
      <div id="d-chart"><div class="d-loading">加载中…</div></div>
    </div>`;
  loadDetail();
}

function switchSpan(key) {
  _detailSpan = key;
  $$("#d-spans .d-span").forEach(el => el.classList.toggle("active", el.dataset.key === key));
  loadDetail(true);
}

async function loadDetail(chartOnly) {
  const span = DETAIL_SPANS.find(s => s.key === _detailSpan) || DETAIL_SPANS[1];
  try {
    const d = await getJSON(
      "/api/fund/" + encodeURIComponent(_detailCode) + "?days=" + span.days
    );
    if (!chartOnly) renderProfile(d.profile);
    renderDetailChart(d.series || []);
  } catch {
    if (!chartOnly) $("#d-profile").innerHTML = `<div class="d-empty">基本面数据暂缺</div>`;
    $("#d-chart").innerHTML = `<div class="d-empty">走势数据暂缺</div>`;
  }
}

function renderProfile(p) {
  const box = $("#d-profile");
  if (!p) { box.innerHTML = `<div class="d-empty">基本面数据暂缺</div>`; return; }
  const fmtPct = v => v == null ? "—" : `<span class="${cls(v)}">${sign(v)}%</span>`;
  box.innerHTML = `
    <div class="d-name">${p.name || _detailCode}<span class="fcode">${_detailCode}</span></div>
    <div class="d-grid">
      <div>基金经理<b>${p.manager || "—"}</b></div>
      <div>规模(亿元)<b>${p.scale != null ? p.scale : "—"}</b></div>
      <div>管理费率<b>${p.rate != null ? p.rate + "%" : "—"}</b></div>
      <div>近1年收益<b>${fmtPct(p.syl_1n)}</b></div>
      <div>近3月收益<b>${fmtPct(p.syl_3y)}</b></div>
      <div>近6月收益<b>${fmtPct(p.syl_6y)}</b></div>
      <div>近1月收益<b>${fmtPct(p.syl_1y)}</b></div>
    </div>`;
}

// 净值折线 + 涨跌幅柱状层的放大版 SVG。points: [{date, nav, equity_return}]
// 复用 portfolio.js:sparkline 的 viewBox + polyline 思路，加一层柱状底图。
function detailChart(points) {
  if (!points || points.length < 2) return "";
  const W = 640, H = 220, padX = 8, padTop = 10, padBottom = 46;
  const navH = H - padTop - padBottom;      // 折线区高度
  const barH = 34;                          // 涨跌柱区高度(底部)
  const barTop = H - barH;
  const n = points.length;

  const navs = points.map(p => p.v_nav);
  const min = Math.min(...navs), max = Math.max(...navs);
  const span = max - min || 1;
  const xAt = i => padX + (W - 2 * padX) * i / (n - 1);
  const yAt = v => padTop + navH * (1 - (v - min) / span);

  const linePts = points.map((p, i) => `${xAt(i).toFixed(1)},${yAt(p.v_nav).toFixed(1)}`);
  const up = points[n - 1].v_nav >= points[0].v_nav;
  const lineColor = up ? "#e0483d" : "#16a34a";
  const pct = (((points[n - 1].v_nav - points[0].v_nav) / points[0].v_nav) * 100).toFixed(2);

  // 涨跌柱：以 equity_return 为高度，正负分色，居中于柱状带
  const rets = points.map(p => p.v_ret).filter(v => v != null);
  const maxAbsRet = Math.max(1, ...rets.map(v => Math.abs(v)));
  const barW = Math.max(1, (W - 2 * padX) / n - 1);
  const barMid = barTop + barH / 2;
  const bars = points.map((p, i) => {
    if (p.v_ret == null) return "";
    const x = xAt(i) - barW / 2;
    const h = (Math.abs(p.v_ret) / maxAbsRet) * (barH / 2 - 2);
    const y = p.v_ret >= 0 ? barMid - h : barMid;
    const color = p.v_ret >= 0 ? "#e0483d" : "#16a34a";
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}"
              height="${Math.max(0.5, h).toFixed(1)}" fill="${color}" opacity="0.75"/>`;
  }).join("");

  return `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
             class="d-chart-svg">
      <line x1="0" y1="${barMid.toFixed(1)}" x2="${W}" y2="${barMid.toFixed(1)}"
            stroke="#e2e6ee" stroke-width="1"/>
      ${bars}
      <polyline fill="none" stroke="${lineColor}" stroke-width="1.8"
                points="${linePts.join(" ")}" stroke-linejoin="round"/>
    </svg>
    <div class="d-chart-foot">
      <span class="d-chart-lbl ${cls(pct)}">区间涨跌 ${sign(pct)}%</span>
      <span class="d-chart-legend">上方折线:净值 · 下方柱状:每日涨跌幅</span>
    </div>`;
}

function renderDetailChart(series) {
  const box = $("#d-chart");
  if (!series.length) { box.innerHTML = `<div class="d-empty">暂无历史净值数据</div>`; return; }
  const points = series.map(p => ({ v_nav: p.nav, v_ret: p.equity_return, date: p.date }));
  const svg = detailChart(points);
  box.innerHTML = svg || `<div class="d-empty">数据点不足,暂无法画图</div>`;
}

async function addToHoldings() {
  const btn = $("#d-add-btn");
  btn.disabled = true;
  try {
    const r = await fetch("/api/holdings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ fund_code: _detailCode }),
    });
    if (r.status === 401) { showAuth(); return; }
    if (r.ok) { btn.textContent = "已加入自选 ✓"; }
    else { btn.textContent = "加入失败,重试"; }
  } catch {
    btn.textContent = "加入失败,重试";
  } finally {
    setTimeout(() => { btn.disabled = false; }, 800);
  }
}

registerPage("fund", renderDetail);
