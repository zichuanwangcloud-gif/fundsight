// ============================================================================
// 盈见 FundSight —— 可交互折线图(P0 皮肤,零依赖)
// 解决"图表太原始不能交互":Y 轴刻度 + 网格线 + X 轴日期 + 十字准星 +
// 跟随 tooltip + 渐变面积填充。供 detail.js 净值图 / 盘中图复用。
//
//   renderLineChart(container, points, opts)
//     container : 目标 DOM(会被写入 .chart-wrap 结构)
//     points    : [{ label, value, tip }]   label=X轴文案 value=Y值 tip=悬停附加HTML
//     opts      : { height, color, zeroLine, minSpan, fmtValue, fmtLabel,
//                   footLeft, footRight, emptyHint, area }
// ============================================================================

(function () {
  const UP = "#e5432f", DOWN = "#0f9d58", GRID = "#eef1f6", AXIS = "#9aa2b3";
  let _uid = 0;

  function renderLineChart(container, points, opts) {
    if (!container) return;
    opts = opts || {};
    const pts = (points || []).filter(p => p && p.value != null);
    if (pts.length < 2) {
      container.innerHTML = `<div class="d-empty">${opts.emptyHint || "数据点不足,暂无法画图"}</div>`;
      return;
    }

    const id = "cch" + (++_uid);
    const W = 700, H = opts.height || 220;
    const padL = 42, padR = 12, padT = 12, padB = 24;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const n = pts.length;
    const vals = pts.map(p => p.value);

    let min = Math.min(...vals), max = Math.max(...vals);
    if (opts.zeroLine) {                       // 盘中涨跌幅:零轴居中、上下对称
      const m = Math.max(Math.abs(min), Math.abs(max), opts.minSpan || 0.5);
      min = -m; max = m;
    } else {                                   // 净值:上下留 6% 呼吸位
      const pad = (max - min) * 0.06 || Math.abs(max) * 0.02 || 1;
      min -= pad; max += pad;
    }
    if (min === max) { min -= 1; max += 1; }
    const span = max - min;

    const xAt = i => padL + plotW * (n === 1 ? 0.5 : i / (n - 1));
    const yAt = v => padT + plotH * (1 - (v - min) / span);
    const fmtV = opts.fmtValue || (v => (+v).toFixed(2));
    const fmtL = opts.fmtLabel || (l => String(l || ""));

    const color = opts.color || (vals[n - 1] >= vals[0] ? UP : DOWN);

    // —— 水平网格线 + Y 轴刻度 ——
    const TICKS = 4;
    let grid = "", ylabels = "";
    for (let t = 0; t <= TICKS; t++) {
      const v = min + span * t / TICKS;
      const y = yAt(v).toFixed(1);
      grid += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="${GRID}" stroke-width="1"/>`;
      ylabels += `<text x="${padL - 6}" y="${(+y + 3).toFixed(1)}" text-anchor="end" font-size="10" fill="${AXIS}">${fmtV(v)}</text>`;
    }
    // —— 盘中零轴(虚线,强调) ——
    let zero = "";
    if (opts.zeroLine) {
      const zy = yAt(0).toFixed(1);
      zero = `<line x1="${padL}" y1="${zy}" x2="${W - padR}" y2="${zy}" stroke="#c7cdda" stroke-width="1" stroke-dasharray="4 3"/>`;
    }

    // —— X 轴日期标签(首/中/末) ——
    const xIdx = n <= 2 ? [0, n - 1] : [0, Math.floor((n - 1) / 2), n - 1];
    const xlabels = xIdx.map(i => {
      const anchor = i === 0 ? "start" : i === n - 1 ? "end" : "middle";
      return `<text x="${xAt(i).toFixed(1)}" y="${H - 6}" text-anchor="${anchor}" font-size="10" fill="${AXIS}">${fmtL(pts[i].label)}</text>`;
    }).join("");

    // —— 折线 + 渐变面积 ——
    const linePts = pts.map((p, i) => `${xAt(i).toFixed(1)},${yAt(p.value).toFixed(1)}`).join(" ");
    const area = opts.area === false ? "" : (() => {
      const base = yAt(opts.zeroLine ? 0 : min).toFixed(1);
      return `<polygon points="${xAt(0).toFixed(1)},${base} ${linePts} ${xAt(n - 1).toFixed(1)},${base}"
                fill="url(#${id}-g)" opacity="0.9"/>`;
    })();

    // —— 十字准星 + 焦点圆(初始隐藏,pointer 时更新) ——
    const cross = `<g id="${id}-cross" style="display:none">
        <line id="${id}-vline" x1="0" y1="${padT}" x2="0" y2="${padT + plotH}" stroke="${AXIS}" stroke-width="1" stroke-dasharray="3 3"/>
        <circle id="${id}-dot" r="4" fill="#fff" stroke="${color}" stroke-width="2.5"/>
      </g>`;

    // 末点常驻圆点
    const lastDot = `<circle cx="${xAt(n - 1).toFixed(1)}" cy="${yAt(vals[n - 1]).toFixed(1)}" r="3.4" fill="${color}"/>`;

    const first = vals[0], last = vals[n - 1];
    const chg = opts.zeroLine ? last : (first ? ((last - first) / Math.abs(first)) * 100 : 0);
    const chgCls = chg > 0 ? "up" : chg < 0 ? "down" : "flat";
    const footLeft = opts.footLeft != null ? opts.footLeft
      : `<span class="lbl ${chgCls}">区间 ${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span>`;
    const footRight = opts.footRight != null ? opts.footRight
      : `<span class="legend">悬停查看每日净值</span>`;

    container.innerHTML = `
      <div class="chart-wrap" id="${id}-wrap">
        <svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
             class="chart-svg" id="${id}-svg" style="display:block;overflow:visible">
          <defs>
            <linearGradient id="${id}-g" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stop-color="${color}" stop-opacity="0.20"/>
              <stop offset="1" stop-color="${color}" stop-opacity="0"/>
            </linearGradient>
          </defs>
          ${grid}${zero}${area}
          <polyline fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"
                    stroke-linecap="round" points="${linePts}"/>
          ${lastDot}${cross}${ylabels}${xlabels}
        </svg>
        <div class="chart-tip" id="${id}-tip"></div>
      </div>
      <div class="chart-foot">${footLeft}${footRight}</div>`;

    // —— 交互:pointermove 定位最近点 ——
    const wrap = document.getElementById(id + "-wrap");
    const svg = document.getElementById(id + "-svg");
    const tip = document.getElementById(id + "-tip");
    const g = document.getElementById(id + "-cross");
    const vline = document.getElementById(id + "-vline");
    const dot = document.getElementById(id + "-dot");
    if (!wrap || !svg) return;

    function onMove(ev) {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const cx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left;
      let i = Math.round((cx / rect.width) * (n - 1));
      i = Math.max(0, Math.min(n - 1, i));
      const p = pts[i];
      const vx = xAt(i), vy = yAt(p.value);
      vline.setAttribute("x1", vx); vline.setAttribute("x2", vx);
      dot.setAttribute("cx", vx); dot.setAttribute("cy", vy);
      g.style.display = "";
      // tooltip 像素定位(viewBox → 实际像素)
      const pxX = (vx / W) * rect.width;
      const pxY = (vy / H) * rect.height;
      tip.style.left = pxX + "px";
      tip.style.top = pxY + "px";
      tip.style.display = "block";
      tip.innerHTML =
        `<div class="t-date">${fmtL(p.label)}</div>` +
        (p.tip != null ? p.tip : `<b>${fmtV(p.value)}</b>`);
    }
    function onLeave() { g.style.display = "none"; tip.style.display = "none"; }

    wrap.addEventListener("pointermove", onMove);
    wrap.addEventListener("pointerleave", onLeave);
    wrap.addEventListener("touchmove", onMove, { passive: true });
    wrap.addEventListener("touchend", onLeave);
  }

  window.renderLineChart = renderLineChart;

  // ------------------------------------------------------------------
  // 多序列折线图 —— 同类对比叠加(本基金/同类/沪深300 累计收益率)。
  //   renderMultiLineChart(container, series, opts)
  //   series: [{ name, color, points:[{label, value}] }]  各序列 x 轴按索引对齐
  //   opts  : { height, fmtValue, fmtLabel, emptyHint }
  // ------------------------------------------------------------------
  function renderMultiLineChart(container, series, opts) {
    if (!container) return;
    opts = opts || {};
    const ss = (series || []).filter(s => s && s.points && s.points.length >= 2);
    if (!ss.length) {
      container.innerHTML = `<div class="d-empty">${opts.emptyHint || "暂无对比数据"}</div>`;
      return;
    }
    const id = "mlc" + (++_uid);
    const W = 700, H = opts.height || 240;
    const padL = 46, padR = 12, padT = 12, padB = 24;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const n = Math.max(...ss.map(s => s.points.length));
    const base = ss[0].points;   // x 轴标签取最长/首序列

    let min = Infinity, max = -Infinity;
    ss.forEach(s => s.points.forEach(p => {
      if (p.value == null) return;
      if (p.value < min) min = p.value;
      if (p.value > max) max = p.value;
    }));
    if (!isFinite(min) || !isFinite(max)) { container.innerHTML = `<div class="d-empty">${opts.emptyHint || "暂无对比数据"}</div>`; return; }
    const padv = (max - min) * 0.08 || 1; min -= padv; max += padv;
    if (min === max) { min -= 1; max += 1; }
    const span = max - min;

    const xAt = i => padL + plotW * (n === 1 ? 0.5 : i / (n - 1));
    const yAt = v => padT + plotH * (1 - (v - min) / span);
    const fmtV = opts.fmtValue || (v => (v >= 0 ? "+" : "") + (+v).toFixed(1) + "%");
    const fmtL = opts.fmtLabel || (l => String(l || "").slice(5));

    const TICKS = 4;
    let grid = "", ylabels = "";
    for (let t = 0; t <= TICKS; t++) {
      const v = min + span * t / TICKS, y = yAt(v).toFixed(1);
      grid += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="#eef1f6" stroke-width="1"/>`;
      ylabels += `<text x="${padL - 6}" y="${(+y + 3).toFixed(1)}" text-anchor="end" font-size="10" fill="#9aa2b3">${fmtV(v)}</text>`;
    }
    // 零轴强调(收益率 0%)
    let zero = "";
    if (min < 0 && max > 0) {
      const zy = yAt(0).toFixed(1);
      zero = `<line x1="${padL}" y1="${zy}" x2="${W - padR}" y2="${zy}" stroke="#c7cdda" stroke-width="1" stroke-dasharray="4 3"/>`;
    }
    const xIdx = n <= 2 ? [0, n - 1] : [0, Math.floor((n - 1) / 2), n - 1];
    const xlabels = xIdx.map(i => {
      const anchor = i === 0 ? "start" : i === n - 1 ? "end" : "middle";
      const lbl = (base[i] || base[base.length - 1] || {}).label;
      return `<text x="${xAt(i).toFixed(1)}" y="${H - 6}" text-anchor="${anchor}" font-size="10" fill="#9aa2b3">${fmtL(lbl)}</text>`;
    }).join("");

    const polylines = ss.map(s => {
      const pts = s.points.map((p, i) => p.value == null ? "" : `${xAt(i).toFixed(1)},${yAt(p.value).toFixed(1)}`).filter(Boolean).join(" ");
      return `<polyline fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" points="${pts}"/>`;
    }).join("");

    const cross = `<g id="${id}-cross" style="display:none">
        <line id="${id}-vline" x1="0" y1="${padT}" x2="0" y2="${padT + plotH}" stroke="#9aa2b3" stroke-width="1" stroke-dasharray="3 3"/>
      </g>`;
    const legend = ss.map(s =>
      `<span class="mlc-leg"><i style="background:${s.color}"></i>${s.name}</span>`).join("");

    container.innerHTML = `
      <div class="chart-wrap" id="${id}-wrap">
        <svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
             class="chart-svg" id="${id}-svg" style="display:block;overflow:visible">
          ${grid}${zero}${polylines}${cross}${ylabels}${xlabels}
        </svg>
        <div class="chart-tip" id="${id}-tip"></div>
      </div>
      <div class="mlc-legend">${legend}</div>`;

    const wrap = document.getElementById(id + "-wrap");
    const svg = document.getElementById(id + "-svg");
    const tip = document.getElementById(id + "-tip");
    const g = document.getElementById(id + "-cross");
    const vline = document.getElementById(id + "-vline");
    if (!wrap || !svg) return;

    function onMove(ev) {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const cxp = (ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left;
      let i = Math.round((cxp / rect.width) * (n - 1));
      i = Math.max(0, Math.min(n - 1, i));
      const vx = xAt(i);
      vline.setAttribute("x1", vx); vline.setAttribute("x2", vx);
      g.style.display = "";
      const lbl = (base[i] || {}).label || "";
      const rows = ss.map(s => {
        const p = s.points[i];
        const v = p ? p.value : null;
        return `<div><i style="background:${s.color}"></i>${s.name} <b class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${v == null ? "—" : fmtV(v)}</b></div>`;
      }).join("");
      tip.style.left = ((vx / W) * rect.width) + "px";
      tip.style.top = (rect.height * 0.15) + "px";
      tip.style.display = "block";
      tip.innerHTML = `<div class="t-date">${fmtL(lbl)}</div>${rows}`;
    }
    function onLeave() { g.style.display = "none"; tip.style.display = "none"; }
    wrap.addEventListener("pointermove", onMove);
    wrap.addEventListener("pointerleave", onLeave);
    wrap.addEventListener("touchmove", onMove, { passive: true });
    wrap.addEventListener("touchend", onLeave);
  }

  window.renderMultiLineChart = renderMultiLineChart;
})();
