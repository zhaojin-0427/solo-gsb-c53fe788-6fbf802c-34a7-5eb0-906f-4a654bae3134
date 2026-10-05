/* 输电网单线退出（N-1）风险筛查台 —— 原生 JS 前端 */
"use strict";

const state = {
  grid: null,          // 已解析的输入 JSON
  result: null,        // /api/screen 响应
  sel: null,           // 选中场景在 result.scenarios 中的下标
  selLine: null,       // 选中查看见证的在役线路 id
  sortByRisk: true,    // 场景表排序方式
  layout: null,        // 拓扑布局 [{x,y}]
  busIndex: {},        // 母线 id -> 下标
  linePairs: {},       // 线路 id -> 端点对下标
  witnessSeq: 0,       // 见证请求序号（防竞态）
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ */
/* 输入与筛查                                                          */
/* ------------------------------------------------------------------ */

async function loadSample() {
  const r = await fetch("sample_grid.json");
  $("grid-input").value = JSON.stringify(await r.json(), null, 1);
  hideErrors();
}

function showErrors(errors) {
  const box = $("input-errors");
  box.innerHTML = "<b>输入错误：</b><ul>" +
    errors.map((e) => `<li>${escapeHtml(e)}</li>`).join("") + "</ul>";
  box.classList.remove("hidden");
}

function hideErrors() {
  $("input-errors").classList.add("hidden");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtDetail(d) {
  if (!d) return "";
  if (typeof d === "string") return d;
  try { return JSON.stringify(d); } catch (e) { return String(d); }
}

async function runScreening() {
  hideErrors();
  let grid;
  try {
    grid = JSON.parse($("grid-input").value);
  } catch (e) {
    showErrors(["JSON 解析失败：" + e.message]);
    return;
  }
  const btn = $("btn-run");
  btn.disabled = true;
  btn.textContent = "计算中…";
  const t0 = performance.now();
  try {
    const r = await fetch("/api/screen", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(grid),
    });
    const body = await r.json();
    if (!r.ok || !body.ok) {
      showErrors(body.errors || [fmtDetail(body.detail) || "服务器错误"]);
      return;
    }
    state.grid = grid;
    state.result = body;
    state.sel = null;
    state.selLine = null;
    buildTopology(grid);
    renderSummary(body, performance.now() - t0);
    renderScenarioTable();
    $("results").classList.remove("hidden");
    $("summary-card").classList.remove("hidden");
    $("detail-body").innerHTML = "<p class='muted'>在左侧场景表中选择一行查看逐线最坏负载率。</p>";
    $("witness-body").innerHTML = "<p class='muted'>选择场景或线路后，此处展示达到最坏潮流的注入方案及独立复核结果。</p>";
    $("detail-title").textContent = "（未选择场景）";
    $("witness-title").textContent = "";
  } catch (e) {
    showErrors(["请求失败：" + e.message]);
  } finally {
    btn.disabled = false;
    btn.textContent = "开始筛查";
  }
}

/* ------------------------------------------------------------------ */
/* 概览与场景表                                                        */
/* ------------------------------------------------------------------ */

function renderSummary(res, totalMs) {
  const m = res.meta;
  const stats = [
    { k: "退出场景数", v: m.scenarios, cls: "" },
    { k: "不可行场景", v: m.infeasible_scenarios, cls: m.infeasible_scenarios ? "bad" : "ok" },
    { k: "越限场景", v: m.overload_scenarios, cls: m.overload_scenarios ? "warn" : "ok" },
    { k: "全表最大负载率", v: (m.max_ratio * 100).toFixed(1) + "%", cls: m.max_ratio > 1 ? "bad" : "ok" },
    { k: "后端计算用时", v: m.elapsed_ms + " ms", cls: "" },
    { k: "端到端用时", v: Math.round(totalMs) + " ms", cls: "" },
  ];
  $("summary-stats").innerHTML = stats.map((s) =>
    `<div class="stat ${s.cls}"><div class="v">${s.v}</div><div class="k">${s.k}</div></div>`
  ).join("");
}

function worstRatio(sc) {
  return sc.worst ? sc.worst.ratio : -1;
}

function sortedScenarios() {
  const arr = state.result.scenarios.map((sc, i) => [sc, i]);
  if (state.sortByRisk) {
    arr.sort((a, b) => worstRatio(b[0]) - worstRatio(a[0]));
  } else {
    arr.sort((a, b) => String(a[0].outage).localeCompare(String(b[0].outage), "zh-Hans-CN", { numeric: true }));
  }
  return arr;
}

function renderScenarioTable() {
  const tbody = $("scenario-table").querySelector("tbody");
  tbody.innerHTML = "";
  sortedScenarios().forEach(([sc, idx], row) => {
    const tr = document.createElement("tr");
    if (sc.status !== "ok") tr.classList.add("infeasible");
    if (idx === state.sel) tr.classList.add("selected");
    const nIslands = sc.islands ? sc.islands.length : 1;
    const nOverload = sc.lines ? sc.lines.overload.filter(Boolean).length : 0;
    const worst = sc.worst;
    tr.innerHTML =
      `<td>${row + 1}</td><td>${escapeHtml(sc.outage)}</td>` +
      (sc.status === "ok"
        ? `<td><span class="badge ok">可行</span></td>`
        : `<td><span class="badge bad">不可行</span></td>`) +
      `<td>${nIslands}</td>` +
      (worst
        ? `<td>${escapeHtml(worst.line)}</td>` +
          `<td class="ratio-cell" style="color:${ratioColor(worst.ratio)}">${(worst.ratio * 100).toFixed(1)}%</td>` +
          `<td>${nOverload || "—"}</td>`
        : `<td>—</td><td>—</td><td>—</td>`);
    tr.addEventListener("click", () => selectScenario(idx));
    tbody.appendChild(tr);
  });
}

function ratioColor(r) {
  if (r > 1) return "#dc2626";
  if (r > 0.85) return "#ea580c";
  if (r > 0.6) return "#ca8a04";
  return "#16a34a";
}

/* ------------------------------------------------------------------ */
/* 场景详情（逐线最坏负载率）                                          */
/* ------------------------------------------------------------------ */

function selectScenario(idx) {
  state.sel = idx;
  state.selLine = null;
  renderScenarioTable();
  const sc = state.result.scenarios[idx];
  $("detail-title").textContent = `（退出 ${sc.outage}）`;
  const body = $("detail-body");

  if (sc.status !== "ok") {
    const bad = (sc.islands || []).filter((i) => !i.feasible);
    body.innerHTML =
      `<p><span class="badge bad">不可行</span> 退出 <b>${escapeHtml(sc.outage)}</b> 后网络解列，以下孤岛不存在零和平衡注入：</p>` +
      "<ul>" + bad.map((i) =>
        `<li>孤岛 {${i.buses.map(escapeHtml).join(", ")}}：Σp_min = ${i.sum_min}，Σp_max = ${i.sum_max}，区间不含 0</li>`
      ).join("") + "</ul>";
    updateTopoColors();
    $("witness-title").textContent = "";
    $("witness-body").innerHTML = "<p class='muted'>该场景不可行，无在役线路最坏潮流。</p>";
    return;
  }

  const L = sc.lines;
  if (!sc.worst) {
    body.innerHTML = "<p class='muted'>该场景没有在役线路，无最坏潮流结果。</p>";
    updateTopoColors();
    $("witness-title").textContent = "";
    $("witness-body").innerHTML = "<p class='muted'>该场景无在役线路。</p>";
    return;
  }
  const rows = L.id.map((id, i) => ({
    id, ratio: L.ratio[i], flow: L.max_abs_flow[i],
    limit: L.limit[i], overload: L.overload[i], dir: L.direction[i],
  })).sort((a, b) => b.ratio - a.ratio);

  const islandHtml = sc.islands
    ? `<p>解列为 ${sc.islands.length} 个孤岛（均可零和平衡）：` +
      sc.islands.map((i) => `{${i.buses.map(escapeHtml).join(", ")}}`).join(" ") + "</p>"
    : "";

  body.innerHTML =
    islandHtml +
    `<p>最危险线路：<b>${escapeHtml(sc.worst.line)}</b>，最坏负载率 ` +
    `<b style="color:${ratioColor(sc.worst.ratio)}">${(sc.worst.ratio * 100).toFixed(2)}%</b>` +
    (sc.worst.overload ? "（越限）" : sc.worst.ratio > 0.999999 ? "（恰好达限，不算越限）" : "") +
    `。点击下表任意线路查看其最坏注入见证：</p>` +
    `<div class="table-wrap"><table><thead><tr>` +
    `<th>线路</th><th>最坏 |潮流|</th><th>限额</th><th>最坏负载率</th><th>状态</th>` +
    `</tr></thead><tbody>` +
    rows.map((r) =>
      `<tr data-line="${escapeHtml(r.id)}">` +
      `<td>${escapeHtml(r.id)}</td>` +
      `<td>${r.flow.toFixed(2)}</td>` +
      `<td>${r.limit}</td>` +
      `<td class="ratio-cell" style="color:${ratioColor(r.ratio)}">${(r.ratio * 100).toFixed(2)}%</td>` +
      `<td>${r.overload ? '<span class="badge bad">越限</span>'
        : r.ratio > 0.999999 ? '<span class="badge warn">达限</span>'
        : '<span class="badge ok">正常</span>'}</td></tr>`
    ).join("") +
    `</tbody></table></div>`;

  body.querySelectorAll("tr[data-line]").forEach((tr) => {
    tr.addEventListener("click", () => showWitness(tr.dataset.line));
  });

  updateTopoColors();
  // 自动展示最危险线路的见证
  showWitness(sc.worst.line);
}

/* ------------------------------------------------------------------ */
/* 注入见证                                                            */
/* ------------------------------------------------------------------ */

async function showWitness(lineId) {
  const sc = state.result.scenarios[state.sel];
  state.selLine = lineId;
  updateTopoColors();
  const seq = ++state.witnessSeq;
  $("witness-title").textContent = `（退出 ${sc.outage} · 线路 ${lineId}）`;
  $("witness-body").innerHTML = "<p class='muted'>加载见证与复核结果…</p>";
  try {
    const r = await fetch("/api/witness", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grid: state.grid, outage: sc.outage, line: lineId }),
    });
    const body = await r.json();
    if (seq !== state.witnessSeq) return;   // 已有更新的请求，丢弃过期响应
    if (!r.ok || !body.ok) {
      $("witness-body").innerHTML = `<p class='muted'>见证获取失败：${escapeHtml((body.errors || [r.statusText]).join("；"))}</p>`;
      return;
    }
    renderWitness(body);
  } catch (e) {
    if (seq !== state.witnessSeq) return;
    $("witness-body").innerHTML = `<p class='muted'>见证获取失败：${escapeHtml(e.message)}</p>`;
  }
}

