/* Setup page: home pin + geofence, and the solar controller's configuration.
   No build step — this file is served as-is. */
import { $, api, initTheme } from "./shared.js";

const DEFAULT_CENTER = [39.8283, -98.5795]; // geographic centre of the US
let map, marker, circle;

function flash(el, message, ok) {
  el.textContent = message;
  el.className = "msg " + (ok ? "ok" : "err");
  setTimeout(() => { el.textContent = ""; }, 4000);
}

function drawGeofence(latlng, radius) {
  if (!marker) {
    marker = L.marker(latlng, { draggable: true }).addTo(map);
    marker.on("drag", () => circle.setLatLng(marker.getLatLng()));
  } else {
    marker.setLatLng(latlng);
  }
  if (!circle) {
    circle = L.circle(latlng, { radius, color: "#3b82f6", weight: 1 }).addTo(map);
  } else {
    circle.setLatLng(latlng).setRadius(radius);
  }
}

async function loadHome() {
  const body = await api("/api/car/home");
  const radius = body.home ? body.home.radius_m : 100;
  // Seed from the saved pin, else the car's current position, else nowhere
  // useful — the user drags it either way.
  const centre = body.home
    ? [body.home.latitude, body.home.longitude]
    : body.car ? [body.car.lat, body.car.lon] : DEFAULT_CENTER;

  map = L.map("map").setView(centre, body.home || body.car ? 16 : 4);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors", maxZoom: 19,
  }).addTo(map);
  drawGeofence(centre, radius);

  $("radius").value = radius;
  $("radius-out").textContent = radius + " m";
  $("classification").textContent = body.classification;
}

$("radius").addEventListener("input", (e) => {
  const r = Number(e.target.value);
  $("radius-out").textContent = r + " m";
  if (circle) circle.setRadius(r);
});

$("save-home").addEventListener("click", async () => {
  try {
    const p = marker.getLatLng();
    await api("/api/car/home", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        latitude: p.lat, longitude: p.lng, radius_m: Number($("radius").value),
      }),
    });
    const body = await api("/api/car/home");
    $("classification").textContent = body.classification;
    flash($("home-msg"), "Saved", true);
  } catch (err) {
    flash($("home-msg"), err.message, false);
  }
});

// An empty field clears the rate back to unconfigured; anything else is a
// number. Number("") is 0, which would silently turn "I have not said"
// into "export earns nothing".
function rateOf(id) {
  const raw = $(id).value.trim();
  return raw === "" ? null : Number(raw);
}

async function loadSolar() {
  const c = await api("/api/car/solar/config");
  $("enabled").checked = !!c.enabled;
  $("period").value = String(c.period_s);
  $("ceiling").value = c.soc_ceiling;
  $("ceiling-out").textContent = c.soc_ceiling + "%";
  $("raise-limit").checked = !!c.raise_limit;
  $("pause-override").checked = !!c.pause_on_override;
  $("grace").value = Math.round(c.grace_s / 60);
  $("margin").value = c.margin_w;
  $("deadband").value = c.deadband_w;
  $("ramp").value = c.ramp_a;
  $("mina").value = c.min_a;
  $("cap").value = c.daily_request_cap;
  $("dsoc").value = c.deadline_soc === null ? "" : c.deadline_soc;
  $("dhour").value = c.deadline_hour === null ? "" : c.deadline_hour;
  $("gracewh").value = c.grace_budget_wh;
  // Blank means "not configured", which suppresses money figures. A
  // configured 0 is a real rate and must still render as 0, not blank.
  $("imp-rate").value = c.import_rate === null ? "" : c.import_rate;
  $("exp-rate").value = c.export_rate === null ? "" : c.export_rate;
}

$("ceiling").addEventListener("input", (e) => {
  $("ceiling-out").textContent = e.target.value + "%";
});

