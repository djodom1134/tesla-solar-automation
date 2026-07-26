/* Car page. Reuses the solar page's chart primitives; adds a Leaflet map.

   The car is asleep most of the time, so every render runs against one of four
   states: live, snapshot (with age), needs-setup, or empty. Which one is in
   force is always visible — a stale number presented as live is worse than no
   number. */

import { $, api, COLOR, nfmt, initTheme, showGate as gate } from "./shared.js";
import { PAD, HEIGHT, el, niceTicks, chartFrame, showTip, hideTip, attachCrosshair }
  from "./chart.js";

const state = {
  config: null,
  body: null,        // last /api/car/state response
  health: null,
  range: "24h",
  history: null,
  map: null,
  marker: null,
  refreshing: false,
};

const RANGES = [
  { key: "24h", label: "24h" },
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
  { key: "90d", label: "90 days" },
  { key: "all", label: "All" },
];

/* Wire units, converted only at render time. gui_settings never changes the payload. */
const miles = (v) => (v == null ? "—" : `${nfmt(v, 0)} mi`);
const celsius = (v) => (v == null ? "—" : `${nfmt(v, 0)}°C`);
const psi = (bar) => (bar == null ? "—" : `${nfmt(bar * 14.5038, 0)} psi`);

function ago(seconds) {
  if (seconds == null) return "an unknown time ago";
  if (seconds < 90) return "just now";
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}

function toast(message, ok = true) {
  const box = $("toast");
  box.textContent = message;
  box.classList.toggle("bad", !ok);
  box.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { box.hidden = true; }, 4000);
}

/* -------------------------------------------------------------- hero/tiles */

function renderState(body) {
  state.body = body;
  const v = body.view;

  $("car-name").textContent = v?.name || "Car";
  $("wake").hidden = body.car_state === "online";

  // Staleness is surfaced, never hidden. A snapshot presented as live is a lie.
  const stale = $("stale-bar");
  if (body.source === "snapshot") {
    stale.hidden = false;
    stale.textContent =
      `Car is ${body.car_state}. Showing the last reading from ${ago(body.age_seconds)}.`;
  } else if (body.source === "none") {
    stale.hidden = false;
    stale.textContent =
      `Car is ${body.car_state} and we have no stored reading yet. Wake it to see live data.`;
  } else {
    stale.hidden = true;
  }
  $("car-status").textContent =
    body.source === "live" ? "Live" : body.source === "snapshot" ? ago(body.age_seconds) : "";

  if (!v) return;

  // Hero ring. r=52 -> circumference 326.7.
  const C = 2 * Math.PI * 52;
  const soc = v.soc ?? 0;
  const fill = $("ring-fill");
  fill.style.strokeDasharray = `${(soc / 100) * C} ${C}`;
  fill.classList.toggle("charging", !!v.charging);

  // The limit is a notch on the ring, so "how much further will it charge" is
  // readable at a glance rather than by comparing two numbers.
  const limit = $("ring-limit");
  if (v.limit == null) {
    limit.style.display = "none";
  } else {
    limit.style.display = "";
    limit.style.strokeDasharray = `1.5 ${C}`;
    limit.style.strokeDashoffset = `${-(v.limit / 100) * C}`;
  }

  $("hero-soc").textContent = v.soc ?? "—";
  $("hero-soc-label").textContent = headline(v);
  $("hero-sub").textContent = subhead(v);
  $("hero-range").textContent = miles(v.range_mi);
  $("hero-limit").textContent = v.limit == null ? "—" : `${v.limit}%`;
  $("hero-odo").textContent = miles(v.odometer_mi);

  renderTiles(v);
  renderMap(v);
  renderControlsAvailability();
}

function headline(v) {
  if (v.charging) return `Charging at ${v.charge_power_kw ?? "—"} kW`;
  if (v.charging_state === "Complete") return "Charge complete";
  if (v.shift && v.shift !== "P") return `Driving · ${v.speed_mph ?? 0} mph`;
  if (v.plugged_in) return "Plugged in, not charging";
  return "Parked";
}

function subhead(v) {
  if (v.charging && v.minutes_to_full) {
    const h = Math.floor(v.minutes_to_full / 60);
    const m = v.minutes_to_full % 60;
    const eta = h ? `${h} h ${m} min` : `${m} min`;
    return `${eta} to ${v.limit}%  ·  ${nfmt(v.energy_added_kwh, 1)} kWh added`;
  }
  if (v.usable_soc != null && v.soc != null && v.usable_soc < v.soc) {
    return `${v.usable_soc}% usable — the pack is cold`;
  }
  return v.version ? `Software ${v.version}` : "";
}

