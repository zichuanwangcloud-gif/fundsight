// ============================================================================
// 盈见 FundSight —— 基金排行榜页(P1b,零依赖)
// 类目胶囊 + 区间胶囊 + 榜单列表(名次/名称/多区间收益,行点进详情)。
// 只读 /api/rank;榜单未就绪(抓取中/沙箱受限)优雅降级提示。
// cls / sign / $ / getJSON / registerPage 来自 app.js(全局)。
// ============================================================================

let _rankState = { cat: "all", period: "1y" };
let _rankMeta = null;

// 兜底类目/区间(meta 拉不到时用),与后端 fund_rank.CATEGORIES/PERIODS 对齐
const _RANK_CATS_FALLBACK = [
  { key: "all", label: "全部" }, { key: "gp", label: "股票" },
  { key: "hh", label: "混合" }, { key: "zs", label: "指数" },
  { key: "zq", label: "债券" }, { key: "qdii", label: "QDII" },
];
const _RANK_PERIODS_FALLBACK = [
  { key: "1m", label: "近1月" }, { key: "3m", label: "近3月" },
  { key: "6m", label: "近6月" }, { key: "1y", label: "近1年" },
  { key: "ytd", label: "今年来" },
];
// 区间 key → 收益字段名
const _PERIOD_FIELD = { "1m": "r_1m", "3m": "r_3m", "6m": "r_6m", "1y": "r_1y", "ytd": "r_ytd" };

async function renderRank(view) {
  _rankState = { cat: "all", period: "1y" };
  view.innerHTML = `
    <div class="rank-head">
      <div class="section-title">基金排行榜 <span class="sub" id="rank-updated"></span></div>
      <div id="rank-cats" class="rank-pills"></div>
      <div id="rank-periods" class="rank-pills rank-periods"></div>
    </div>
    <div id="rank-list" class="rank-list"></div>`;

  if (!_rankMeta) {
    try { _rankMeta = await getJSON("/api/rank/meta"); }
    catch { _rankMeta = { categories: _RANK_CATS_FALLBACK, periods: _RANK_PERIODS_FALLBACK }; }
  }
  if (!_rankMeta.categories || !_rankMeta.categories.length) _rankMeta.categories = _RANK_CATS_FALLBACK;
  if (!_rankMeta.periods || !_rankMeta.periods.length) _rankMeta.periods = _RANK_PERIODS_FALLBACK;

  renderRankPills();
  loadRank();
}

function renderRankPills() {
  const cats = $("#rank-cats"), pers = $("#rank-periods");
  if (cats) {
    cats.innerHTML = _rankMeta.categories.map(c =>
      `<span class="pill${c.key === _rankState.cat ? " active" : ""}" data-cat="${c.key}">${c.label}</span>`).join("");
    cats.querySelectorAll(".pill").forEach(el => el.addEventListener("click", () => {
      _rankState.cat = el.dataset.cat; renderRankPills(); loadRank();
    }));
  }
  if (pers) {
    pers.innerHTML = _rankMeta.periods.map(p =>
      `<span class="pill${p.key === _rankState.period ? " active" : ""}" data-period="${p.key}">${p.label}</span>`).join("");
    pers.querySelectorAll(".pill").forEach(el => el.addEventListener("click", () => {
      _rankState.period = el.dataset.period; renderRankPills(); loadRank();
    }));
  }
}

async function loadRank() {
  const list = $("#rank-list");
  if (!list) return;
  list.innerHTML = `<div class="d-loading">榜单加载中…</div>`;
  let data;
  try {
    data = await getJSON(`/api/rank?cat=${encodeURIComponent(_rankState.cat)}&period=${encodeURIComponent(_rankState.period)}`);
  } catch {
    list.innerHTML = `<div class="empty">榜单暂不可用</div>`;
    return;
  }
  const items = (data && data.items) || [];
  const upd = $("#rank-updated");
  if (upd) upd.textContent = data.updated_at ? `更新于 ${data.updated_at}` : "";
  if (!items.length) {
    list.innerHTML = `<div class="empty">榜单生成中,请稍后再来 👀<br><span class="rank-empty-sub">(后台每日刷新一次)</span></div>`;
    return;
  }
  const pf = _PERIOD_FIELD[_rankState.period] || "r_1y";
  list.innerHTML = `
    <div class="rank-row rank-th">
      <span class="rk-no">#</span>
      <span class="rk-name">基金</span>
      <span class="rk-ret">本区间</span>
      <span class="rk-nav">净值</span>
    </div>` + items.map(it => rankRow(it, pf)).join("");
  list.querySelectorAll(".rank-row[data-code]").forEach(el => {
    el.addEventListener("click", () => { location.hash = "#/fund/" + el.dataset.code; });
  });
}

function rankRow(it, pf) {
  const ret = it[pf];
  const medal = it.rank <= 3 ? ` rk-top` : "";
  return `<div class="rank-row" data-code="${it.fund_code}" role="button" tabindex="0"
       aria-label="查看 ${(it.name || it.fund_code)} 详情">
      <span class="rk-no${medal}">${it.rank}</span>
      <span class="rk-name"><b>${it.name || it.fund_code}</b><i>${it.fund_code}</i></span>
      <span class="rk-ret ${cls(ret)}">${ret == null ? "—" : sign(+(+ret).toFixed(2)) + "%"}</span>
      <span class="rk-nav">${it.nav != null ? (+it.nav).toFixed(4) : "—"}</span>
    </div>`;
}

registerPage("rank", renderRank);