function renderWitness(res) {
  const w = res.witness;
  const c = res.checks;
  const dirText = w.direction === 1 ? "正向最大潮流" : "反向最大潮流";
  const inj = Object.entries(w.injections);
  const busBounds = {};
  state.grid.buses.forEach((b) => { busBounds[b.id] = b; });
  inj.sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  const flows = Object.entries(w.flows)
    .map(([id, f]) => {
      const lim = (state.grid.lines.find((l) => l.id === id) || {}).limit || 1;
      return { id, f, lim, ratio: Math.abs(f) / lim };
    })
    .sort((a, b) => b.ratio - a.ratio);

  const checkHtml =
    `<div class="checks">` +
    `<span class="check ${c.bounds_ok ? "pass" : "fail"}">${c.bounds_ok ? "✓" : "✗"} 注入在上下界内（最大越界 ${c.bounds_max_violation}）</span>` +
    c.island_balance.map((b, i) =>
      `<span class="check ${b.ok ? "pass" : "fail"}">${b.ok ? "✓" : "✗"} 岛 ${i + 1} 注入和 = ${b.sum_injection}</span>`
    ).join("") +
    `<span class="check ${c.flow_consistency_max_err < 1e-6 ? "pass" : "fail"}">${c.flow_consistency_max_err < 1e-6 ? "✓" : "✗"} 潮流满足 f=(θ起−θ终)/x（最大偏差 ${c.flow_consistency_max_err}）</span>` +
    (c.at_limit_not_overload ? `<span class="check pass">✓ 恰好达限，未判越限</span>` : "") +
    `</div>`;

  const injRows = inj.map(([id, p]) => {
    const b = busBounds[id] || {};
    const th = w.angles ? w.angles[id] : null;
    return `<tr><td>${escapeHtml(id)}</td><td>${fmt(p)}</td>` +
      `<td>${b.p_min}</td><td>${b.p_max}</td><td>${th === null ? "—" : fmt(th, 5)}</td></tr>`;
  }).join("");

  const flowRows = flows.slice(0, 20).map((r) =>
    `<tr><td>${escapeHtml(r.id)}</td><td>${fmt(r.f)}</td><td>${r.lim}</td>` +
    `<td class="ratio-cell" style="color:${ratioColor(r.ratio)}">${(r.ratio * 100).toFixed(1)}%</td></tr>`
  ).join("");

  $("witness-body").innerHTML =
    `<p>使线路 <b>${escapeHtml(w.line)}</b> 达到最坏潮流的可行注入方案（${dirText}）：` +
    `f = <b>${fmt(w.flow)}</b>，限额 ${w.limit}，负载率 ` +
    `<b style="color:${ratioColor(w.ratio)}">${(w.ratio * 100).toFixed(2)}%</b>，` +
    (w.overload ? `<span class="badge bad">越限</span>` : `<span class="badge ok">未越限</span>`) +
    `（各线路最坏见证互不相同，本见证仅对应线路 ${escapeHtml(w.line)}）</p>` +
    checkHtml +
    `<div class="witness-grid">` +
    `<div><h3>母线注入见证（含相角 θ，可手工复核潮流）</h3>` +
    `<div class="table-wrap"><table><thead><tr><th>母线</th><th>注入 p</th><th>p_min</th><th>p_max</th><th>θ</th></tr></thead>` +
    `<tbody>${injRows}</tbody></table></div></div>` +
    `<div><h3>该见证下的线路潮流（按负载率排序，前 20 条）</h3>` +
    `<div class="table-wrap"><table><thead><tr><th>线路</th><th>潮流 f</th><th>限额</th><th>负载率</th></tr></thead>` +
    `<tbody>${flowRows}</tbody></table></div></div>` +
    `</div>`;
}

