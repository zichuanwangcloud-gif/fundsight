// ============================================================================
// 盈见 FundSight —— 大盘指数条(P1a,零依赖)
// 拉 /api/market/indices,在市场页/持仓页顶部渲染 4 大指数(红涨绿跌)。
// 盘中每 60s 刷新;接口空态(抓取未就绪/沙箱受限)优雅降级——不渲染、不报错。
// cls / sign 来自 app.js(全局)。
// ============================================================================

let _idxTimer = null;

async function renderIndexBar(containerId) {
  const box = document.getElementById(containerId);
  if (!box) return;
  try {
    const r = await fetch("/api/market/indices", { credentials: "same-origin" });
    if (!r.ok) { box.innerHTML = ""; return; }
    const data = await r.json();
    const items = (data && data.indices) || [];
    if (!items.length) { box.innerHTML = ""; return; }  // 空态:不占位
    box.innerHTML = `<div class="idx-strip">${items.map(idxCell).join("")}</div>`;
  } catch {
    box.innerHTML = "";
  }
}

function idxCell(it) {
  const pct = it.change_pct;
  const c = cls(pct);
  const price = it.price != null ? (+it.price).toFixed(2) : "—";
  const chg = it.change != null ? sign(+(+it.change).toFixed(2)) : "—";
  const pctTxt = pct != null ? sign(+(+pct).toFixed(2)) + "%" : "—";
  return `<div class="idx-cell">
      <div class="idx-name">${it.name || it.code || ""}</div>
      <div class="idx-price ${c}">${price}</div>
      <div class="idx-chg ${c}">${chg} · ${pctTxt}</div>
    </div>`;
}

// 盘中每 60s 刷新指数条;切页时调用 stopIndexBar 清理 timer。
function startIndexBar(containerId) {
  renderIndexBar(containerId);
  stopIndexBar();
  _idxTimer = setInterval(() => renderIndexBar(containerId), 60000);
}
function stopIndexBar() {
  if (_idxTimer) { clearInterval(_idxTimer); _idxTimer = null; }
}
