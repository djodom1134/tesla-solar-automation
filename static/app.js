/* Tesla solar import/export dashboard.
   Charts are hand-rolled SVG — no CDN, no build step. */

import {
  PAD, HEIGHT, el, barPath, niceTicks, chartFrame, xTickEvery,
  showTip, hideTip, attachCrosshair, renderLegend,
} from "./chart.js";
import { $, api, COLOR, nfmt, initTheme, showGate as gate } from "./shared.js";

const state = {
  period: "today",
  data: null,
  config: null,
  tableView: false,
  loading: false,
};

/* ------------------------------------------------------------------ format */

const kwh = (v) => nfmt(v, Math.abs(v ?? 0) >= 100 ? 0 : 1);
const kw = (v) => nfmt(v, 2);

function money(v) {
  const cur = state.config?.currency || "USD";
  try {
    return (v ?? 0).toLocaleString(undefined, { style: "currency", currency: cur });
  } catch {
    return `${cur} ${nfmt(v, 2)}`;
  }
}

function bucketLabel(iso, bucket, period) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  if (bucket === "year") return String(d.getFullYear());
  if (bucket === "month") return d.toLocaleDateString(undefined, { month: "short" });
  if (period === "week") return d.toLocaleDateString(undefined, { weekday: "short" });
  return d.toLocaleDateString(undefined, { day: "numeric" });
}

function fullLabel(iso, bucket) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  if (bucket === "year") return String(d.getFullYear());
  if (bucket === "month") return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

const timeLabel = (iso) =>
  new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

/* ------------------------------------------------------- chart: diverging */

/** Grid import/export. Export above the zero line, import below it.
    This is the diverging form: two opposed hues, neutral zero, one axis. */
function renderDivergingBars(host, rows, bucket, period) {
  const maxUp = Math.max(0, ...rows.map((r) => r.grid_export));
  const maxDown = Math.max(0, ...rows.map((r) => r.grid_import));
  const { ticks, lo, hi } = niceTicks(-maxDown, maxUp, 4);
  const { svg, plotW, plotH, yScale } = chartFrame(host, {
    yLo: lo, yHi: hi, yTicks: ticks, unit: "kWh",
  });

  const band = plotW / rows.length;
  const bw = Math.min(24, band * 0.62);
  const zeroY = yScale(0);
  const step = xTickEvery(rows.length, plotW);

  // Direct-label only the extremes — a number on every bar goes unread.
  const iUp = rows.findIndex((r) => r.grid_export === maxUp && maxUp > 0);
  const iDown = rows.findIndex((r) => r.grid_import === maxDown && maxDown > 0);

  rows.forEach((r, i) => {
    const cx = PAD.left + band * i + band / 2;
    const x = cx - bw / 2;

    const hUp = Math.abs(yScale(r.grid_export) - zeroY);
    const hDown = Math.abs(yScale(-r.grid_import) - zeroY);

    if (hUp > 0.5) {
      svg.append(el("path", {
        d: barPath(x, zeroY - hUp, bw, hUp, 4, true),
        fill: COLOR("export"), class: "mark",
      }));
    }
    if (hDown > 0.5) {
      svg.append(el("path", {
        d: barPath(x, zeroY, bw, hDown, 4, false),
        fill: COLOR("import"), class: "mark",
      }));
    }

    if (i === iUp) {
      const t = el("text", { x: cx, y: zeroY - hUp - 7, "text-anchor": "middle", class: "bar-label" });
      t.textContent = kwh(r.grid_export);
      svg.append(t);
    }
    if (i === iDown) {
      const t = el("text", { x: cx, y: zeroY + hDown + 14, "text-anchor": "middle", class: "bar-label" });
      t.textContent = kwh(r.grid_import);
      svg.append(t);
    }

    if (i % step === 0) {
      const t = el("text", {
        x: cx, y: PAD.top + plotH + 20, "text-anchor": "middle", class: "tick-label",
      });
      t.textContent = bucketLabel(r.timestamp, bucket, period);
      svg.append(t);
    }

    // Hit target spans the whole band, so the reader aims at a date, not a 2px bar.
    const hit = el("rect", {
      x: PAD.left + band * i, y: PAD.top, width: band, height: plotH,
      class: "hit", tabindex: 0, role: "button",
    });
    const label = fullLabel(r.timestamp, bucket);
    const rowsFor = () => [
      { name: "Exported", value: `${kwh(r.grid_export)} kWh`, color: COLOR("export") },
      { name: "Imported", value: `${kwh(r.grid_import)} kWh`, color: COLOR("import") },
      { name: "Net", value: `${r.grid_net >= 0 ? "+" : "−"}${kwh(Math.abs(r.grid_net))} kWh`, color: COLOR("muted") },
    ];
    hit.addEventListener("pointermove", (e) => showTip(e, label, rowsFor()));
    hit.addEventListener("focus", () => {
      const b = hit.getBoundingClientRect();
      showTip({ clientX: b.left + b.width / 2, clientY: b.top + b.height / 2 }, label, rowsFor());
    });
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("blur", hideTip);
    svg.append(hit);
  });
}

