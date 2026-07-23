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
let _intradayTimer = null;  // 今日实时涨幅折线轮询 timer(盘中 30s,收盘停)

function renderDetail(view, code) {
  _detailCode = code;
  _detailSpan = "90";
  // 切回详情页重新渲染:先停上一只基金的轮询,避免跨基金串数据
  if (_intradayTimer) { clearInterval(_intradayTimer); _intradayTimer = null; }
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
    <div id="d-fundamentals" class="d-profile" hidden></div>
    <div id="d-holdings" class="d-profile" hidden></div>
    <div id="d-compare" class="d-chart-card" hidden></div>
    <div id="d-intraday" class="d-chart-card"><div class="d-loading">实时涨幅加载中…</div></div>
    <div id="d-returns" class="d-profile"><div class="d-loading">阶段收益加载中…</div></div>
    <div id="d-risk" class="d-profile"><div class="d-loading">风险指标加载中…</div></div>
    <div id="d-cost-curve" class="d-chart-card" hidden><div class="d-loading">成本曲线加载中…</div></div>
    <div id="d-attribution" class="d-chart-card" hidden><div class="d-loading">归因加载中…</div></div>
    <div class="d-chart-card">
      <div class="d-spans" id="d-spans">
        ${DETAIL_SPANS.map(s =>
          `<span class="d-span ${s.key === _detailSpan ? "active" : ""}" data-key="${s.key}"
                 onclick="switchSpan('${s.key}')">${s.label}</span>`).join("")}
      </div>
      <div id="d-chart"><div class="d-loading">加载中…</div></div>
    </div>`;
  loadDetail();
  loadIntraday();
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
    if (!chartOnly) {
      renderProfile(d.profile);
      renderFundamentals(d.profile);
      loadHoldings();
      loadCompare();
      getJSON("/api/fund/" + encodeURIComponent(_detailCode) + "/returns")
        .then(ret => renderReturns(ret.periods))
        .catch(() => { const b = $("#d-returns"); if (b) b.innerHTML = ""; });
      loadCostCurve();
      loadAttribution();
      loadRisk();
    }
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
      <div>管理费率<b>${p.rate != null && p.rate !== "" ? (String(p.rate).includes("%") ? p.rate : p.rate + "%") : "—"}</b></div>
      <div>近1年收益<b>${fmtPct(p.syl_1n)}</b></div>
      <div>近3月收益<b>${fmtPct(p.syl_3y)}</b></div>
      <div>近6月收益<b>${fmtPct(p.syl_6y)}</b></div>
      <div>近1月收益<b>${fmtPct(p.syl_1y)}</b></div>
    </div>`;
}

