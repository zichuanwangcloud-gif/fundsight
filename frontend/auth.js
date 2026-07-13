// 登录 / 注册门控 + 应用启动。整合自用户体系(PR #9)。
// 启动时探测 /api/me:未登录显示门控,已登录隐藏门控并 startApp()(app.js)。

let authMode = "login"; // login | register

function showAuth() {
  stopNotifPoller();
  const ub = $("#userbar"); if (ub) ub.style.display = "none";
  $("#auth").style.display = "flex";
  $("#auth-err").textContent = "";
  $("#a-pass").value = "";
}
function hideAuth(username) {
  $("#auth").style.display = "none";
  const ub = $("#userbar"); if (ub) ub.style.display = "flex";
  $("#me-name").textContent = username;
  ensureChangePwButton();
  startNotifPoller();
}
function toggleAuthMode() {
  authMode = authMode === "login" ? "register" : "login";
  const login = authMode === "login";
  $("#auth-submit").textContent = login ? "登录" : "注册";
  $("#auth-switch-text").textContent = login ? "还没有账号？" : "已有账号？";
  $("#auth-switch").textContent = login ? "注册" : "登录";
  $("#a-pass").setAttribute("autocomplete", login ? "current-password" : "new-password");
  $("#auth-err").textContent = "";
}
async function submitAuth() {
  const username = $("#a-user").value.trim();
  const password = $("#a-pass").value;
  if (!username || !password) { $("#auth-err").textContent = "请输入用户名和密码"; return; }
  const url = authMode === "login" ? "/api/login" : "/api/register";
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body: JSON.stringify({ username, password }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    $("#auth-err").textContent = d.error || "操作失败";
    return;
  }
  const d = await r.json();
  hideAuth(d.username);
  startApp();
}
async function logout() {
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  showAuth();
}

// ---- 修改密码(M10B-B2):改密后该用户所有存量 session 失效 ----
let _changePwBtnInjected = false;
function ensureChangePwButton() {
  if (_changePwBtnInjected) return;
  const ub = $("#userbar");
  if (!ub) return;
  const btn = document.createElement("button");
  btn.textContent = "修改密码";
  btn.type = "button";
  btn.onclick = openChangePwDialog;
  // 插在「退出」按钮之前
  const logoutBtn = ub.querySelector("button[onclick*='logout']");
  ub.insertBefore(btn, logoutBtn || null);
  _changePwBtnInjected = true;
}

function openChangePwDialog() {
  // 极简弹窗,零依赖、不依赖 index.html 的额外结构
  const overlay = document.createElement("div");
  overlay.id = "changepw-overlay";
  overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:1000";
  const box = document.createElement("div");
  box.style.cssText = "background:#fff;padding:20px;border-radius:10px;width:320px;max-width:92vw;box-shadow:0 8px 30px rgba(0,0,0,.3)";
  box.innerHTML = `
    <h3 style="margin:0 0 12px;font-size:16px">修改密码</h3>
    <input id="cpw-old" type="password" placeholder="当前密码" autocomplete="current-password"
      style="width:100%;box-sizing:border-box;margin:6px 0;padding:8px;border:1px solid #ccc;border-radius:6px">
    <input id="cpw-new" type="password" placeholder="新密码" autocomplete="new-password"
      style="width:100%;box-sizing:border-box;margin:6px 0;padding:8px;border:1px solid #ccc;border-radius:6px">
    <input id="cpw-new2" type="password" placeholder="确认新密码" autocomplete="new-password"
      style="width:100%;box-sizing:border-box;margin:6px 0;padding:8px;border:1px solid #ccc;border-radius:6px">
    <div id="cpw-err" style="color:#c0392b;font-size:13px;min-height:18px"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:8px">
      <button id="cpw-cancel" type="button">取消</button>
      <button id="cpw-submit" type="button" style="font-weight:600">确认修改</button>
    </div>`;
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
  $("#cpw-cancel").onclick = close;
  $("#cpw-submit").onclick = submitChangePw;
  $("#cpw-new2").addEventListener("keydown", e => { if (e.key === "Enter") submitChangePw(); });
  $("#cpw-old").focus();
}

async function submitChangePw() {
  const oldPw = $("#cpw-old").value;
  const newPw = $("#cpw-new").value;
  const newPw2 = $("#cpw-new2").value;
  const err = $("#cpw-err");
  err.textContent = "";
  if (!oldPw || !newPw) { err.textContent = "请填写当前密码和新密码"; return; }
  if (newPw !== newPw2) { err.textContent = "两次新密码不一致"; return; }
  if (newPw.length < 6) { err.textContent = "新密码至少 6 位"; return; }
  const r = await fetch("/api/change-password", {
    method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { err.textContent = d.error || "修改失败"; return; }
  // 改密成功:已让该用户所有其他设备 session 失效,当前设备拿到新会话保持登录。
  document.getElementById("changepw-overlay").remove();
  toast("密码已修改,已退出其他设备");
}

let _toastTimer = null;
function toast(msg) {
  let el = document.getElementById("app-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "app-toast";
    el.style.cssText = "position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:#333;color:#fff;padding:10px 18px;border-radius:8px;z-index:1100;box-shadow:0 4px 14px rgba(0,0,0,.3)";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.display = "block";
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.style.display = "none"; }, 3000);
}

// 启动：探测登录态,决定显示门控还是进入应用
(async function init() {
  const pass = $("#a-pass");
  if (pass) pass.addEventListener("keydown", e => { if (e.key === "Enter") submitAuth(); });
  const r = await fetch("/api/me", { credentials: "same-origin" });
  if (r.status === 401) return showAuth();
  const d = await r.json();
  hideAuth(d.username);
  startApp();
})();