$("save-solar").addEventListener("click", async () => {
  try {
    await api("/api/car/solar/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: $("enabled").checked ? 1 : 0,
        period_s: Number($("period").value),
        soc_ceiling: Number($("ceiling").value),
        raise_limit: $("raise-limit").checked ? 1 : 0,
        pause_on_override: $("pause-override").checked ? 1 : 0,
        grace_s: Number($("grace").value) * 60,
        margin_w: Number($("margin").value),
        deadband_w: Number($("deadband").value),
        ramp_a: Number($("ramp").value),
        min_a: Number($("mina").value),
        daily_request_cap: Number($("cap").value),
        grace_budget_wh: Number($("gracewh").value),
        import_rate: rateOf("imp-rate"),
        export_rate: rateOf("exp-rate"),
      }),
    });
    flash($("solar-msg"), "Saved", true);
  } catch (err) {
    flash($("solar-msg"), err.message, false);
  }
});

async function loadGarage() {
  const c = await api("/api/car/garage/config");
  $("garage-url").value = c.garage_url || "";
  $("garage-auto-open").checked = !!c.garage_auto_open;
  $("garage-ring").value = c.garage_ring_m;
  $("garage-close-hour").value = c.garage_close_hour === null ? "" : c.garage_close_hour;
  $("garage-warn").value = c.garage_close_warn_s;
}

$("garage-test").addEventListener("click", async () => {
  const msg = $("garage-test-msg");
  const url = $("garage-url").value.trim();
  if (!url) { flash(msg, "Enter a URL first", false); return; }
  try {
    // Read-only: this only ever GETs /status.json, never a command.
    const r = await api("/api/car/garage/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (r.reachable) {
      flash(msg, `Reachable — door is ${r.door_state}${r.obstructed ? " (obstructed)" : ""}`, true);
    } else {
      flash(msg, "Could not reach the device at that URL", false);
    }
  } catch (err) {
    flash(msg, err.message, false);
  }
});

$("save-garage").addEventListener("click", async () => {
  try {
    await api("/api/car/garage/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        garage_url: $("garage-url").value.trim() || null,
        garage_auto_open: $("garage-auto-open").checked ? 1 : 0,
        garage_ring_m: Number($("garage-ring").value),
        garage_close_hour: $("garage-close-hour").value === ""
          ? null : Number($("garage-close-hour").value),
        garage_close_warn_s: Number($("garage-warn").value),
      }),
    });
    flash($("garage-msg"), "Saved", true);
  } catch (err) {
    flash($("garage-msg"), err.message, false);
  }
});

$("save-deadline").addEventListener("click", async () => {
  try {
    const soc = $("dsoc").value === "" ? null : Number($("dsoc").value);
    const hour = $("dhour").value === "" ? null : Number($("dhour").value);
    await api("/api/car/solar/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deadline_soc: soc, deadline_hour: hour }),
    });
    flash($("deadline-msg"), "Saved", true);
  } catch (err) {
    flash($("deadline-msg"), err.message, false);
  }
});

(async function init() {
  initTheme();
  // Each load is caught independently, so a failure in one does not mask
  // whether the other succeeded -- disabling only the Save buttons whose
  // form is actually showing stale HTML defaults. Without this, a failed
  // load leaves the page looking normal and clicking Save writes those
  // defaults over the owner's real settings.
  let homeOk = true;
  let solarOk = true;
  let garageOk = true;
  try {
    await loadHome();
  } catch (err) {
    console.error(err);
    homeOk = false;
  }
  try {
    await loadSolar();
  } catch (err) {
    console.error(err);
    solarOk = false;
  }
  try {
    await loadGarage();
  } catch (err) {
    console.error(err);
    garageOk = false;
  }

  if (!homeOk || !solarOk || !garageOk) {
    const parts = [];
    if (!homeOk) parts.push("home");
    if (!solarOk) parts.push("solar charging");
    if (!garageOk) parts.push("garage");
    const banner = $("setup-error");
    if (banner) {
      banner.textContent =
        `Could not load your current ${parts.join(" and ")} settings from the ` +
        "server. Saving is disabled so the form's placeholder values cannot " +
        "overwrite yours -- reload this page once the server is reachable.";
      banner.hidden = false;
    }
    if (!homeOk) $("save-home").disabled = true;
    if (!solarOk) {
      // The deadline fields are populated by loadSolar() too, so a failed
      // load leaves them at HTML placeholders as well.
      $("save-solar").disabled = true;
      $("save-deadline").disabled = true;
    }
    if (!garageOk) $("save-garage").disabled = true;
  }
})();