// 同类排名 + 资产配置 + 持有人结构 —— 数据已在 profile 响应里(PRD-05/06 已抓),此处呈现。
function renderFundamentals(p) {
  const box = $("#d-fundamentals");
  if (!box) return;
  if (!p) { box.hidden = true; return; }
  const blocks = [];

  // 同类排名
  if (p.peer_rank != null && p.peer_total) {
    const pct = p.peer_percentile != null ? ` · 超越 ${(+p.peer_percentile).toFixed(0)}% 同类` : "";
    blocks.push(`<div class="d-fund-sec">
      <div class="d-fund-h">同类排名</div>
      <div class="d-fund-rank"><b>${p.peer_rank}</b> / ${p.peer_total}<span class="d-fund-note">${pct}</span></div>
    </div>`);
  }

  // 资产配置(股/债/现金 占净比,横向条)
  const alloc = [
    { k: "股票", v: p.asset_alloc_stock, c: "var(--up)" },
    { k: "债券", v: p.asset_alloc_bond, c: "var(--brand)" },
    { k: "现金", v: p.asset_alloc_cash, c: "var(--down)" },
  ].filter(a => a.v != null);
  if (alloc.length) {
    blocks.push(`<div class="d-fund-sec">
      <div class="d-fund-h">资产配置<span class="d-fund-note">最新一期 占净值比</span></div>
      ${alloc.map(a => `<div class="d-bar-row">
        <span class="d-bar-k">${a.k}</span>
        <span class="d-bar-track"><i style="width:${Math.max(0, Math.min(100, a.v))}%;background:${a.c}"></i></span>
        <span class="d-bar-v">${(+a.v).toFixed(1)}%</span>
      </div>`).join("")}
    </div>`);
  }

  // 持有人结构(机构/个人)
  if (p.holder_inst != null || p.holder_retail != null) {
    const rows = [
      { k: "机构持有", v: p.holder_inst, c: "var(--brand)" },
      { k: "个人持有", v: p.holder_retail, c: "var(--gold)" },
    ].filter(a => a.v != null);
    blocks.push(`<div class="d-fund-sec">
      <div class="d-fund-h">持有人结构<span class="d-fund-note">最新一期</span></div>
      ${rows.map(a => `<div class="d-bar-row">
        <span class="d-bar-k">${a.k}</span>
        <span class="d-bar-track"><i style="width:${Math.max(0, Math.min(100, a.v))}%;background:${a.c}"></i></span>
        <span class="d-bar-v">${(+a.v).toFixed(1)}%</span>
      </div>`).join("")}
    </div>`);
  }

  if (!blocks.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<div class="d-name">基金档案 <span class="fcode">同类 · 配置 · 持有人</span></div>
    <div class="d-fund-grid">${blocks.join("")}</div>`;
}

// 重仓股 Top10 —— GET /api/fund/{code}/holdings。名次/名称/占净比条,点进个股行情。
async function loadHoldings() {
  const box = $("#d-holdings");
  if (!box) return;
  let data;
  try {
    data = await getJSON("/api/fund/" + encodeURIComponent(_detailCode) + "/holdings");
  } catch { box.hidden = true; return; }
  const items = (data && data.holdings) || [];
  if (!items.length) { box.hidden = true; return; }
  box.hidden = false;
  const maxW = Math.max(...items.map(h => h.weight || 0), 1);
  box.innerHTML = `<div class="d-name">重仓股 Top${items.length}
      <span class="fcode">${data.period || ""}</span></div>
    <div class="d-hold-list">${items.map(h => holdingRow(h, maxW)).join("")}</div>
    <div class="d-chart-legend">占比为占基金净值比例;点名称看个股行情</div>`;
}

function holdingRow(h, maxW) {
  const w = h.weight || 0;
  const barPct = Math.max(2, (w / maxW) * 100);
  return `<div class="d-hold-row">
      <span class="d-hold-no">${h.rank}</span>
      <a class="d-hold-name" href="//quote.eastmoney.com/unify/r/${(h.stock_code || "").length === 6 && h.stock_code[0] === "6" ? "1" : "0"}.${h.stock_code}" target="_blank" rel="noopener">${h.stock_name || h.stock_code}</a>
      <span class="d-hold-track"><i style="width:${barPct}%"></i></span>
      <span class="d-hold-w">${h.weight != null ? (+h.weight).toFixed(2) + "%" : "—"}</span>
    </div>`;
}

// 同类对比叠加图 —— 本基金 vs 同类平均 vs 沪深300 累计收益率(GET /api/fund/{code}/compare)。
const _COMPARE_COLORS = { self: "#e5432f", peer: "#9aa2b3", hs300: "#2b5bd7" };

async function loadCompare() {
  const box = $("#d-compare");
  if (!box) return;
  let data;
  try {
    data = await getJSON("/api/fund/" + encodeURIComponent(_detailCode) + "/compare");
  } catch { box.hidden = true; return; }
  const series = ((data && data.series) || []).map(s => ({
    name: s.name,
    color: _COMPARE_COLORS[s.key] || "#9aa2b3",
    points: (s.points || []).map(p => ({ label: p.date, value: p.value })),
  })).filter(s => s.points.length >= 2);
  if (!series.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<div class="d-name">同类对比<span class="fcode">累计收益率 · 本基金 / 同类平均 / 沪深300</span></div>
    <div id="d-compare-chart"></div>`;
  renderMultiLineChart($("#d-compare-chart"), series, {
    height: 240,
    fmtLabel: d => String(d || "").slice(5),
    emptyHint: "暂无对比数据",
  });
}

function renderReturns(periods) {
  const box = $("#d-returns");
  if (!box || !periods) return;
  const fmt = v => v == null ? "—" : `<span class="${cls(v)}">${sign(v)}%</span>`;
  box.innerHTML = `
    <div class="d-name">阶段收益<span class="fcode">基于历史净值只读计算</span></div>
    <div class="d-grid">
      <div>近1月<b>${fmt(periods.m1)}</b></div>
      <div>近3月<b>${fmt(periods.m3)}</b></div>
      <div>今年以来<b>${fmt(periods.ytd)}</b></div>
      <div>成立以来<b>${fmt(periods.max)}</b></div>
    </div>`;
}

// 净值走势 —— 可交互折线(chart.js):坐标轴 + 网格 + 十字准星 + 悬停 tooltip。
// series: [{date, nav, equity_return}]。当日涨跌幅进 tooltip,不再画柱状层。
function renderDetailChart(series) {
  const box = $("#d-chart");
  if (!series || !series.length) { box.innerHTML = `<div class="d-empty">暂无历史净值数据</div>`; return; }
  const points = series.map(p => {
    const ret = p.equity_return;
    const retLine = ret == null ? ""
      : `<div>当日 <span class="${cls(ret)}">${sign(+ret.toFixed(2))}%</span></div>`;
    return {
      label: p.date,
      value: p.nav,
      tip: `<b>净值 ${p.nav != null ? (+p.nav).toFixed(4) : "—"}</b>${retLine}`,
    };
  });
  renderLineChart(box, points, {
    height: 232,
    fmtValue: v => (+v).toFixed(3),
    fmtLabel: d => String(d || "").slice(5),
    footRight: `<span class="legend">悬停查看每日净值 · 当日涨跌</span>`,
    emptyHint: "数据点不足,暂无法画图",
  });
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

// 分批买入加权成本曲线 —— 仅画点列(每次买入后的加权成本),不画连续走势(红线)。
// points: [{date, shares, cost_basis, weighted_price}]
function costCurveSvg(points) {
  if (!points || points.length < 1) return "";
  const W = 640, H = 180, padX = 10, padTop = 14, padBottom = 30;
  const h = H - padTop - padBottom;
  const n = points.length;
  const ps = points.map(p => p.weighted_price).filter(v => v != null);
  if (ps.length < 1) return `<div class="d-empty">成本数据不足</div>`;
  const min = Math.min(...ps), max = Math.max(...ps);
  const span = (max - min) || 1;
  const xAt = i => padX + (W - 2 * padX) * (n === 1 ? 0.5 : i / (n - 1));
  const yAt = v => padTop + h * (1 - (v - min) / span);
  const dots = points.map((p, i) => {
    if (p.weighted_price == null) return "";
    const cx = xAt(i).toFixed(1), cy = yAt(p.weighted_price).toFixed(1);
    return `<circle cx="${cx}" cy="${cy}" r="3.4" fill="#2b5bd7">
              <title>${p.date}  加权 ${p.weighted_price}</title>
            </circle>`;
  }).join("");
  const xlabels = points.map((p, i) => {
    if (n > 6 && i % 2 !== 0 && i !== n - 1) return "";
    return `<text x="${xAt(i).toFixed(1)}" y="${(H - 8).toFixed(1)}"
              text-anchor="middle" font-size="9" fill="#a0a8b8">${(p.date || "").slice(5)}</text>`;
  }).join("");
  return `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" class="d-chart-svg">
      ${dots}${xlabels}
    </svg>
    <div class="d-chart-foot">
      <span class="d-chart-lbl">加权成本点列</span>
      <span class="d-chart-legend">每个点 = 一次买入后的加权平均成本(不连曲线)</span>
    </div>`;
}

async function loadCostCurve() {
  const box = $("#d-cost-curve");
  if (!box) return;
  try {
    const resp = await fetch("/api/fund/" + encodeURIComponent(_detailCode) + "/cost-curve",
      { credentials: "same-origin" });
    if (resp.status === 401) { box.hidden = true; return; }  // 未登录:隐藏(A3 优雅降级)
    box.hidden = false;
    if (!resp.ok) { box.innerHTML = `<div class="d-name">分批成本</div><div class="d-empty">成本数据暂缺</div>`; return; }
    const r = await resp.json();
    const pts = (r && r.points) || [];
    if (!pts.length) { box.innerHTML = `<div class="d-name">分批成本<span class="fcode">无买入记录</span></div>
      <div class="d-empty">暂无买入流水,无法画成本曲线</div>`; return; }
    box.innerHTML = `<div class="d-name">分批买入成本曲线<span class="fcode">点状 · 不连走势</span></div>
      ${costCurveSvg(pts)}`;
  } catch {
    box.hidden = false;
    box.innerHTML = `<div class="d-name">分批成本</div><div class="d-empty">成本数据暂缺</div>`;
  }
}

// 阶段收益归因 —— 每个阶段画一批次贡献点列(零轴上下分色),不画连续走势。
// period: {batches:[{date,shares,cost_price,contribution,ratio}], total} | null
function attributionSvg(period) {
  if (!period || !period.batches || !period.batches.length) return "";
  const W = 640, H = 150, padX = 10, padTop = 14, padBottom = 26;
  const h = H - padTop - padBottom;
  const n = period.batches.length;
  const mid = padTop + h / 2;
  const vals = period.batches.map(b => b.contribution);
  const maxAbs = Math.max(1, ...vals.map(v => Math.abs(v)));
  const xAt = i => padX + (W - 2 * padX) * (n === 1 ? 0.5 : i / (n - 1));
  const dots = period.batches.map((b, i) => {
    const cx = xAt(i).toFixed(1);
    const r = Math.max(2, (Math.abs(b.contribution) / maxAbs) * (h / 2 - 2));
    // 半径表达贡献量;颜色按贡献正负(与 --up / --down 一致)
    const fill = b.contribution >= 0 ? "#e5432f" : "#0f9d58";
    return `<circle cx="${cx}" cy="${mid.toFixed(1)}" r="${r.toFixed(1)}" fill="${fill}" opacity="0.8">
              <title>${b.date}  贡献 ${b.contribution}  占比 ${(b.ratio * 100).toFixed(1)}%</title>
            </circle>`;
  }).join("");
  return `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" class="d-chart-svg">
      <line x1="0" y1="${mid.toFixed(1)}" x2="${W}" y2="${mid.toFixed(1)}" stroke="#e2e6ee" stroke-width="1"/>
      ${dots}
    </svg>`;
}

function renderAttribution(data) {
  const box = $("#d-attribution");
  if (!box) return;
  const p = data && data.periods;
  if (!p) { box.hidden = true; return; }
  const labels = { m1: "近1月", m3: "近3月", ytd: "今年以来", max: "成立以来" };
  const cards = ["m1", "m3", "ytd", "max"].map(k => {
    const per = p[k];
    if (!per) {
      return `<div class="d-attr-cell"><div class="d-attr-h">${labels[k]}<b>—</b></div>
        <div class="d-attr-empty">数据不足</div></div>`;
    }
    const total = per.total;
    const fmt = v => `<span class="${cls(v)}">${sign(v)}</span>`;
    return `<div class="d-attr-cell">
      <div class="d-attr-h">${labels[k]}<b>${fmt(total)}</b></div>
      ${attributionSvg(per)}
      <div class="d-attr-foot">${per.batches.length} 批次</div>
    </div>`;
  }).join("");
  box.innerHTML = `<div class="d-name">阶段收益归因<span class="fcode">按批次 · 点列</span></div>
    <div class="d-attr-grid">${cards}</div>
    <div class="d-chart-legend">点大小 = 贡献额,红涨绿跌,不画连续走势</div>`;
}

async function loadAttribution() {
  const box = $("#d-attribution");
  if (!box) return;
  try {
    const resp = await fetch("/api/fund/" + encodeURIComponent(_detailCode) + "/returns-attribution",
      { credentials: "same-origin" });
    if (resp.status === 401) { box.hidden = true; return; }
    box.hidden = false;
    if (!resp.ok) { box.innerHTML = `<div class="d-name">阶段收益归因</div><div class="d-empty">归因数据暂缺</div>`; return; }
    const r = await resp.json();
    renderAttribution(r);
  } catch {
    box.hidden = false;
    box.innerHTML = `<div class="d-name">阶段收益归因</div><div class="d-empty">归因数据暂缺</div>`;
  }
}

// 今日盘中实时涨幅折线 —— 纵轴 gszzl(估算涨跌幅%),零轴参考线,红涨绿跌。
// 盘中每 30s 轮询延伸;收盘停止轮询,图保留今日全天数据直到次日开盘。
async function loadIntraday() {
  const box = $("#d-intraday");
  if (!box) return;
  try {
    const d = await getJSON("/api/fund/" + encodeURIComponent(_detailCode) + "/intraday");
    renderIntradayChart(d);
  } catch {
    box.innerHTML = `<div class="d-name">今日实时涨幅</div><div class="d-empty">数据暂缺</div>`;
  }
}

function renderIntradayChart(d) {
  const box = $("#d-intraday");
  if (!box) return;
  const ticks = (d && d.ticks) || [];
  const open = !!(d && d.market_open);
  const tag = open
    ? '<span class="d-intraday-tag live">盘中实时</span>'
    : '<span class="d-intraday-tag closed">已收盘</span>';
  box.innerHTML = `<div class="d-name">今日实时涨幅 ${tag}</div><div id="d-intraday-chart"></div>`;
  renderIntradaySvg($("#d-intraday-chart"), ticks, d);
  // 盘中开轮询;收盘保持定格,不再轮询(图一直展示到次日开盘)
  if (open && !_intradayTimer) {
    _intradayTimer = setInterval(loadIntraday, 30000);
  } else if (!open && _intradayTimer) {
    clearInterval(_intradayTimer);
    _intradayTimer = null;
  }
}

// 今日盘中实时涨幅 —— 可交互折线(chart.js),零轴居中、红涨绿跌,悬停看每个时点估值。
function renderIntradaySvg(container, ticks, d) {
  if (!container) return;
  const pts = (ticks || []).filter(t => t.gszzl != null);
  if (pts.length < 1) {
    const hint = (d && d.market_open) ? "今日暂无估值点,开盘后自动更新" : "今日暂无盘中估值数据";
    container.innerHTML = `<div class="d-empty">${hint}</div>`;
    return;
  }
  const last = pts[pts.length - 1].gszzl;
  const hi = Math.max(...pts.map(t => t.gszzl));
  const lo = Math.min(...pts.map(t => t.gszzl));
  const fmtPct = v => (v >= 0 ? "+" : "") + (+v).toFixed(2) + "%";
  const points = pts.map(t => ({
    label: t.quote_time || "",
    value: t.gszzl,
    tip: `<b class="${cls(t.gszzl)}">${fmtPct(t.gszzl)}</b>`,
  }));
  renderLineChart(container, points, {
    height: 200,
    zeroLine: true,
    minSpan: 0.5,
    color: last >= 0 ? "#e5432f" : "#0f9d58",
    fmtValue: v => (v >= 0 ? "+" : "") + (+v).toFixed(1) + "%",
    fmtLabel: t => String(t || "").slice(0, 5),
    footLeft: `<span class="lbl ${cls(last)}">最新 ${sign(+last.toFixed(2))}%</span>`,
    footRight: `<span class="legend">最高 ${fmtPct(hi)} · 最低 ${fmtPct(lo)} · 零轴虚线</span>`,
    emptyHint: "估值数据不足",
  });
}

// 风险概览四宫格 —— 波动率/最大回撤/夏普/卡玛(点状,不画走势曲线)。
// 数据来自 GET /api/fund/{code}/risk(PRD-01),近1年基于复权净值(02)。
function renderRisk(r) {
  const box = $("#d-risk");
  if (!box || !r) return;
  const fmt = v => v == null ? "—" : `<b>${v}</b>`;
  const fmtPct = v => v == null ? "—" : `<b class="${cls(v)}">${v}%</b>`;
  box.innerHTML = `
    <div class="d-name">风险概览<span class="fcode">近1年 · 点状统计</span></div>
    <div class="d-grid">
      <div>年化波动率<b>${fmtPct(r.volatility)}</b></div>
      <div>最大回撤<b>${fmtPct(r.max_drawdown)}</b></div>
      <div>夏普比率<b>${fmt(r.sharpe)}</b></div>
      <div>卡玛比率<b>${fmt(r.calmar)}</b></div>
    </div>
    ${r.max_drawdown != null && r.max_drawdown_peak_date
      ? `<div class="d-chart-legend">最大回撤峰值 ${r.max_drawdown_peak_date} → 谷底 ${r.max_drawdown_trough_date || "—"}</div>` : ""}
    ${r.note ? `<div class="d-chart-legend">${r.note}</div>` : ""}`;
}

async function loadRisk() {
  const box = $("#d-risk");
  if (!box) return;
  try {
    const r = await getJSON("/api/fund/" + encodeURIComponent(_detailCode) + "/risk");
    renderRisk(r);
  } catch {
    box.innerHTML = `<div class="d-name">风险概览</div><div class="d-empty">风险数据暂缺</div>`;
  }
}

registerPage("fund", renderDetail);
