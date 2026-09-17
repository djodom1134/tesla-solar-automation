/* Shared across the solar and car pages. */

export const $ = (id) => document.getElementById(id);

/* Entity -> color. One entity keeps its hue across every chart. Read from CSS
   so the light/dark steps stay defined in exactly one place. */
export const COLOR = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();

export const nfmt = (v, d = 1) =>
  (v ?? 0).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

export async function api(path, opts) {
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

/* Charts read their hues from CSS, so a theme flip has to re-render them. */
export function initTheme(onChange) {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.dataset.theme = saved;
  const btn = $("theme-toggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const current =
      document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("theme", next);
    onChange?.();
  });
}

/* -------------------------------------------------------------------- gate */

/* Populates the gate card's contents only. Each page's own wrapper is
   responsible for showing #gate and hiding its app container — the two pages
   don't share a container id (#app vs #car-app), so this function must not
   guess at one. */
export function showGate(config, message) {
  const cfg = config;
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
