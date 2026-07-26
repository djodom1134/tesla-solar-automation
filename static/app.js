/* Tesla solar import/export dashboard.
   Charts are hand-rolled SVG — no CDN, no build step. */

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";

const state = {
  period: "today",
  data: null,
  config: null,
  tableView: false,
  loading: false,
};

/* Entity -> color. One entity keeps its hue across every chart:
   grid import is always red, grid export always blue. Read from CSS so the
   light/dark steps stay defined in exactly one place. */
const COLOR = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();

/* ------------------------------------------------------------------ format */

const nfmt = (v, d = 1) =>
  (v ?? 0).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

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

/* ------------------------------------------------------------------- svg */

function el(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  return node;
}

/** Rounded only at the data end; square where it meets the baseline. */
function barPath(x, y, w, h, r, roundTop) {
  if (h <= 0) return "";
  const rr = Math.min(r, w / 2, h);
  return roundTop
    ? `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y} L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} L${x + w},${y + h} Z`
    : `M${x},${y} L${x},${y + h - rr} Q${x},${y + h} ${x + rr},${y + h} L${x + w - rr},${y + h} Q${x + w},${y + h} ${x + w},${y + h - rr} L${x + w},${y} Z`;
}

/** Clean axis ticks that always include zero. */
function niceTicks(min, max, count = 5) {
  if (min === max) { min = Math.min(0, min); max = Math.max(1, max); }
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(Math.abs(raw) || 1));
  const norm = raw / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let t = lo; t <= hi + step / 2; t += step) ticks.push(Math.abs(t) < step / 1e6 ? 0 : t);
  return { ticks, lo, hi };
}

const PAD = { top: 24, right: 20, bottom: 32, left: 52 };
const HEIGHT = 260;

function chartFrame(host, { yLo, yHi, yTicks, unit, width }) {
  host.replaceChildren();
  const w = width || host.clientWidth || 800;
  const svg = el("svg", { viewBox: `0 0 ${w} ${HEIGHT}`, role: "img" });

  const plotW = w - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const yScale = (v) => PAD.top + plotH - ((v - yLo) / (yHi - yLo)) * plotH;

  for (const t of yTicks) {
    const y = yScale(t);
    // The zero line is the axis, not a gridline — it carries the diverging baseline.
    svg.append(
      el("line", {
        x1: PAD.left, x2: PAD.left + plotW, y1: y, y2: y,
        class: t === 0 ? "axis-line" : "gridline",
      })
    );
    const label = el("text", { x: PAD.left - 8, y: y + 4, "text-anchor": "end", class: "tick-label" });
    label.textContent = nfmt(t, Math.abs(t) < 10 && t !== 0 ? 1 : 0);
    svg.append(label);
  }

  const unitText = el("text", { x: PAD.left - 8, y: PAD.top - 10, "text-anchor": "end", class: "axis-title" });
  unitText.textContent = unit;
  svg.append(unitText);

  host.append(svg);
  return { svg, w, plotW, plotH, yScale };
}

function xTickEvery(n, plotW) {
  // Keep ~8 labels max so they never collide.
  const maxLabels = Math.max(2, Math.floor(plotW / 60));
  return Math.max(1, Math.ceil(n / maxLabels));
}

/* --------------------------------------------------------------- tooltip */

const tip = $("tooltip");

function showTip(evt, title, rows) {
  tip.replaceChildren();
  const head = document.createElement("div");
  head.className = "tt-title";
  head.textContent = title; // untrusted-by-default: never innerHTML
  tip.append(head);

  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "tt-row";
    const name = document.createElement("span");
    name.className = "tt-name";
    const key = document.createElement("span");
    key.className = "tt-key";
    key.style.background = r.color;
    const text = document.createElement("span");
    text.textContent = r.name;
    name.append(key, text);
    const val = document.createElement("span");
    val.className = "tt-value";
    val.textContent = r.value;
    row.append(name, val);
    tip.append(row);
  }

  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  let x = evt.clientX + 14;
  let y = evt.clientY - box.height / 2;
  if (x + box.width > innerWidth - 8) x = evt.clientX - box.width - 14;
  y = Math.max(8, Math.min(y, innerHeight - box.height - 8));
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

const hideTip = () => { tip.hidden = true; };

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

/** Vertical hairline that snaps to the nearest sample. One tooltip lists every
    series at that X — the pointer never has to land on a line. */
