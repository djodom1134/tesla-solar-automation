/* Hand-rolled SVG chart machinery, shared by the solar and car pages. */

import { $, nfmt } from "./shared.js";

export const SVG_NS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------------- svg */

export function el(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  return node;
}

/** Rounded only at the data end; square where it meets the baseline. */
export function barPath(x, y, w, h, r, roundTop) {
  if (h <= 0) return "";
  const rr = Math.min(r, w / 2, h);
  return roundTop
    ? `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y} L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} L${x + w},${y + h} Z`
    : `M${x},${y} L${x},${y + h - rr} Q${x},${y + h} ${x + rr},${y + h} L${x + w - rr},${y + h} Q${x + w},${y + h} ${x + w},${y + h - rr} L${x + w},${y} Z`;
}

/** Clean axis ticks that always include zero. */
export function niceTicks(min, max, count = 5) {
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

export const PAD = { top: 24, right: 20, bottom: 32, left: 52 };
export const HEIGHT = 260;

export function chartFrame(host, { yLo, yHi, yTicks, unit, width }) {
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

export function xTickEvery(n, plotW) {
  // Keep ~8 labels max so they never collide.
  const maxLabels = Math.max(2, Math.floor(plotW / 60));
  return Math.max(1, Math.ceil(n / maxLabels));
}

/* --------------------------------------------------------------- tooltip */

/* The tooltip element is resolved per call rather than at module load, so the
   module can be imported before the DOM exists. */
export function showTip(evt, title, rows) {
  const tip = $("tooltip");
  if (!tip) return;
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

export const hideTip = () => { const tip = $("tooltip"); if (tip) tip.hidden = true; };

/** Vertical hairline that snaps to the nearest sample. One tooltip lists every
    series at that X — the pointer never has to land on a line. */
export function attachCrosshair(svg, rows, xScale, plotW, plotH, rowsFor, titleFor) {
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

export function renderLegend(host, items) {
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
