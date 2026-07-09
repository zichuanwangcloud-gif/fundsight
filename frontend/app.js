// 通用工具 + 极简 hash 路由 + 顶部 Tab —— 零依赖。
// 页面注册约定(供各线路复用):
//   registerPage("market", (view, param) => { ... })   在自己的 *.js 里注册
//   路由 #/market → PAGES["market"](view);  #/fund/020608 → PAGES["fund"](view, "020608")

const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

// 涨跌着色 / 带符号 —— 各页共用
function cls(v) { return v > 0 ? "up" : v < 0 ? "down" : "flat"; }
function scls(v) { return v > 0 ? "s-up" : v < 0 ? "s-down" : "s-flat"; }
function sign(v) { return (v > 0 ? "+" : "") + v; }

// 取 JSON 的小封装(带会话 Cookie)
async function getJSON(url) {
  const r = await fetch(url, { credentials: "same-origin" });
  return r.json();
}

// ---- 页面注册与路由 ----
const PAGES = {};
function registerPage(key, renderFn) { PAGES[key] = renderFn; }

function currentRoute() {
  // #/market | #/portfolio | #/fund/020608
  const raw = (location.hash || "#/portfolio").replace(/^#\//, "");
  const [key, ...rest] = raw.split("/");
  return { key: key || "portfolio", param: rest.join("/") };
}

function renderRoute() {
  const { key, param } = currentRoute();
  const view = $("#view");
  // Tab 高亮:详情 #/fund/xxx 归属「市场」Tab
  const tabKey = key === "fund" ? "market" : key;
  $$(".tabs .tab").forEach(t => t.classList.toggle("active", t.dataset.key === tabKey));
  const fn = PAGES[key];
  if (fn) fn(view, param);
  else view.innerHTML = `<div class="placeholder">该模块建设中 🚧</div>`;
}

window.addEventListener("hashchange", renderRoute);

// 首屏渲染由 auth.js 在确认登录态后调用(未登录先显示门控)。
function startApp() {
  if (!location.hash) location.hash = "#/portfolio";  // 默认「我的持仓」
  renderRoute();
}
