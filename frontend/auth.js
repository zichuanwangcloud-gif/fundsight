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

let _cpwClose = null;
function openChangePwDialog() {
  // 复用 app.js 的通用模态壳:token 样式 + Esc/遮罩关闭,零内联硬编码。
  const { box, close } = openOverlay(`
    <h3>修改密码</h3>
    <input id="cpw-old" type="password" placeholder="当前密码" autocomplete="current-password">
    <input id="cpw-new" type="password" placeholder="新密码" autocomplete="new-password">
    <input id="cpw-new2" type="password" placeholder="确认新密码" autocomplete="new-password">
    <div id="cpw-err" class="err"></div>
    <div class="app-overlay-btns">
      <button id="cpw-cancel" type="button" class="ghost">取消</button>
      <button id="cpw-submit" type="button" class="primary">确认修改</button>
    </div>`, { onClose: () => { _cpwClose = null; } });
  _cpwClose = close;
  box.querySelector("#cpw-cancel").onclick = close;
  box.querySelector("#cpw-submit").onclick = submitChangePw;
  box.querySelector("#cpw-new2").addEventListener("keydown", e => { if (e.key === "Enter") submitChangePw(); });
  box.querySelector("#cpw-old").focus();
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
  if (_cpwClose) _cpwClose();
  toast("密码已修改,已退出其他设备");
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
