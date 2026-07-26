# Solar import/export dashboard

A local dashboard for grid import and export on a Tesla solar / Powerwall system, built
on the [Tesla Fleet API](https://developer.tesla.com/docs/fleet-api/endpoints/energy).

Read-only: it requests the `energy_device_data` scope and never issues a command.

```bash
pip install -r requirements.txt
DEMO=1 python app.py      # -> http://localhost:8000, synthetic data, no Tesla account
```

Then, for real data:

```bash
cp .env.example .env      # fill in credentials — see SETUP.md
python setup_tesla.py keys && python setup_tesla.py register
python app.py
```

New to Fleet API? **[SETUP.md](SETUP.md)** — Tesla requires partner registration and a
domain-hosted public key before any call works, even for your own hardware. It's about
20 minutes. `DEMO=1` exists so you can see what you're signing up for first; it emits
Tesla's raw watt-hour schema through the identical derivation path, so what you see is
what you'll get.

## What you get

- **Net grid position** as the headline number — exported minus imported, for the period.
- **Live strip** — solar, home, grid, and battery power right now, with the grid tile
  naming its direction rather than making you decode a sign.
- **Grid import & export** — a diverging chart around zero: exported above the line,
  imported below.
- **Where your home's energy came from** — battery / solar / grid, stacked to consumption.
- **Self-sufficiency** — the share of consumption that never touched the grid.
- Today · week · month · year · lifetime; optional cost/credit if you set your rates.
- Table view, keyboard-navigable charts, and a dark mode with its own selected colors.

`Today` plots the intraday **power** feed (kW), because Tesla's energy history returns a
single bucket for a single day. Every other period plots **energy** (kWh).

## Layout

| File | |
|---|---|
| `app.py` | FastAPI: OAuth routes + one combined `/api/dashboard` per view |
| `tesla.py` | Fleet API client — token rotation, refresh-on-401, TTL cache |
| `energy.py` | Turns Tesla's source/destination flow fields into import/export metrics |
| `setup_tesla.py` | `keys` · `register` · `verify` · `doctor` |
| `static/` | The dashboard — vanilla JS, hand-rolled SVG charts, no build step |

## Notes on the data

Tesla reports every flow split by source and destination, which is the only reason a
truthful import/export view is possible:

```
grid_energy_imported                    total pulled from the grid
  ├─ consumer_energy_imported_from_grid   ...of which went to the house
  └─ battery_energy_imported_from_grid    ...of which charged the battery

grid_energy_exported_from_solar         sent out, by origin
grid_energy_exported_from_battery
```

There is no single "exported" total — `energy.py` sums it by origin. And
`grid_energy_imported` already includes the battery's share, so adding it to the
consumer figure double-counts. Values arrive in watt-hours and are converted to kWh.

Two gotchas that cost real debugging time:

- **`end_date` must never be midnight.** A range ending `00:00:00` returns all-zero
  energy values. The client always anchors to `23:59:59`.
- **Percentages are recomputed from totals, never averaged.** Averaging per-day
  self-sufficiency would weight a cloudy 2 kWh day the same as a sunny 40 kWh one.

Multi-Powerwall sites have a [known upstream quirk](https://github.com/teslamotors/vehicle-command/issues/184)
where grid import/export can read high. That's Tesla's number, not a bug here — the
table view shows exactly what the API returned.

## Security

`.env`, `.tokens.json`, and `keys/` are gitignored. Tokens are written `0600` and stay
on your machine — the app talks only to Tesla. Refresh tokens are single-use and rotate
on every exchange, so the new one is persisted atomically *before* it's used; losing
that write means re-authorizing.
