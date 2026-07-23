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

// ---- 全站统一反馈组件(零依赖):toast 轻提示 + confirm 确认弹窗 + overlay 壳 ----
// 消费 theme token,替代割裂的原生 alert()/confirm() 与各页自建弹层。
let _toastTimer = null;
function toast(msg) {
  let el = document.getElementById("app-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "app-toast";
    el.className = "app-toast";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.classList.remove("show"); }, 3000);
}

// 通用模态壳:返回 { overlay, box, close }。自带 Esc + 遮罩点击关闭。
function openOverlay(innerHTML, { onClose } = {}) {
  const overlay = document.createElement("div");
  overlay.className = "app-overlay";
  const box = document.createElement("div");
  box.className = "app-overlay-box";
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.innerHTML = innerHTML;
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  const close = () => {
    if (!overlay.isConnected) return;
    document.removeEventListener("keydown", onKey);
    overlay.remove();
    if (onClose) onClose();
  };
  const onKey = e => { if (e.key === "Escape") close(); };
  overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", onKey);
  return { overlay, box, close };
}

// 品牌内确认弹窗,返回 Promise<boolean>(替代原生 confirm)。
function confirmDialog(message, { okText = "确认", cancelText = "取消", danger = false } = {}) {
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (done) return; done = true; ov.close(); resolve(v); };
    const ov = openOverlay(`
      <div class="app-confirm-msg">${message}</div>
      <div class="app-overlay-btns">
        <button type="button" class="ghost" data-act="cancel">${cancelText}</button>
        <button type="button" class="${danger ? "danger" : "primary"}" data-act="ok">${okText}</button>
      </div>`, { onClose: () => finish(false) });
    ov.box.querySelector('[data-act="ok"]').onclick = () => finish(true);
    ov.box.querySelector('[data-act="cancel"]').onclick = () => finish(false);
    ov.box.querySelector('[data-act="ok"]').focus();
  });
}

// 键盘可达:为 role="button" 且可聚焦的自定义控件补 Enter/Space 激活(WCAG 2.1.1)。
document.addEventListener("keydown", e => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const t = e.target;
  if (t && t.getAttribute && t.getAttribute("role") === "button" && t.hasAttribute("tabindex")) {
    e.preventDefault();
    t.click();
  }
});

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

// ---- 站内通知轮询 + 浮窗(M9-D) ----
let _notifTimer = null;

async function _refreshNotifBadge() {
  const badge = $("#notif-badge");
  if (!badge) return;
  try {
    const r = await fetch("/api/notifications", { credentials: "same-origin" });
    if (r.status === 401) { stopNotifPoller(); return; }
    const data = await r.json();
    const n = (data.notifications || []).length;
    badge.style.display = n > 0 ? "" : "none";
    const c = $("#notif-cnt"); if (c) c.textContent = n;
  } catch (e) { /* 静默:网络抖动不打扰用户 */ }
}
function startNotifPoller() {
  _refreshNotifBadge();
  if (_notifTimer) return;
  _notifTimer = setInterval(_refreshNotifBadge, 60000);  // 每分钟轮询
}
function stopNotifPoller() {
  if (_notifTimer) { clearInterval(_notifTimer); _notifTimer = null; }
  const badge = $("#notif-badge"); if (badge) badge.style.display = "none";
  const pop = $("#notif-pop"); if (pop) pop.style.display = "none";
}
async function _markNotifRead(id) {
  await fetch(`/api/notifications/${id}/read`, { method: "POST", credentials: "same-origin" });
  _refreshNotifBadge();
  showNotifs();
}
async function showNotifs() {
  const pop = $("#notif-pop");
  if (!pop) return;
  if (pop.style.display !== "none") { pop.style.display = "none"; return; }  // 再点收起
  pop.innerHTML = `<div class="empty">加载中…</div>`;
  pop.style.display = "";
  try {
    const r = await fetch("/api/notifications", { credentials: "same-origin" });
    if (r.status === 401) { pop.style.display = "none"; return showAuth(); }
    const data = await r.json();
    const items = data.notifications || [];
    if (!items.length) { pop.innerHTML = `<div class="empty">暂无未读通知</div>`; return; }
    pop.innerHTML = items.map(it => `
      <div class="item">
        <button onclick="_markNotifRead(${it.id})">已读</button>
        <div class="msg">${it.message || ""}</div>
        <div class="meta">${it.kind || ""} · ${it.created_at || ""}</div>
      </div>`).join("");
  } catch (e) {
    pop.innerHTML = `<div class="empty">加载失败</div>`;
  }
}
