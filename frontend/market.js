// 「基金市场」页 —— 分类 Tab + 搜索 + 分页列表 + 加自选。
// 由 app.js 的路由在 #/market 时调用 registerPage 的渲染函数。
// cls / scls / sign / $ / $$ / getJSON 来自 app.js(全局)。

let _mktTimer = null;
let _mktState = { cat: "", q: "", page: 1, size: 20, items: [], total: 0 };
let _mktHeldCodes = null; // 已加自选的 fund_code 集合;拉不到就保持 null(不打标)

function renderMarket(view) {
  _mktState = { cat: "", q: "", page: 1, size: 20, items: [], total: 0 };
  view.innerHTML = `
    <div class="mkt-search">
      <input id="mkt-q" placeholder="搜索基金:代码 / 名称 / 拼音" autocomplete="off">
    </div>
    <div id="mkt-tabs" class="mkt-tabs"></div>
    <div id="mkt-list" class="mkt-list"></div>
    <div id="mkt-more" class="mkt-more"></div>
  `;

  const q = $("#mkt-q");
  q.addEventListener("input", () => {
    clearTimeout(_mktTimer);
    _mktTimer = setTimeout(() => {
      _mktState.q = q.value.trim();
      _mktState.page = 1;
      loadList(true);
    }, 250);
  });

  loadHeldCodes();
  loadCategories();
  loadList(true);
}

async function loadHeldCodes() {
  // 已在自选的行打标;端点由线路 D 提供,可能还不存在 —— 优雅降级,失败就不打标。
  try {
    const r = await fetch("/api/holdings/codes", { credentials: "same-origin" });
    if (!r.ok) { _mktHeldCodes = null; return; }
    const d = await r.json();
    _mktHeldCodes = new Set(Array.isArray(d) ? d : (d.codes || []));
    renderList(); // codes 到得晚时补一次打标
  } catch {
    _mktHeldCodes = null;
  }
}

async function loadCategories() {
  try {
    const cats = await getJSON("/api/categories");
    const tabs = $("#mkt-tabs");
    if (!tabs) return;
    const all = [{ cat: "", count: null, label: "全部" }, ...cats];
    tabs.innerHTML = all.map(c => {
      const label = c.label || c.cat;
      const count = c.count != null ? `<span class="mkt-tab-n">${c.count}</span>` : "";
      const active = _mktState.cat === c.cat ? " active" : "";
      return `<div class="mkt-tab${active}" data-cat="${c.cat}">${label}${count}</div>`;
    }).join("");
    tabs.querySelectorAll(".mkt-tab").forEach(el => {
      el.addEventListener("click", () => {
        _mktState.cat = el.dataset.cat;
        _mktState.page = 1;
        tabs.querySelectorAll(".mkt-tab").forEach(t => t.classList.toggle("active", t === el));
        loadList(true);
      });
    });
  } catch {
    const tabs = $("#mkt-tabs");
    if (tabs) tabs.innerHTML = "";
  }
}

async function loadList(reset) {
  const list = $("#mkt-list");
  if (reset) list.innerHTML = `<div class="mkt-loading">加载中…</div>`;
  const params = new URLSearchParams({
    page: String(_mktState.page), size: String(_mktState.size),
  });
  if (_mktState.cat) params.set("cat", _mktState.cat);
  if (_mktState.q) params.set("q", _mktState.q);
  const r = await fetch("/api/market?" + params.toString(), { credentials: "same-origin" });
  if (r.status === 401) return showAuth();
  const data = await r.json();
  if (reset) {
    _mktState.items = data.items || [];
  } else {
    _mktState.items = _mktState.items.concat(data.items || []);
  }
  _mktState.total = data.total || 0;
  renderList();
  renderMore();
}

function renderList() {
  const list = $("#mkt-list");
  if (!list) return;
  const items = _mktState.items;
  if (!items.length) { list.innerHTML = `<div class="empty">没有匹配的基金</div>`; return; }
  list.innerHTML = items.map(it => {
    const held = _mktHeldCodes && _mktHeldCodes.has(it.fund_code);
    return `<div class="mkt-row">
      <div class="mkt-row-main">
        <span class="mkt-name">${it.name}</span>
        <span class="mkt-code">${it.fund_code}</span>
        <span class="mkt-type">${it.fund_type || ""}</span>
      </div>
      <a class="mkt-detail" href="#/fund/${it.fund_code}">详情 →</a>
      ${held
        ? `<span class="mkt-held">已自选 ✓</span>`
        : `<button class="mkt-add" data-code="${it.fund_code}">加自选</button>`}
    </div>`;
  }).join("");
  list.querySelectorAll(".mkt-add").forEach(btn => {
    btn.addEventListener("click", () => addToHoldings(btn.dataset.code, btn));
  });
}

function renderMore() {
  const more = $("#mkt-more");
  if (!more) return;
  const loaded = _mktState.items.length;
  if (loaded >= _mktState.total) { more.innerHTML = ""; return; }
  more.innerHTML = `<button id="mkt-more-btn">加载更多(${loaded}/${_mktState.total})</button>`;
  const btn = $("#mkt-more-btn");
  if (btn) btn.addEventListener("click", () => {
    _mktState.page += 1;
    loadList(false);
  });
}

async function addToHoldings(code, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "添加中…"; }
  try {
    const r = await fetch("/api/holdings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      credentials: "same-origin", body: JSON.stringify({ fund_code: code }),
    });
    if (r.status === 401) return showAuth();
    if (!r.ok) throw new Error("add failed");
    if (_mktHeldCodes) _mktHeldCodes.add(code);
    renderList();
  } catch {
    if (btn) { btn.disabled = false; btn.textContent = "加自选"; }
  }
}

registerPage("market", renderMarket);
