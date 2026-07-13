// 「系统状态」页 —— 抓取任务可观测面板(M9-B)。
// 只读 /api/admin/sync-status(各任务最近一次概览) + /api/admin/sync-runs(流水)。
// cls / scls / $ / getJSON / showAuth 来自 app.js / auth.js(全局)。

// 后台任务名 → 中文标签
const _TASK_LABELS = {
  fund_list_sync: "全量列表同步",
  nav_refresh: "收盘净值回填",
  quote_refresh: "盘中估值刷新",
  history_refresh: "历史净值刷新",
  profile_refresh: "基本面刷新",
  quote_one: "单只估值补拉",
  history_one: "单只历史补拉",
};
function _taskLabel(name) { return _TASK_LABELS[name] || name; }

function _statusBadge(status) {
  if (status === "ok") return `<span class="badge ok">成功</span>`;
  return `<span class="badge fail">失败</span>`;
}
function _cell(v, suffix = "") {
  return v == null ? "—" : v + suffix;
}

async function renderAdmin(view) {
  view.innerHTML = `
    <div class="admin-head">
      <h2>抓取任务状态</h2>
      <button class="ghost" onclick="renderAdmin(document.getElementById('view'))">刷新</button>
    </div>
    <p class="admin-hint">后台各抓取任务的执行结果(只读)。失败行点「错误」列查原因。</p>
    <h3 class="admin-sec">各任务最近一次</h3>
    <div id="admin-summary"></div>
    <h3 class="admin-sec">最近执行流水 <span class="admin-muted">(最近 50 条)</span></h3>
    <div id="admin-runs"></div>`;
  await Promise.all([_loadSummary(), _loadRuns()]);
}

async function _loadSummary() {
  const box = $("#admin-summary");
  try {
    const resp = await fetch("/api/admin/sync-status", { credentials: "same-origin" });
    if (resp.status === 401) return showAuth();
    const data = await resp.json();
    if (!data.tasks || !data.tasks.length) {
      box.innerHTML = `<p class="admin-muted">暂无任务执行记录(服务刚启动或未跑过抓取)</p>`;
      return;
    }
    box.innerHTML = `
      <table class="admin-tbl">
        <thead><tr><th>任务</th><th>状态</th><th>条数</th><th>耗时</th><th>最近执行</th><th>错误</th></tr></thead>
        <tbody>${data.tasks.map(t => `
          <tr>
            <td>${_taskLabel(t.task_name)}</td>
            <td>${_statusBadge(t.status)}</td>
            <td>${_cell(t.affected)}</td>
            <td>${_cell(t.duration_ms, "ms")}</td>
            <td>${t.started_at || "—"}</td>
            <td class="admin-err">${t.error || ""}</td>
          </tr>`).join("")}</tbody>
      </table>`;
  } catch (e) {
    box.innerHTML = `<p class="admin-err">加载失败: ${e}</p>`;
  }
}

async function _loadRuns() {
  const box = $("#admin-runs");
  try {
    const resp = await fetch("/api/admin/sync-runs?limit=50", { credentials: "same-origin" });
    if (resp.status === 401) return showAuth();
    const data = await resp.json();
    if (!data.runs || !data.runs.length) {
      box.innerHTML = `<p class="admin-muted">暂无流水</p>`;
      return;
    }
    box.innerHTML = `
      <table class="admin-tbl">
        <thead><tr><th>时间</th><th>任务</th><th>状态</th><th>条数</th><th>耗时</th><th>错误</th></tr></thead>
        <tbody>${data.runs.map(run => `
          <tr>
            <td>${run.started_at || "—"}</td>
            <td>${_taskLabel(run.task_name)}</td>
            <td>${_statusBadge(run.status)}</td>
            <td>${_cell(run.affected)}</td>
            <td>${_cell(run.duration_ms, "ms")}</td>
            <td class="admin-err">${run.error || ""}</td>
          </tr>`).join("")}</tbody>
      </table>`;
  } catch (e) {
    box.innerHTML = `<p class="admin-err">加载失败: ${e}</p>`;
  }
}

registerPage("admin", renderAdmin);