function fmt(v, nd = 3) {
  return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: nd });
}

/* ------------------------------------------------------------------ */
/* 拓扑图                                                              */
/* ------------------------------------------------------------------ */

function buildTopology(grid) {
  const n = grid.buses.length;
  state.busIndex = {};
  grid.buses.forEach((b, i) => { state.busIndex[b.id] = i; });
  state.layout = forceLayout(grid);
  state.linePairs = {};
  grid.lines.forEach((l) => {
    state.linePairs[l.id] = [state.busIndex[l.from], state.busIndex[l.to]];
  });

  const svg = $("topo");
  const pos = state.layout;
  const xs = pos.map((p) => p.x), ys = pos.map((p) => p.y);
  const x0 = Math.min(...xs) - 30, x1 = Math.max(...xs) + 30;
  const y0 = Math.min(...ys) - 30, y1 = Math.max(...ys) + 30;
  svg.setAttribute("viewBox", `${x0} ${y0} ${x1 - x0} ${y1 - y0}`);
  svg.innerHTML = "";

  const nodeR = Math.max(2.5, Math.min(9, 220 / n));
  state.nodeR = nodeR;

  // 并行线路分组，分别画弧线以便区分
  const groups = {};
  grid.lines.forEach((l) => {
    const a = state.busIndex[l.from], b = state.busIndex[l.to];
    const key = a < b ? `${a}-${b}` : `${b}-${a}`;
    (groups[key] = groups[key] || []).push(l);
  });

  const ns = "http://www.w3.org/2000/svg";
  state.edgeEls = {};
  Object.values(groups).forEach((group) => {
    group.forEach((l, i) => {
      const [a, b] = state.linePairs[l.id];
      const pa = pos[a], pb = pos[b];
      const path = document.createElementNS(ns, "path");
      let d;
      if (group.length === 1) {
        d = `M ${pa.x} ${pa.y} L ${pb.x} ${pb.y}`;
      } else {
        const mx = (pa.x + pb.x) / 2, my = (pa.y + pb.y) / 2;
        const dx = pb.x - pa.x, dy = pb.y - pa.y;
        const len = Math.hypot(dx, dy) || 1;
        const off = (i - (group.length - 1) / 2) * Math.min(18, len / 3);
        d = `M ${pa.x} ${pa.y} Q ${mx - (dy / len) * off} ${my + (dx / len) * off} ${pb.x} ${pb.y}`;
      }
      path.setAttribute("d", d);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "#94a3b8");
      path.setAttribute("stroke-width", 1.6);
      path.classList.add("edge");
      const title = document.createElementNS(ns, "title");
      title.textContent = `${l.id}（${l.from} — ${l.to}）`;
      path.appendChild(title);
      path.addEventListener("click", () => {
        if (state.sel === null) return;
        const sc = state.result.scenarios[state.sel];
        if (sc.status !== "ok" || sc.outage === l.id) return;
        showWitness(l.id);
      });
      svg.appendChild(path);
      state.edgeEls[l.id] = path;
    });
  });

  grid.buses.forEach((b, i) => {
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("cx", pos[i].x);
    c.setAttribute("cy", pos[i].y);
    c.setAttribute("r", nodeR);
    c.setAttribute("fill", "#fff");
    c.setAttribute("stroke", "#1d4ed8");
    c.setAttribute("stroke-width", 1.4);
    const title = document.createElementNS(ns, "title");
    title.textContent = `${b.id}  [${b.p_min}, ${b.p_max}]`;
    c.appendChild(title);
    svg.appendChild(c);
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", pos[i].x + nodeR + 1.5);
    t.setAttribute("y", pos[i].y + 3);
    t.classList.add("bus-label");
    t.textContent = b.id;
    svg.appendChild(t);
  });
  updateTopoColors();
}