function renderTiles(v) {
  const host = $("tiles");
  host.replaceChildren();
  const tiles = [
    ["Inside", celsius(v.inside_c)],
    ["Outside", celsius(v.outside_c)],
    ["Locked", v.locked == null ? "—" : v.locked ? "Yes" : "No"],
    ["Sentry", v.sentry == null ? "—" : v.sentry ? "On" : "Off"],
    // The closest thing to a camera the API offers. There is no footage endpoint.
    ["Dashcam", v.dashcam || "—"],
    ["Charge port", v.port_open == null ? "—" : v.port_open ? "Open" : "Closed"],
  ];

  const openDoors = Object.entries(v.doors || {}).filter(([, open]) => open);
  const openWindows = Object.entries(v.windows || {}).filter(([, open]) => open);
  const openTrunks = Object.entries(v.trunks || {}).filter(([, open]) => open);
  const openings = openDoors.length + openWindows.length + openTrunks.length;
  tiles.push(["Open", openings ? String(openings) : "All closed"]);

  const lowTyre = Object.entries(v.tpms_warn || {}).find(([, warn]) => warn);
  if (Object.keys(v.tpms_bar || {}).length) {
    tiles.push(["Tyres", lowTyre ? `Low: ${lowTyre[0].toUpperCase()}` : "OK"]);
  }

  for (const [label, value] of tiles) {
    const tile = document.createElement("div");
    tile.className = "live-tile";
    const l = document.createElement("span");
    l.className = "tile-label";
    l.textContent = label;
    const val = document.createElement("span");
    val.className = "tile-value";
    val.textContent = value;
    tile.append(l, val);
    host.append(tile);
  }
}

/* --------------------------------------------------------------------- map */

function renderMap(v) {
  const note = $("map-note");
  if (v.lat == null || v.lon == null) {
    note.textContent = "Location unavailable — needs the vehicle_location scope.";
    $("map").classList.add("empty");
    return;
  }
  note.textContent = v.route
    ? `Navigating to ${v.route.destination || "destination"} · ${Math.round(v.route.minutes)} min`
    : "";
  $("map").classList.remove("empty");

  if (!state.map) {
    state.map = L.map("map", { zoomControl: true, attributionControl: true })
      .setView([v.lat, v.lon], 15);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(state.map);
    // Leaflet resolves its default icons relative to the CSS by default.
    L.Icon.Default.prototype.options.imagePath = "/vendor/images/";
    state.marker = L.marker([v.lat, v.lon]).addTo(state.map);
  } else {
    state.marker.setLatLng([v.lat, v.lon]);
    state.map.setView([v.lat, v.lon], state.map.getZoom());
  }

  state.marker.bindPopup(
    v.shift && v.shift !== "P" ? `Moving · ${v.speed_mph ?? 0} mph` : "Parked"
  );
  // A map created inside a hidden element measures zero; recompute once shown.
  setTimeout(() => state.map.invalidateSize(), 0);
}

/* ------------------------------------------------------------------- boot */

async function refresh() {
  // The 60s poll, the post-wake retry, and a slow prior request can all land
  // close together. Without this, an older response can resolve last and
  // overwrite a newer render with stale data.
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const [body, health] = await Promise.all([
      api("/api/car/state"),
      api("/api/car/health"),
    ]);
    state.health = health;
    $("error-bar").hidden = true;
    renderState(body);
    renderFooter(health);
  } catch (err) {
    if (err.status === 401) { showGate(err.message); return; }
    $("error-bar").hidden = false;
    $("error-bar").textContent = err.message;
  } finally {
    state.refreshing = false;
  }
}

function renderFooter(health) {
  const c = health.collector;
  $("foot-collector").textContent = c.running
    ? `Collector running · last sample ${ago(Math.floor(Date.now() / 1000) - c.last_sample)}`
    : "Collector not running — SoC history has gaps";
  $("foot-calls").textContent = `${health.calls_today} samples in 24 h`;
  $("foot-version").textContent = state.body?.view?.version
    ? `Software ${state.body.view.version}` : "";
}

function showGate(message) {
  $("car-app").hidden = true;
  $("gate").hidden = false;
  gate(state.config, message);
}

async function main() {
  initTheme(() => renderSoc());
  state.config = await api("/api/config");
  if (!state.config.configured || !state.config.authenticated) { showGate(); return; }

  $("gate").hidden = true;
  $("car-app").hidden = false;

  $("wake").addEventListener("click", async () => {
    $("wake").disabled = true;
    try {
      await api("/api/car/wake", { method: "POST" });
      toast("Wake sent — the car takes up to a minute to come online.");
      setTimeout(refresh, 15000);
    } catch (err) {
      toast(err.message, false);
    } finally {
      $("wake").disabled = false;
    }
  });

  initRanges();
  await refresh();
  await loadHistory();

  // Live values go stale fast. This is a read against our own snapshot when the
  // car is asleep, so it costs nothing while parked.
  setInterval(() => { if (!document.hidden) refresh(); }, 60_000);
  addEventListener("resize", () => renderSoc());
}

main().catch((err) => showGate(err.message));

/* ------------------------------------------------------------ range picker */

