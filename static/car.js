/* Car page. Reuses the solar page's chart primitives; adds a Leaflet map.

   The car is asleep most of the time, so every render runs against one of four
   states: live, snapshot (with age), needs-setup, or empty. Which one is in
   force is always visible — a stale number presented as live is worse than no
   number. */

import { $, api, COLOR, nfmt, initTheme, showGate as gate } from "./shared.js";
import {
  PAD, HEIGHT, el, niceTicks, chartFrame, showTip, hideTip, attachCrosshair,
  estimateLabelWidth, pickNonOverlappingTicks,
} from "./chart.js";

const state = {
  config: null,
  body: null,        // last /api/car/state response
  health: null,
  range: "24h",
  history: null,
  map: null,
  marker: null,
  refreshing: false,
  commands: null,
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

/* "27 Jul" -- written out manually rather than via toLocaleDateString so the
   day/month order and the month's spelling can't flip with the viewer's
   locale (en-US renders "Jul 27"); this project's own copy always reads
   "since 27 Jul". */
const SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function shortDate(epochSeconds) {
  if (epochSeconds == null) return null;
  const d = new Date(epochSeconds * 1000);
  return `${d.getDate()} ${SHORT_MONTHS[d.getMonth()]}`;
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
      loadSolar(),   // never throws -- the card just stays hidden on failure
      loadReach(),   // same: no landmarks simply means no list
      loadGarage(),  // same -- stays hidden on failure
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

/* --------------------------------------------------------------- solar */

/* The battery, horizontal. The full width is 100%: the sun's share of the
   charge, then the grid's, then the unfilled track. The sun sits at the left
   so it is always the first thing read. This is a PROPORTION of the pack,
   not a physical layer -- electrons mix (the "How these numbers work" note
   says so).

   Limit markers ride on the bar: the owner's own limit and, when the
   controller has raised it for the sun, the raised one. A raise that has
   stuck is then visible at a glance rather than discovered next month. */
function renderBattery(s) {
  const bar = $("solar-battery");
  const marks = $("solar-battery-marks");
  const legend = $("solar-battery-legend");
  bar.replaceChildren();
  marks.replaceChildren();
  legend.replaceChildren();

  const soc = s.soc;
  if (soc == null) {
    legend.textContent = "Charge unknown — the car has not been read yet.";
    return;
  }
  const clamp = (v) => Math.max(0, Math.min(100, v));
  const sun = clamp(Math.min(soc, s.banked_pct || 0));
  const grid = clamp(soc - sun);

  const seg = (cls, pct, title) => {
    if (!(pct > 0)) return;
    const d = document.createElement("span");
    d.className = `seg ${cls}`;
    d.style.width = `${pct}%`;
    d.title = title;
    bar.append(d);
  };
  seg("seg-sun", sun, `${nfmt(sun, 1)}% of the charge came from the sun`);
  seg("seg-grid", grid, `${nfmt(grid, 1)}% of the charge came from the grid`);

  // The owner's limit, and the controller's raise when there is one. The
  // raise is what makes "why is it charging past 80%?" answer itself.
  const raised = s.raised_to && s.original_limit && s.raised_to !== s.original_limit;
  const limits = raised
    ? [[s.original_limit, "your limit"], [s.raised_to, "raised for sun"]]
    : (s.limit != null ? [[s.limit, "limit"]] : []);
  for (const [pct, what] of limits) {
    const tick = document.createElement("span");
    tick.className = "sc-limit-tick";
    tick.style.left = `${clamp(pct)}%`;
    bar.append(tick);
    const m = document.createElement("span");
    m.className = "sc-mark" + (what === "raised for sun" ? " sc-mark-raised" : "")
      + (pct >= 85 ? " edge-right" : pct <= 15 ? " edge-left" : "");
    m.style.left = `${clamp(pct)}%`;
    m.textContent = `${pct}% ${what}`;
    marks.append(m);
  }

  const total = document.createElement("strong");
  total.textContent = `${soc}% charged`;
  legend.append(total, legendItem("key-solar", `Sun ${nfmt(sun, 0)}%`),
                legendItem("key-grid", `Grid ${nfmt(grid, 0)}%`));
}

function legendItem(keyClass, text) {
  const item = document.createElement("span");
  item.className = "legend-item";
  const key = document.createElement("span");
  key.className = `key ${keyClass}`;
  key.setAttribute("aria-hidden", "true");
  item.append(key, document.createTextNode(text));
  return item;
}

/* A two-part split bar -- sun, then grid -- with both values labelled
   beneath its ends, so no number depends on hovering. Used for energy,
   miles and money alike; each bar is its own 0-100% scale, never compared
   across. `fmt` renders one value. */
function renderSplit(host, sun, grid, fmt) {
  host.replaceChildren();
  const total = (sun || 0) + (grid || 0);
  if (!(total > 0)) { host.hidden = true; return; }
  host.hidden = false;
  const pct = (v) => (100 * v) / total;

  const bar = document.createElement("div");
  bar.className = "sc-split-bar";
  for (const [cls, v, name] of [["seg-sun", sun, "Sun"], ["seg-grid", grid, "Grid"]]) {
    if (!(v > 0)) continue;
    const d = document.createElement("span");
    d.className = `seg ${cls}`;
    d.style.flexGrow = String(v);
    d.title = `${name}: ${fmt(v)} (${nfmt(pct(v), 1)}%)`;
    bar.append(d);
  }

  const labels = document.createElement("div");
  labels.className = "sc-split-labels";
  labels.append(
    legendItem("key-solar", `Sun ${fmt(sun)} · ${nfmt(pct(sun), 0)}%`),
    legendItem("key-grid", `Grid ${fmt(grid)}`));
  host.append(bar, labels);
}

/* ------------------------------------------------- live banked-miles ticker

The owner asked for the number to climb in hundredths of a mile, 10-20 times a
second. The physics does not allow it: at 2.4 kW a hundredth of a mile takes
about four seconds, and even at the car's full 11.3 kW it takes nearly one. So
the display shows THOUSANDTHS, which at real charge rates moves 2-10 times a
second -- the same idea, one digit finer, and honest about it.

The server sends a rate in miles per second and the moment it was measured;
everything between polls is projected here. Two rules keep that honest:

  - the projection is only ever forward from a server anchor, so a stale tab
    cannot invent miles that the ledger never recorded;
  - when a poll lands the display EASES to the new truth instead of snapping,
    because the ledger updates in SoC steps and a jump backwards reads as a
    bug even when it is a correction.
*/
const ENGAGED = new Set(["charging", "grace"]);

const ticker = {
  anchor: null,      // miles reported by the server
  anchorAt: 0,       // performance.now() when it arrived
  rate: 0,           // miles per second
  shown: null,       // what the eye currently sees
  raf: 0,
};

function tickerStop() {
  if (ticker.raf) cancelAnimationFrame(ticker.raf);
  ticker.raf = 0;
}

function tickerFrame() {
  ticker.raf = requestAnimationFrame(tickerFrame);
  if (ticker.anchor === null) return;
  const elapsed = (performance.now() - ticker.anchorAt) / 1000;
  const target = ticker.anchor + ticker.rate * elapsed;
  // Ease toward the projection rather than assigning it: on the frame after a
  // poll this absorbs the correction over ~half a second instead of jumping.
  ticker.shown = ticker.shown === null
    ? target
    : ticker.shown + (target - ticker.shown) * 0.12;
  const el = $("solar-banked-live");
  if (el) el.textContent = ticker.shown.toFixed(3);
  reachPaint(ticker.shown);
}

function tickerSync(miles, ratePerSec) {
  ticker.anchor = miles;
  ticker.anchorAt = performance.now();
  ticker.rate = ratePerSec || 0;
  if (ticker.shown === null) ticker.shown = miles;
  if (!ticker.raf && ratePerSec > 0) tickerFrame();
  if (!(ratePerSec > 0)) {
    tickerStop();
    ticker.shown = miles;
    const el = $("solar-banked-live");
    if (el) el.textContent = miles.toFixed(3);
    reachPaint(miles);
  }
}

/* ------------------------------------------------------------ where to go */

let reachPlaces = null;

async function loadReach() {
  try {
    const r = await api("/api/car/solar/landmarks");
    reachPlaces = r.places || [];
    $("solar-reach-head").textContent = r.round_trip
      ? "Round trips it covers" : "Places within reach";
  } catch (_) {
    reachPlaces = null;
  }
  // loadSolar runs alongside this and may already have painted, against no
  // places. While the ticker is moving the next frame repaints anyway; when
  // it is still (no sun coming in) nothing would until the next poll.
  if (ticker.shown !== null) reachPaint(ticker.shown);
}

function reachPaint(miles) {
  const box = $("solar-reach");
  if (!box) return;
  if (!reachPlaces || !reachPlaces.length || !(miles > 0)) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const list = $("solar-reach-list");
  // The farthest few already in reach plus the next two, so there is always
  // something visibly approaching, and the list stays short enough to sit
  // beside the number it belongs to.
  const lit = reachPlaces.filter((p) => miles >= p.needed);
  const next = reachPlaces.filter((p) => miles < p.needed).slice(0, 2);
  const rows = lit.slice(-4).concat(next);
  list.innerHTML = "";
  for (const p of rows) {
    const reachable = miles >= p.needed;
    const li = document.createElement("li");
    li.className = reachable ? "reach-on" : "reach-off";
    const name = document.createElement("span");
    name.className = "reach-name";
    name.textContent = p.name + (p.mountain ? " \u26f0" : "");
    const dist = document.createElement("span");
    dist.className = "reach-dist";
    dist.textContent = reachable
      ? `${p.miles} mi each way`
      : `${(p.needed - miles).toFixed(1)} mi to go`;
    li.append(name, dist);
    list.append(li);
  }
}

/* Money saved against a gasoline car. Four tiles: the two MEASURED figures
   (sun, grid electricity -- since tracking began) and the two ESTIMATES
   (the car's whole life, and a year ahead), marked "est." on their labels.
   Sun and grid are never added into one measured number: the sun's miles
   cost nothing, the grid's cost electricity, and a single total would hide
   which one is doing the work. The estimates do carry a total, with the
   split drawn beneath it. */
const usd = (v, d = 2) => `${v < 0 ? "−" : ""}$${nfmt(Math.abs(v), d)}`;

function renderMoney(sv, p, since) {
  const box = $("solar-money");
  if (!sv && !p) { box.hidden = true; return; }
  box.hidden = false;
  const mpg = sv ? sv.mpg : 20;       // the projection is always at the same mpg
  $("solar-money-head").textContent = `Saved vs. a ${nfmt(mpg, 0)} mpg gas car`;

  if (sv) {
    $("money-sun").textContent = usd(sv.sun_usd);
    $("money-sun-sub").textContent =
      (sv.sun_usd_today ? `${usd(sv.sun_usd_today)} today · ` : "") + `since ${since}`;
    if (sv.grid_usd === null) {
      $("money-grid").textContent = usd(sv.grid_gas_usd);
      $("money-grid-sub").textContent = "gas only — set an import rate to net off power";
    } else {
      $("money-grid").textContent = usd(sv.grid_usd);
      $("money-grid-sub").textContent =
        `${usd(sv.grid_gas_usd)} gas − ${usd(sv.grid_electric_usd)} power`;
    }
  }

  const life = $("money-life-tile"), year = $("money-year-tile");
  life.hidden = year.hidden = !p;
  if (p) {
    const whole = (v) => usd(v, 0);
    $("money-life").textContent = whole(p.lifetime_usd);
    renderSplit($("money-life-split"), p.lifetime_sun_usd, p.lifetime_grid_usd, whole);
    const start = new Date(p.in_service_ts * 1000);
    const month = `${SHORT_MONTHS[start.getMonth()]} ${start.getFullYear()}`;
    $("money-life-sub").textContent =
      `${nfmt(p.odometer_mi, 0)} mi since ${p.in_service_basis === "configured" ? "" : "~"}${month}`;
    $("money-year").textContent = whole(p.yearly_usd);
    renderSplit($("money-year-split"), p.yearly_sun_usd, p.yearly_grid_usd, whole);
    $("money-year-sub").textContent = `at ${nfmt(p.annual_mi, 0)} mi a year`;
  }
}

async function loadSolar() {
  let s;
  try {
    s = await api("/api/car/solar/status");
  } catch (_) {
    return;                       // the card simply stays hidden
  }
  const card = $("solar-card");
  card.hidden = false;

  // WHO is driving, as its own pill. The mode and the machine's state are
  // different questions, and the state alone cannot answer the first: it
  // reads "idle" both while the controller waits for sun and while it stands
  // aside because you set your own rate.
  const mode = s.mode || (s.enabled ? s.state : "off");
  const MODE_LABEL = {
    solar: "solar", now: "charging now", manual: "manual", off: "off",
  };
  $("solar-mode").textContent = MODE_LABEL[mode] || mode;
  $("solar-mode").className = "pill mode-" + mode;
  $("solar-mode").title = {
    solar: "Charge rate is following the sun.",
    now: "Charging at full rate regardless of the sun, until midnight.",
    manual: "You set the charge rate yourself; solar control is paused.",
    off: "Solar charging is switched off in setup.",
  }[mode] || "";

  // A disabled controller that has never run also reports state:"idle" --
  // identical to an enabled-but-quiet one. Showing "off" here is the only
  // thing that tells the two apart (spec 7.4: honesty over a working-looking
  // default).
  const label = s.enabled ? s.state : "off";
  $("solar-state").textContent = label;
  $("solar-state").className = "pill state-" + label;

  // --- 1. Now: one sentence, in words, of what the car is doing.
  const spare = s.surplus_w == null ? null : `${nfmt(s.surplus_w / 1000, 2)} kW`;
  const amps = s.amps == null ? null : `${s.amps} A`;
  let now;
  if (mode === "manual") now = "You set the charge rate — solar control is paused.";
  else if (mode === "now") now = "Charging at full rate until midnight, sun or not.";
  else if (mode === "off") now = "Solar charging is switched off.";
  else if (s.state === "charging") {
    now = `Charging from the sun${amps ? ` at ${amps}` : ""}`
        + (spare ? ` — ${spare} of spare sun.` : ".");
  } else if (s.state === "grace") {
    now = `Riding out a cloud at the minimum rate${amps ? ` (${amps})` : ""}.`;
  } else if (s.surplus_w > 0 && s.start_w && s.surplus_w < s.start_w) {
    // Exporting, but not enough to run the car's slowest charge.
    now = `Waiting for more sun — ${spare} spare, `
        + `${nfmt(s.start_w / 1000, 2)} kW needed to start.`;
  } else if (s.surplus_w > 0) {
    now = `Waiting to start — ${spare} of spare sun.`;
  } else if (s.house_w != null && s.solar_w != null) {
    // The 2026-09-19 case: plenty of sun, and the house drinking all of it.
    now = `Waiting for spare sun — ${nfmt(s.solar_w / 1000, 1)} kW from the `
        + `roof, the house is using ${nfmt(s.house_w / 1000, 1)} kW.`;
  } else {
    now = "Waiting for spare sun.";
  }
  $("solar-now").textContent = now;

  // The pause says what happened and offers the way out. Both halves matter:
  // "manual" alone leaves the owner to guess what they did and how to undo
  // it, which is how a safety feature turns into a mystery.
  const ov = $("solar-override");
  ov.hidden = mode !== "manual";
  if (mode === "manual") {
    const when = s.override_since
      ? new Date(s.override_since * 1000)
          .toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
      : null;
    const rate = s.override_amps === null || s.override_amps === undefined
      ? "the charge rate" : `${s.override_amps} A`;
    $("solar-override-note").textContent =
      `You set ${rate}${when ? " at " + when : ""}. Solar control is paused `
      + "until charging is stopped and started again, or you resume it here.";
  }

  const warn = $("solar-warn");
  if (s.sun_wasted) {
    // The one the card had no words for on 2026-09-19: a full car, plugged
    // in, under an exporting sky, with the controller quietly doing nothing.
    warn.textContent = "Sun is going to the grid with the car plugged in and "
                     + "not charging. The charge limit should be raised for it "
                     + "— check the controller is enabled and not rate-limited.";
    warn.hidden = false;
  } else if (s.capped) {
    warn.textContent = "Paused: daily API request cap reached. Resumes tomorrow.";
    warn.hidden = false;
  } else if (s.dirty && !ENGAGED.has(s.state)) {
    // dirty is the NORMAL, healthy condition for the whole duration of an
    // engagement -- it is set on charge_start and cleared only on restore, so
    // showing this while the controller is actively charging alarmed the
    // owner during correct operation. It is only news once the engagement has
    // ended and the restore has still not happened.
    warn.textContent = "Your original charge settings have not been restored yet — "
                     + "waiting for the car to be home and reachable.";
    warn.hidden = false;
  } else if (s.rate_limited) {
    warn.textContent = "Tesla is rate-limiting this account — polling every "
                     + `${s.backoff_s}s until it clears, not the usual cadence.`;
    warn.hidden = false;
  } else {
    warn.hidden = true;
  }

  renderBattery(s);

  // --- 2. Sun in the battery. The STOCK ("how far on sun already in the
  // pack"), the one hero number. Two figures exist -- the car's own rated
  // range and the owner's measured consumption -- shown one at a time,
  // always labelled which, never averaged (spec 7.4).
  const lower = s.ledger_stale ? " · lower bound (a sample gap)" : "";
  if (s.banked_miles === null) {
    $("solar-banked-live").textContent = "—";
    $("solar-banked-sub").textContent = `${nfmt(s.banked_pct, 2)}% of the charge${lower}`;
    tickerStop();
  } else {
    const basis = s.banked_miles_basis === "measured"
      ? "at measured mi/kWh" : "at the car's rated range";
    $("solar-banked-sub").textContent =
      `${nfmt(s.banked_pct, 2)}% of the charge, ${basis}${lower}`;
    tickerSync(Number(s.banked_miles),
               ENGAGED.has(s.state) ? (s.accrual_mi_per_s || 0) : 0);
  }

  // --- 3. Money.
  const since = s.free_miles_since == null ? "tracking began" : shortDate(s.free_miles_since);
  renderMoney(s.savings, s.projection, since);

  // --- 4. Where the energy came from. Energy charged at home is kWh, which
  // is measured; miles driven counts every mile, Supercharged ones included.
  // Two separate bars on their own scales, never read against each other.
  $("solar-mix-head").textContent = s.free_miles_since == null
    ? "Where the energy came from"
    : `Where the energy came from · since ${since}`;
  const kwh = (v) => `${nfmt(v, 1)} kWh`;
  renderSplit($("mix-charged"), s.charged_solar_kwh, s.charged_grid_kwh, kwh);
  $("mix-charged-sub").textContent = !s.charged_solar_kwh && !s.charged_grid_kwh
    ? "Nothing recorded yet."
    : (s.charged_solar_miles == null ? ""
       : `${nfmt(s.charged_solar_miles, 1)} free miles put in`);
  // Lifetime free miles driven, promoted to its own element (2026-09-19):
  // the number that answers "how much of my driving has the sun actually
  // paid for", which was a sub-line under a bar before. The split beneath it
  // is the same lifetime mileage, sun against everything else.
  const mi = (v) => `${nfmt(v, 0)} mi`;
  if (s.tracked_miles > 0) {
    $("free-miles").textContent = nfmt(s.free_miles_driven, 1);
    renderSplit($("free-split"), s.free_miles_driven,
                Math.max(0, s.tracked_miles - s.free_miles_driven), mi);
    $("free-sub").textContent =
      `${nfmt(s.free_miles_share, 1)}% of ${nfmt(s.tracked_miles, 0)} mi`
      + (s.free_miles_since == null ? "" : ` since ${since}`);
  } else {
    $("free-miles").textContent = "—";
    renderSplit($("free-split"), 0, 0, mi);
    $("free-sub").textContent = "No miles tracked yet.";
  }

  // Honesty is the feature here, not a caveat on it: when either input is
  // still unproven, say exactly what's missing rather than blanking the
  // line or showing a confident wrong number (spec 7.4).
  let today;
  if (s.free_miles === null) {
    const missing = [];
    if (s.miles_sampled < 50) missing.push(`50 miles of driving (have ${s.miles_sampled})`);
    if (s.pack_sessions < 2) missing.push(`2 charge sessions (have ${s.pack_sessions})`);
    today = missing.length
      ? `Today: free miles still calibrating — need ${missing.join(" and ")}.`
      : "Today: free miles still calibrating.";
  } else {
    today = `Today: ${s.free_miles} free miles from ${s.solar_kwh_today} kWh of sun`;
  }
  $("solar-today").textContent =
    `${today} · ${nfmt(s.grace_import_wh_today, 0)} Wh bought riding out clouds.`;

  // --- 5. The fine print.
  renderNotes(s);
}

/* Everything that qualifies a number, in one place, so none of it has to
   interrupt the numbers themselves. */
function renderNotes(s) {
  const notes = [
    "The sun/grid split of the battery is a proportion of the charge, not a "
    + "physical layer — electrons mix, and the car has no idea which is which.",
    "Banked sun is spent first: every mile you drive comes out of it until "
    + "it is empty, and only then out of the grid share. Drive 10 miles on a "
    + "25-mile bank and 15 miles of sun are left, all 10 counted as driven free.",
  ];
  if (s.mi_per_kwh) {
    notes.push(`Miles are converted at ${s.mi_per_kwh} mi/kWh, measured from `
      + `${nfmt(s.miles_sampled, 0)} miles of this car's own driving.`);
  }
  const sv = s.savings;
  if (sv) {
    const week = shortDate(new Date(`${sv.gas_week}T12:00:00`).getTime() / 1000);
    notes.push(`Gas is regular at ${usd(sv.gas_usd_per_gal)}/gal this week `
      + `(${sv.gas_source}, week of ${week}), refreshed daily. Each charge is `
      + `priced at its own week's gas, against a ${nfmt(sv.mpg, 0)} mpg car. `
      + "The sun is counted as free.");
    if (sv.import_rate != null) {
      notes.push(`Grid electricity is costed at your import rate, `
        + `$${nfmt(sv.import_rate, 4)}/kWh.`);
    }
  }
  const p = s.projection;
  if (p) {
    const start = new Date(p.in_service_ts * 1000);
    const month = `${SHORT_MONTHS[start.getMonth()]} ${start.getFullYear()}`;
    notes.push(`The lifetime and yearly figures are estimates: `
      + `${nfmt(p.odometer_mi, 0)} mi over ${nfmt(p.years, 1)} years since ${month}`
      + (p.in_service_basis === "configured" ? "" : " (estimated from the VIN's model year)")
      + `, with the ${nfmt(100 * p.sun_share, 1)}% of miles driven on sun since tracking `
      + "began applied to the whole life. Grid miles cost "
      + `$${nfmt(p.electric_usd_per_mi, 3)}/mi at the home rate; Supercharging `
      + "costs more, so the grid saving is an upper bound.");
  }
  notes.push(`While riding out clouds the controller holds the minimum rate and `
    + `buys a little power on purpose: ${nfmt(s.grace_import_wh_total / 1000, 1)} kWh `
    + "in total so far.");
  const list = $("solar-notes");
  list.replaceChildren(...notes.map((t) => {
    const li = document.createElement("li");
    li.textContent = t;
    return li;
  }));
}

/* --------------------------------------------------------------- garage */

async function loadGarage() {
  let g;
  try {
    g = await api("/api/car/garage");
  } catch (_) {
    return;                       // the card simply stays hidden
  }
  const card = $("garage-card");
  card.hidden = false;
  const stateEl = $("garage-state");
  const obEl = $("garage-obstructed");
  const note = $("garage-note");
  const openBtn = $("garage-open");
  const closeBtn = $("garage-close");

  // Render an unreachable device as unreachable -- never a stale state
  // presented as current.
  if (!g.reachable) {
    stateEl.textContent = "unreachable";
    stateEl.className = "pill";
    obEl.hidden = true;
    note.textContent = "Could not reach the garage door opener.";
    openBtn.disabled = true;
    closeBtn.disabled = true;
    return;
  }

  const label = g.door_state || "unknown";
  stateEl.textContent = label;
  stateEl.className = "pill state-" + label.toLowerCase();
  obEl.hidden = !g.obstructed;
  note.textContent = g.light_on ? "Light is on." : "";
  openBtn.disabled = false;
  closeBtn.disabled = false;
}

async function garageCommand(action, btn) {
  // Manual button: the owner is present and just pressed it, so this acts
  // immediately -- no warning wait, unlike the scheduled close. Closing is
  // still the one irreversible-feeling direction, so it gets the same
  // confirm() the high-risk car commands use.
  if (action === "close" && !window.confirm("Close the garage door?")) return;
  btn.disabled = true;
  try {
    const result = await api(`/api/car/garage/${action}`, { method: "POST" });
    toast(result.ok ? `Garage ${action} sent` : `Garage ${action} may not have worked`,
          result.ok);
  } catch (err) {
    toast(err.message, false);
  } finally {
    await loadGarage();
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

  // The "or automated control is re-enabled from the car page" half of the
  // release condition. PUT /charge-mode already clears the latch, so there
  // is no new route behind this button.
  $("solar-resume").addEventListener("click", async () => {
    const btn = $("solar-resume");
    btn.disabled = true;
    try {
      await api("/api/car/charge-mode", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "solar" }),
      });
      toast("Solar control resumed", true);
    } catch (err) {
      toast(err.message, false);
    } finally {
      btn.disabled = false;
      await loadSolar();
    }
  });

  $("garage-open").addEventListener("click", () => garageCommand("open", $("garage-open")));
  $("garage-close").addEventListener("click", () => garageCommand("close", $("garage-close")));

  initRanges();
  await loadCommands();
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

  // X labels: dates for multi-day ranges, clock time for a single day. Real
  // samples land anywhere from 60s to 6000+s apart, so thinning "every Nth
  // row" still bunches labels wherever samples happen to cluster in time --
  // walk actual pixel positions instead and drop whichever candidate would
  // overlap the previously kept label, so no two ever collide regardless of
  // how uneven the row spacing is.
  const multiDay = span > 36 * 3600;
  const xLabel = (r) =>
    multiDay
      ? new Date(r.ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" })
      : new Date(r.ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const tickCandidates = rows.map((r, i) => {
    const label = xLabel(r);
    return { x: xScale(i), label, width: estimateLabelWidth(label) };
  });
  for (const t of pickNonOverlappingTicks(tickCandidates)) {
    const text = el("text", {
      x: t.x, y: PAD.top + plotH + 20, "text-anchor": "middle", class: "tick-label",
    });
    text.textContent = t.label;
    svg.append(text);
  }

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

/* --------------------------------------------------------------- controls */

async function loadCommands() {
  try {
    state.commands = await api("/api/car/commands");
    renderControls();
  } catch (err) {
    $("controls").textContent = err.message;
  }
}

function renderControls() {
  const host = $("controls");
  host.replaceChildren();
  const { groups, commands } = state.commands;

  const head = document.createElement("div");
  head.className = "card-head";
  const h2 = document.createElement("h2");
  h2.textContent = "Controls";
  const note = document.createElement("span");
  note.className = "muted small";
  note.id = "controls-note";
  head.append(h2, note);
  host.append(head);

  for (const group of groups) {
    const inGroup = commands.filter((c) => c.group === group);
    if (!inGroup.length) continue;

    const details = document.createElement("details");
    details.className = "control-group";
    // Charging is what the page is mostly for; the rest stay folded away.
    details.open = group === "Charging";
    const summary = document.createElement("summary");
    summary.textContent = group;
    details.append(summary);

    const grid = document.createElement("div");
    grid.className = "control-grid";
    for (const cmd of inGroup) grid.append(controlRow(cmd));
    details.append(grid);
    host.append(details);
  }
  renderControlsAvailability();
}

function controlRow(cmd) {
  const row = document.createElement("div");
  row.className = "control-row";
  row.dataset.cmd = cmd.id;

  const label = document.createElement("span");
  label.className = "control-label";
  label.textContent = cmd.label;
  row.append(label);

  const inputs = document.createElement("div");
  inputs.className = "control-inputs";
  const fields = {};

  for (const param of cmd.params) {
    let field;
    if (param.type === "enum") {
      field = document.createElement("select");
      for (const opt of param.options) {
        const o = document.createElement("option");
        o.value = String(opt.value);
        o.textContent = opt.label;
        field.append(o);
      }
      field.value = String(param.default);
    } else if (param.type === "bool") {
      field = document.createElement("select");
      for (const [v, t] of [["true", "On"], ["false", "Off"]]) {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = t;
        field.append(o);
      }
      field.value = String(param.default);
    } else if (param.type === "string") {
      field = document.createElement("input");
      // PINs are never prefilled and never echoed.
      field.type = param.secret ? "password" : "text";
      field.autocomplete = "off";
      field.placeholder = param.label;
    } else {
      field = document.createElement("input");
      field.type = "number";
      if (param.min != null) field.min = param.min;
      if (param.max != null) field.max = param.max;
      field.value = param.default ?? "";
      field.step = param.type === "float" ? "0.5" : "1";
    }
    field.setAttribute("aria-label", param.label);
    field.className = "control-field";
    fields[param.name] = field;
    inputs.append(field);
  }

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = cmd.risk === "high" ? "btn danger" : "btn";
  btn.textContent = "Send";
  btn.addEventListener("click", () => send(cmd, fields, btn));
  inputs.append(btn);

  row.append(inputs);
  return row;
}

async function send(cmd, fields, btn) {
  const payload = {};
  for (const [name, field] of Object.entries(fields)) payload[name] = field.value;

  if (cmd.confirm) {
    const asleep = state.body?.car_state !== "online";
    const extra = asleep ? "\n\nThe car is asleep — wake it first or this will fail." : "";
    if (!window.confirm(`${cmd.label}?${extra}`)) return;
  }

  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "…";
  try {
    const result = await api(`/api/car/command/${cmd.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast(result.message, result.ok);
    // The car takes a moment to reflect the change; re-read rather than guess.
    if (result.ok) setTimeout(refresh, 3000);
  } catch (err) {
    toast(err.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function renderControlsAvailability() {
  const host = $("controls");
  if (!host || !state.commands) return;
  const health = state.health;
  const v = state.body?.view;
  const note = $("controls-note");

  // One blocking reason at a time, most fundamental first — a list of four
  // problems is less actionable than the one to fix now.
  let blocked = null;
  if (health && health.proxy === false) {
    blocked = "Signing proxy is not running — start it to enable controls.";
  } else if (health && health.key_paired === false) {
    blocked = "This app's key is not paired with the car. Open tesla.com/_ak/example.com on your phone.";
  } else if (health && health.missing_scopes?.length) {
    blocked = `Missing scope: ${health.missing_scopes.join(", ")}. Reconnect your Tesla account.`;
  }

  note.textContent = blocked || (state.body?.car_state === "online"
    ? "" : "Car is asleep — commands will wake it or fail.");

  for (const row of host.querySelectorAll(".control-row")) {
    const cmd = state.commands.commands.find((c) => c.id === row.dataset.cmd);
    let disabled = Boolean(blocked);
    let reason = blocked || "";

    // Seat and wheel heaters are rejected outright unless climate is already
    // running, so the UI says so rather than letting the car refuse.
    if (!disabled && cmd.needs.includes("climate_on") && v && !v.climate_any) {
      disabled = true;
      reason = "Turn climate on first.";
    }
    if (!disabled && cmd.needs.includes("user_present") && v && !v.user_present) {
      disabled = true;
      reason = "Only works when someone is in the car.";
    }
    if (!disabled && cmd.id === "set_sentry_mode" && v && v.sentry_available === false) {
      disabled = true;
      reason = "Sentry is not available on this car right now.";
    }

    row.classList.toggle("disabled", disabled);
    row.title = reason;
    for (const control of row.querySelectorAll("button, input, select")) {
      control.disabled = disabled;
    }
  }
}