/** Intraday grid power, same diverging story at 5-minute resolution. */
function renderDivergingArea(host, rows) {
  const maxUp = Math.max(0, ...rows.map((r) => r.grid_export));
  const maxDown = Math.max(0, ...rows.map((r) => r.grid_import));
  const { ticks, lo, hi } = niceTicks(-maxDown, maxUp, 4);
  const { svg, plotW, plotH, yScale } = chartFrame(host, { yLo: lo, yHi: hi, yTicks: ticks, unit: "kW" });

  const xScale = (i) => PAD.left + (i / Math.max(1, rows.length - 1)) * plotW;
  const zeroY = yScale(0);

  const area = (key, sign, color) => {
    let d = `M${xScale(0)},${zeroY}`;
    rows.forEach((r, i) => { d += ` L${xScale(i)},${yScale(sign * r[key])}`; });
    d += ` L${xScale(rows.length - 1)},${zeroY} Z`;
    svg.append(el("path", { d, fill: color, "fill-opacity": 0.16 }));

    let line = "";
    rows.forEach((r, i) => { line += `${i ? "L" : "M"}${xScale(i)},${yScale(sign * r[key])}`; });
    svg.append(el("path", {
      d: line, fill: "none", stroke: color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
  };

  area("grid_export", 1, COLOR("export"));
  area("grid_import", -1, COLOR("import"));

  const step = Math.max(1, Math.ceil(rows.length / Math.max(2, Math.floor(plotW / 70))));
  rows.forEach((r, i) => {
    if (i % step !== 0) return;
    const t = el("text", {
      x: xScale(i), y: PAD.top + plotH + 20, "text-anchor": "middle", class: "tick-label",
    });
    t.textContent = timeLabel(r.timestamp);
    svg.append(t);
  });

  attachCrosshair(svg, rows, xScale, plotW, plotH, (r) => [
    { name: "Exporting", value: `${kw(r.grid_export)} kW`, color: COLOR("export") },
    { name: "Importing", value: `${kw(r.grid_import)} kW`, color: COLOR("import") },
  ], (r) => timeLabel(r.timestamp));
}

/* ----------------------------------------------------------- chart: stack */

/** Where the home's energy came from. Stack order is battery -> solar -> grid:
    it keeps aqua and red non-adjacent, which is what lifts the worst colorblind
    adjacent-pair separation from ΔE 9.7 to 35.9 in dark mode. */
function renderStackedBars(host, rows, bucket, period, hasBattery) {
  const series = [
    hasBattery && { key: "home_from_battery", name: "Battery", color: COLOR("battery") },
    { key: "home_from_solar", name: "Solar", color: COLOR("solar") },
    { key: "home_from_grid", name: "Grid", color: COLOR("import") },
  ].filter(Boolean);

  const totals = rows.map((r) => series.reduce((s, x) => s + r[x.key], 0));
  const { ticks, lo, hi } = niceTicks(0, Math.max(0.1, ...totals), 4);
  const { svg, plotW, plotH, yScale } = chartFrame(host, { yLo: lo, yHi: hi, yTicks: ticks, unit: "kWh" });

  const band = plotW / rows.length;
  const bw = Math.min(24, band * 0.62);
  const step = xTickEvery(rows.length, plotW);
  const GAP = 2; // surface gap — white does the separating, never a stroke
  const iMax = totals.indexOf(Math.max(...totals));

  rows.forEach((r, i) => {
    const cx = PAD.left + band * i + band / 2;
    const x = cx - bw / 2;
    let cursor = yScale(0);
    const topIdx = series.reduce((last, s, j) => (r[s.key] > 0 ? j : last), -1);

    series.forEach((s, j) => {
      const v = r[s.key];
      if (v <= 0) return;
      const h = Math.abs(yScale(v) - yScale(0));
      const drawH = Math.max(0, h - (j === topIdx ? 0 : GAP));
      if (drawH <= 0.5) return;
      const y = cursor - h;
      svg.append(el("path", {
        d: barPath(x, y, bw, drawH, 4, j === topIdx),
        fill: s.color, class: "mark",
      }));
      cursor -= h;
    });

    if (i === iMax && totals[i] > 0) {
      const t = el("text", { x: cx, y: yScale(totals[i]) - 7, "text-anchor": "middle", class: "bar-label" });
      t.textContent = kwh(totals[i]);
      svg.append(t);
    }

    if (i % step === 0) {
      const t = el("text", {
        x: cx, y: PAD.top + plotH + 20, "text-anchor": "middle", class: "tick-label",
      });
      t.textContent = bucketLabel(r.timestamp, bucket, period);
      svg.append(t);
    }

    const hit = el("rect", {
      x: PAD.left + band * i, y: PAD.top, width: band, height: plotH,
      class: "hit", tabindex: 0, role: "button",
    });
    const label = fullLabel(r.timestamp, bucket);
    const rowsFor = () => [
      ...series.map((s) => ({ name: s.name, value: `${kwh(r[s.key])} kWh`, color: s.color })),
      { name: "Total used", value: `${kwh(totals[i])} kWh`, color: COLOR("muted") },
    ];
    const show = (e) => showTip(e, label, rowsFor());
    hit.addEventListener("pointermove", show);
    hit.addEventListener("focus", () => {
      const b = hit.getBoundingClientRect();
      showTip({ clientX: b.left + b.width / 2, clientY: b.top + b.height / 2 }, label, rowsFor());
    });
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("blur", hideTip);
    svg.append(hit);
  });

  return series;
}

/* ----------------------------------------------------------- chart: lines */

function renderLines(host, rows, series) {
  const all = series.flatMap((s) => rows.map((r) => r[s.key]));
  const { ticks, lo, hi } = niceTicks(Math.min(0, ...all), Math.max(0.1, ...all), 4);
  const { svg, plotW, plotH, yScale } = chartFrame(host, { yLo: lo, yHi: hi, yTicks: ticks, unit: "kW" });

  const xScale = (i) => PAD.left + (i / Math.max(1, rows.length - 1)) * plotW;

  for (const s of series) {
    let d = "";
    rows.forEach((r, i) => { d += `${i ? "L" : "M"}${xScale(i)},${yScale(r[s.key])}`; });
    svg.append(el("path", {
      d, fill: "none", stroke: s.color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
  }

  const step = Math.max(1, Math.ceil(rows.length / Math.max(2, Math.floor(plotW / 70))));
  rows.forEach((r, i) => {
    if (i % step !== 0) return;
    const t = el("text", {
      x: xScale(i), y: PAD.top + plotH + 20, "text-anchor": "middle", class: "tick-label",
    });
    t.textContent = timeLabel(r.timestamp);
    svg.append(t);
  });

  attachCrosshair(svg, rows, xScale, plotW, plotH,
    (r) => series.map((s) => ({ name: s.name, value: `${kw(r[s.key])} kW`, color: s.color })),
    (r) => timeLabel(r.timestamp));
}

/* ------------------------------------------------------------------ table */

function renderTable(rows, total, bucket, hasBattery) {
  const cols = [
    { key: "solar", name: "Solar", cls: "key-solar" },
    { key: "home", name: "Home used", cls: "key-home" },
    { key: "home_from_solar", name: "Home ← solar" },
    hasBattery && { key: "home_from_battery", name: "Home ← battery" },
    { key: "home_from_grid", name: "Home ← grid" },
    { key: "grid_import", name: "Imported", cls: "key-import" },
    { key: "grid_export", name: "Exported", cls: "key-export" },
    { key: "grid_net", name: "Net" },
  ].filter(Boolean);

  const table = $("data-table");
  const thead = table.querySelector("thead");
  const tbody = table.querySelector("tbody");
  thead.replaceChildren();
  tbody.replaceChildren();

  const hr = document.createElement("tr");
  const th0 = document.createElement("th");
  th0.textContent = "Period";
  hr.append(th0);
  for (const c of cols) {
    const th = document.createElement("th");
    if (c.cls) {
      const key = document.createElement("span");
      key.className = `key ${c.cls}`;
      th.append(key);
    }
    th.append(document.createTextNode(`${c.name} (kWh)`));
    hr.append(th);
  }
  thead.append(hr);

  for (const r of rows) {
    const tr = document.createElement("tr");
    const td0 = document.createElement("td");
    td0.textContent = fullLabel(r.timestamp, bucket);
    tr.append(td0);
    for (const c of cols) {
      const td = document.createElement("td");
      const v = r[c.key];
      td.textContent = (c.key === "grid_net" && v > 0 ? "+" : "") + kwh(v);
      tr.append(td);
    }
    tbody.append(tr);
  }

  let tfoot = table.querySelector("tfoot");
  if (!tfoot) { tfoot = document.createElement("tfoot"); table.append(tfoot); }
  tfoot.replaceChildren();
  const ftr = document.createElement("tr");
  const ftd = document.createElement("td");
  ftd.textContent = "Total";
  ftr.append(ftd);
  for (const c of cols) {
    const td = document.createElement("td");
    const v = total[c.key];
    td.textContent = (c.key === "grid_net" && v > 0 ? "+" : "") + kwh(v);
    ftr.append(td);
  }
  tfoot.append(ftr);
}

/* ------------------------------------------------------------------ render */

function renderLive(live, hasBattery) {
  const box = $("live");
  if (!live) { box.hidden = true; return; }
  box.hidden = false;

  $("live-solar").textContent = kw(live.solar);
  $("live-home").textContent = kw(live.home);

  // The grid tile flips identity with direction — import is red, export is blue, always.
  const importing = live.grid_direction === "import";
  const exporting = live.grid_direction === "export";
  $("live-grid-label").textContent = importing
    ? "Importing now"
    : exporting
    ? "Exporting now"
    : "Grid idle";
  $("live-grid").textContent = kw(Math.abs(live.grid));
  $("live-grid-key").className = `key ${importing ? "key-import" : exporting ? "key-export" : "key-idle"}`;

  const batteryTile = $("live-battery-tile");
  batteryTile.hidden = !hasBattery;
  if (hasBattery) {
    const pct = live.battery_percent;
    $("live-battery-label").textContent =
      live.battery_direction === "charging"
        ? `Battery charging${pct != null ? ` · ${nfmt(pct, 0)}%` : ""}`
        : live.battery_direction === "discharging"
        ? `Battery discharging${pct != null ? ` · ${nfmt(pct, 0)}%` : ""}`
        : `Battery idle${pct != null ? ` · ${nfmt(pct, 0)}%` : ""}`;
    $("live-battery").textContent = kw(Math.abs(live.battery));
  }
}

function render() {
  const d = state.data;
  if (!d) return;

  $("site-name").textContent = d.site_name;
  $("updated").textContent = `Updated ${new Date(d.generated_at).toLocaleTimeString()}`;
  renderLive(d.live, d.has_battery);

  const total = d.total || {};
  const rows = d.rows || [];
  const label = state.config.periods.find((p) => p.key === d.period)?.label ?? d.period;
  $("hero-period").textContent = label.toLowerCase();

  const hasData = rows.length > 0 && (total.home > 0 || total.solar > 0);
  $("empty").hidden = hasData;
  for (const id of ["card-grid", "card-mix"]) $(id).hidden = !hasData;
  if (!hasData) {
    $("empty").textContent = "No energy data for this period yet.";
    return;
  }

  /* Hero — the one number the dashboard leads with. */
  const net = total.grid_net ?? 0;
  $("hero-value").textContent = `${net > 0 ? "+" : net < 0 ? "−" : ""}${kwh(Math.abs(net))}`;
  $("hero-sub").textContent =
    net > 0
      ? `Net exporter — you sent ${kwh(total.grid_export)} kWh out and pulled ${kwh(total.grid_import)} kWh back.`
      : net < 0
      ? `Net importer — you pulled ${kwh(total.grid_import)} kWh in and sent ${kwh(total.grid_export)} kWh out.`
      : "Perfectly balanced with the grid.";

  const m = d.money;
  $("hero-money").hidden = !m;
  if (m) {
    $("money-net").textContent = `${m.net >= 0 ? "+" : "−"}${money(Math.abs(m.net))}`;
    $("money-net").className = `money-figure${m.net >= 0 ? " positive" : ""}`;
    $("money-detail").textContent = `${money(m.credit)} export credit − ${money(m.cost)} import cost`;
  }

  /* KPIs */
  $("kpi-solar").textContent = kwh(total.solar);
  $("kpi-home").textContent = kwh(total.home);
  $("kpi-import").textContent = kwh(total.grid_import);
  $("kpi-export").textContent = kwh(total.grid_export);
  const self = total.self_sufficiency ?? 0;
  $("kpi-self").textContent = nfmt(self, 0);
  $("kpi-self-fill").style.width = `${Math.max(0, Math.min(100, self))}%`;

  /* Charts. `today` has no multi-bucket energy series — Tesla returns a single row —
     so it plots the intraday power feed instead, still one axis, still kW. */
  const intraday = d.period === "today" && d.intraday && d.intraday.length > 1 ? d.intraday : null;

  if (intraday) {
    $("grid-title").textContent = "Grid power today";
    $("grid-sub").textContent = "Above the line you're exporting; below it you're importing.";
    renderDivergingArea($("chart-grid"), intraday);
    renderLegend($("grid-legend"), [
      { name: "Exporting to grid", color: COLOR("export"), line: true },
      { name: "Importing from grid", color: COLOR("import"), line: true },
    ]);

    const lineSeries = [
      { key: "solar", name: "Solar", color: COLOR("solar") },
      { key: "home", name: "Home", color: COLOR("home") },
      ...(d.has_battery ? [{ key: "battery", name: "Battery", color: COLOR("battery") }] : []),
    ];
    $("mix-title").textContent = "Power flows today";
    $("mix-sub").textContent = d.has_battery
      ? "Battery is positive when discharging, negative when charging."
      : "Solar production against household demand.";
    renderLines($("chart-mix"), intraday, lineSeries);
    renderLegend($("mix-legend"), lineSeries.map((s) => ({ ...s, line: true })));
  } else {
    const single = rows.length === 1;
    $("grid-title").textContent = "Grid import & export";
    $("grid-sub").textContent = single
      ? "A single bucket for this period."
      : "Above the line you exported; below it you imported.";
    renderDivergingBars($("chart-grid"), rows, d.bucket, d.period);
    renderLegend($("grid-legend"), [
      { name: "Exported to grid", color: COLOR("export") },
      { name: "Imported from grid", color: COLOR("import") },
    ]);

    $("mix-title").textContent = "Where your home's energy came from";
    $("mix-sub").textContent = "Stacked to the home's total consumption.";
    const series = renderStackedBars($("chart-mix"), rows, d.bucket, d.period, d.has_battery);
    renderLegend($("mix-legend"), series.map((s) => ({ name: s.name, color: s.color })));
  }

  renderTable(rows, total, d.bucket, d.has_battery);
  $("card-table").hidden = !state.tableView;
}

/* -------------------------------------------------------------------- data */

async function load() {
  state.loading = true;
  $("content").classList.add("loading"); // hold the previous render, no skeleton flash
  $("error-bar").hidden = true;
  try {
    state.data = await api(`/api/dashboard?period=${state.period}`);
    render();
  } catch (err) {
    if (err.status === 401) { showGate(err.message); return; }
    $("error-bar").hidden = false;
    $("error-bar").textContent = err.message;
  } finally {
    state.loading = false;
    $("content").classList.remove("loading");
  }
}

/* -------------------------------------------------------------------- gate */

const showGate = (message) => {
  $("app").hidden = true;
  $("gate").hidden = false;
  gate(state.config, message);
};

/* -------------------------------------------------------------------- init */

function initPeriods() {
  const host = $("period");
  host.replaceChildren();
  for (const p of state.config.periods) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.role = "tab";
    btn.textContent = p.label;
    btn.setAttribute("aria-selected", String(p.key === state.period));
    btn.addEventListener("click", () => {
      state.period = p.key;
      for (const b of host.children) b.setAttribute("aria-selected", String(b === btn));
      load();
    });
    host.append(btn);
  }
}

async function main() {
  initTheme(() => render());
  state.config = await api("/api/config");

  if (!state.config.configured || !state.config.authenticated) {
    showGate();
    return;
  }

  $("gate").hidden = true;
  $("app").hidden = false;

  initPeriods();

  $("table-toggle").addEventListener("change", (e) => {
    state.tableView = e.target.checked;
    $("card-table").hidden = !state.tableView;
  });

  $("logout").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST" });
    location.href = "/";
  });

  await load();

  // Live values go stale fast; the history behind them does not.
  setInterval(() => { if (!state.loading && !document.hidden) load(); }, 30_000);
  addEventListener("resize", () => { if (state.data) render(); });
}

main().catch((err) => showGate(err.message));