function updateTopoColors() {
  if (!state.edgeEls) return;
  const sc = state.sel === null ? null : state.result.scenarios[state.sel];
  const ratioByLine = {};
  if (sc && sc.lines) {
    sc.lines.id.forEach((id, i) => { ratioByLine[id] = sc.lines.ratio[i]; });
  }
  Object.entries(state.edgeEls).forEach(([id, el]) => {
    el.setAttribute("stroke-dasharray", "none");
    el.setAttribute("stroke-width", 1.6);
    el.setAttribute("opacity", 1);
    if (!sc) {
      el.setAttribute("stroke", "#94a3b8");
      return;
    }
    if (id === sc.outage) {
      el.setAttribute("stroke", "#dc2626");
      el.setAttribute("stroke-dasharray", "5 4");
      el.setAttribute("stroke-width", 2.2);
      return;
    }
    if (sc.status !== "ok") {
      el.setAttribute("stroke", "#cbd5e1");
      return;
    }
    el.setAttribute("stroke", ratioColor(ratioByLine[id] ?? 0));
    if (id === state.selLine) {
      el.setAttribute("stroke", "#1d4ed8");
      el.setAttribute("stroke-width", 4);
    } else if (sc.worst && id === sc.worst.line) {
      el.setAttribute("stroke-width", 3.4);
    }
  });
  const hint = $("topo-hint");
  if (sc) {
    hint.textContent = sc.status === "ok"
      ? `（退出 ${sc.outage}：虚线；颜色 = 各线路最坏负载率；粗线 = 最危险线路${state.selLine ? "；蓝线 = 选中见证线路" : ""}）`
      : `（退出 ${sc.outage}：场景不可行）`;
  } else {
    hint.textContent = "（选择左侧场景后按最坏负载率着色）";
  }
}

