// 登录 / 注册门控 + 应用启动。整合自用户体系(PR #9)。
// 启动时探测 /api/me:未登录显示门控,已登录隐藏门控并 startApp()(app.js)。

let authMode = "login"; // login | register

function showAuth() {
  const ub = $("#userbar"); if (ub) ub.style.display = "none";
  $("#auth").style.display = "flex";
  $("#auth-err").textContent = "";
  $("#a-pass").value = "";
}
function hideAuth(username) {
  $("#auth").style.display = "none";
  const ub = $("#userbar"); if (ub) ub.style.display = "flex";
  $("#me-name").textContent = username;
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