function initRanges() {
  const host = $("soc-range");
  host.replaceChildren();
  for (const r of RANGES) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.role = "tab";
    btn.textContent = r.label;
    btn.setAttribute("aria-selected", String(r.key === state.range));
    btn.addEventListener("click", () => {
      state.range = r.key;
      for (const b of host.children) b.setAttribute("aria-selected", String(b === btn));
      loadHistory();
    });
    host.append(btn);
  }
}

async function loadHistory() {
  try {
    state.history = await api(`/api/car/history?range=${state.range}`);
    renderSoc();
  } catch (err) {
    $("soc-note").textContent = err.message;
  }
}

/* ------------------------------------------------------------- SoC chart */

function renderSoc() {
  const host = $("soc-chart");
  const data = state.history;
  if (!host) return;

  if (!data || !data.rows.length) {
    host.replaceChildren();
    const since = data?.since;
    $("soc-note").textContent = since
      ? "No samples in this range."
      : "No history yet — the collector starts recording from now on.";
    return;
  }

  const rows = data.rows;
  const socs = rows.map((r) => r.soc);
  // SoC is a percentage; anchoring to 0-100 stops a flat day looking dramatic.
  const lo = Math.max(0, Math.min(...socs) - 10);
  const hi = Math.min(100, Math.max(...socs) + 10);
  const { ticks } = niceTicks(lo, hi, 4);
  const { svg, plotW, plotH, yScale } =
    chartFrame(host, { yLo: lo, yHi: hi, yTicks: ticks, unit: "%" });

  const t0 = rows[0].ts;
  const span = Math.max(1, rows[rows.length - 1].ts - t0);
  const xScale = (i) => PAD.left + ((rows[i].ts - t0) / span) * plotW;

  // Charging stretches get a subtle band, so "why did it go up" is answered
  // without reading the tooltip.
  let runStart = null;
  rows.forEach((r, i) => {
    if (r.charging && runStart === null) runStart = i;
    if ((!r.charging || i === rows.length - 1) && runStart !== null) {
      const x1 = xScale(runStart);
      const x2 = xScale(i);
      if (x2 - x1 > 1) {
        svg.append(el("rect", {
          x: x1, y: PAD.top, width: x2 - x1, height: plotH,
          fill: COLOR("solar"), opacity: 0.12,
        }));
      }
      runStart = null;
    }
  });

  // Two paths: measured (solid) and inferred-across-sleep (dashed).
  let solid = "";
  let dashed = "";
  rows.forEach((r, i) => {
    const x = xScale(i);
    const y = yScale(r.soc);
    if (i === 0) { solid += `M${x},${y}`; return; }
    if (r.gap) {
      const px = xScale(i - 1);
      const py = yScale(rows[i - 1].soc);
      dashed += `M${px},${py}L${x},${y}`;
      solid += `M${x},${y}`;
    } else {
      solid += `L${x},${y}`;
    }
  });

  if (dashed) {
    svg.append(el("path", {
      d: dashed, fill: "none", stroke: COLOR("battery"), "stroke-width": 2,
      "stroke-dasharray": "4 4", opacity: 0.45,
    }));
  }
  svg.append(el("path", {
    d: solid, fill: "none", stroke: COLOR("battery"), "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));

  // X labels: dates for multi-day ranges, clock time for a single day.
  const multiDay = span > 36 * 3600;
  const step = Math.max(1, Math.ceil(rows.length / Math.max(2, Math.floor(plotW / 70))));
  rows.forEach((r, i) => {
    if (i % step !== 0) return;
    const d = new Date(r.ts * 1000);
    const label = multiDay
      ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
      : d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    const text = el("text", {
      x: xScale(i), y: PAD.top + plotH + 20, "text-anchor": "middle", class: "tick-label",
    });
    text.textContent = label;
    svg.append(text);
  });

  attachCrosshair(
    svg, rows, xScale, plotW, plotH,
    (r) => {
      const out = [{ name: "Charge", value: `${r.soc}%`, color: COLOR("battery") }];
      // usable and displayed SoC diverge when the pack is cold; showing both on
      // the line would read as a rendering bug, so it lives here.
      if (r.usable_soc != null && r.usable_soc !== r.soc) {
        out.push({ name: "Usable", value: `${r.usable_soc}%`, color: COLOR("export") });
      }
      if (r.charging) out.push({ name: "Charging", value: "yes", color: COLOR("solar") });
      if (r.gap) out.push({ name: "Gap", value: "car asleep", color: COLOR("baseline") });
      return out;
    },
    (r) => new Date(r.ts * 1000).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    })
  );

  const since = data.since ? new Date(data.since * 1000) : null;
  const gaps = rows.filter((r) => r.gap).length;
  $("soc-note").textContent =
    (since ? `Recording since ${since.toLocaleDateString()}. ` : "") +
    (gaps ? `Dashed segments are periods the car was asleep (${gaps}).` : "");
}

/* Temporary no-op stub. Task 12 (controls) replaces renderControlsAvailability.
   Keep this task independently runnable until then — this is not dead code. */
function renderControlsAvailability() {}