/* Fruchterman–Reingold 力导向布局（若输入提供 x/y 坐标则直接使用） */
function forceLayout(grid) {
  const n = grid.buses.length;
  if (grid.buses.every((b) => Number.isFinite(b.x) && Number.isFinite(b.y))) {
    return grid.buses.map((b) => ({ x: b.x, y: -b.y }));
  }
  const idx = {};
  grid.buses.forEach((b, i) => { idx[b.id] = i; });
  const edges = grid.lines.map((l) => [idx[l.from], idx[l.to]]);
  const pos = grid.buses.map((_, i) => {
    const a = (2 * Math.PI * i) / n;
    return { x: 100 * Math.cos(a) + (i % 7), y: 100 * Math.sin(a) + (i % 5) };
  });
  const k = 26;
  let temp = 45;
  const iters = n > 60 ? 350 : 500;
  for (let it = 0; it < iters; it++) {
    const disp = pos.map(() => ({ x: 0, y: 0 }));
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y;
        let d = Math.hypot(dx, dy) || 0.01;
        const f = (k * k) / d;
        dx /= d; dy /= d;
        disp[i].x += dx * f; disp[i].y += dy * f;
        disp[j].x -= dx * f; disp[j].y -= dy * f;
      }
    }
    for (const [a, b] of edges) {
      let dx = pos[a].x - pos[b].x, dy = pos[a].y - pos[b].y;
      const d = Math.hypot(dx, dy) || 0.01;
      const f = (d * d) / k;
      dx /= d; dy /= d;
      disp[a].x -= dx * f; disp[a].y -= dy * f;
      disp[b].x += dx * f; disp[b].y += dy * f;
    }
    for (let i = 0; i < n; i++) {
      const dl = Math.hypot(disp[i].x, disp[i].y) || 0.01;
      const s = Math.min(dl, temp) / dl;
      pos[i].x += disp[i].x * s;
      pos[i].y += disp[i].y * s;
    }
    temp *= 0.96;
  }
  return pos;
}

/* ------------------------------------------------------------------ */
/* 事件绑定                                                            */
/* ------------------------------------------------------------------ */

$("btn-sample").addEventListener("click", loadSample);
$("btn-run").addEventListener("click", runScreening);
$("file-input").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => { $("grid-input").value = rd.result; hideErrors(); };
  rd.readAsText(f);
});
$("sort-risk").addEventListener("click", () => {
  state.sortByRisk = true;
  $("sort-risk").classList.add("active");
  $("sort-name").classList.remove("active");
  renderScenarioTable();
});
$("sort-name").addEventListener("click", () => {
  state.sortByRisk = false;
  $("sort-name").classList.add("active");
  $("sort-risk").classList.remove("active");
  renderScenarioTable();
});
$("sort-risk").classList.add("active");

// 页面打开即加载示例，便于值班员直接体验
loadSample();