function attachCrosshair(svg, rows, xScale, plotW, plotH, rowsFor, titleFor) {
  const line = el("line", { y1: PAD.top, y2: PAD.top + plotH, class: "crosshair", opacity: 0 });
  svg.append(line);

  const surface = el("rect", {
    x: PAD.left, y: PAD.top, width: plotW, height: plotH, class: "hit", tabindex: 0,
  });

  const at = (i, evt) => {
    const r = rows[i];
    if (!r) return;
    line.setAttribute("x1", xScale(i));
    line.setAttribute("x2", xScale(i));
    line.setAttribute("opacity", 1);
    showTip(evt, titleFor(r), rowsFor(r));
  };

  surface.addEventListener("pointermove", (e) => {
    // The SVG scales to its container, so map client px back into viewBox units first.
    const box = svg.getBoundingClientRect();
    const vbWidth = svg.viewBox.baseVal.width || box.width;
    const px = ((e.clientX - box.left) / box.width) * vbWidth;
    const i = Math.round(((px - PAD.left) / plotW) * (rows.length - 1));
    at(Math.max(0, Math.min(rows.length - 1, i)), e);
  });
  surface.addEventListener("pointerleave", () => {
    line.setAttribute("opacity", 0);
    hideTip();
  });

  let focusIdx = Math.floor(rows.length / 2);
  surface.addEventListener("focus", () => {
    const b = surface.getBoundingClientRect();
    at(focusIdx, { clientX: b.left + b.width / 2, clientY: b.top });
  });
  surface.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    focusIdx = Math.max(0, Math.min(rows.length - 1, focusIdx + (e.key === "ArrowRight" ? 1 : -1)));
    const b = surface.getBoundingClientRect();
    at(focusIdx, { clientX: b.left + (focusIdx / (rows.length - 1)) * b.width, clientY: b.top });
  });
  surface.addEventListener("blur", () => {
    line.setAttribute("opacity", 0);
    hideTip();
  });

  svg.append(surface);
}

/* ----------------------------------------------------------------- legend */

function renderLegend(host, items) {
  host.replaceChildren();
  for (const it of items) {
    const wrap = document.createElement("span");
    wrap.className = "legend-item";
    const sw = document.createElement("span");
    sw.className = `legend-swatch${it.line ? " line" : ""}`;
    sw.style.background = it.color;
    const label = document.createElement("span");
    label.textContent = it.name;
    wrap.append(sw, label);
    host.append(wrap);
  }
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

async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    const err = new Error(body.detail || `Request failed (${resp.status})`);
    err.status = resp.status;
    err.kind = body.error;
    throw err;
  }
  return resp.json();
}

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

function showGate(message) {
  $("app").hidden = true;
  $("gate").hidden = false;

  const cfg = state.config;
  const actions = $("gate-actions");
  actions.replaceChildren();

  if (!cfg?.configured) {
    $("gate-title").textContent = "Finish setup first";
    $("gate-body").textContent =
      "TESLA_CLIENT_ID and TESLA_CLIENT_SECRET are not set. Copy .env.example to .env, fill in your credentials from developer.tesla.com, then run `python setup_tesla.py doctor`. SETUP.md walks through it.";
  } else {
    $("gate-title").textContent = "Connect your Tesla account";
    $("gate-body").textContent =
      "Sign in with Tesla to grant read-only access to your energy site (scope: energy_device_data). Tokens are stored locally in .tokens.json and never leave this machine.";
    const btn = document.createElement("a");
    btn.className = "btn";
    btn.href = "/auth/login";
    btn.textContent = "Connect Tesla account";
    actions.append(btn);

    if (cfg.manual_callback) {
      const alt = document.createElement("a");
      alt.className = "ghost-btn";
      alt.href = "/auth/manual";
      alt.textContent = "Paste callback URL";
      actions.append(alt);
    }
  }

  const errBox = $("gate-error");
  const urlErr = new URLSearchParams(location.search).get("error");
  const text = message || (urlErr ? `Login failed: ${urlErr.replace(/_/g, " ")}` : "");
  errBox.hidden = !text;
  errBox.textContent = text;
}

/* -------------------------------------------------------------------- init */

function initTheme() {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.dataset.theme = saved;
  $("theme-toggle").addEventListener("click", () => {
    const current =
      document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("theme", next);
    render(); // charts read their hues from CSS, so re-render against the new steps
  });
}

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
  initTheme();
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
