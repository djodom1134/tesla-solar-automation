# Whole-House Solar Allocation — Design

**Date:** 2026-07-27
**Supersedes nothing.** Extends `2026-07-26-solar-aware-charging-design.md`.

**Goal:** House stays within a comfort band (hard constraint); subject to that,
maximise solar energy delivered into the car (objective).

---

## 1. The measured baseline

Fourteen days of metered `calendar_history`, 2026-07-13 → 07-26. Every number
here is **[M] measured**, not modelled.

| | mean/day |
|---|---|
| Solar produced | 42.4 kWh |
| Total consumed (house + car) | 90.0 kWh |
| Grid imported | 62.4 kWh |
| Grid exported | 14.9 kWh |
| Exported 09:00–19:00 | 13.9 kWh |

**The site is a net importer on all 14 days.** Consumption is more than double
production.

Export splits sharply by occupancy:

| | in-window export | free-mile ceiling |
|---|---|---|
| Owner home (load > 60 kWh, 11 days) | **8.9 kWh/day** | ~31 mi/day |
| Owner away (3 days) | **32 kWh/day** | ~112 mi/day, car absent |

### 1.1 The energy-conservation constraint

Deferring the air conditioner **does not create solar energy**. The array
produced what it produced; the house still needs the cooling later. Total daily
import is `load − solar` and shifting load within the day does not move it.

Under this site's **flat-rate import with no TOU and no storage**, which load
consumes the solar is economically irrelevant. Every "free solar mile" this
project creates is worth approximately **$0**.

This is not a reason not to build it — the owner's goal is explicitly a ledger
goal ("as many free solar miles as possible"), and `green.py` already keeps that
ledger. It is a reason to **build the cheap items first and the expensive item
last**. The owner has reviewed this finding and elected to proceed with the full
scope including setpoint writes.

**Open, unanswered, and worth one bill:** the **export credit**. If full-retail
net metering applies, the dollar value of the entire project is zero and only
the ledger remains. `IMPORT_RATE_PER_KWH` and `EXPORT_RATE_PER_KWH` are unset.

---

## 2. Verified defects in shipped code

Both reproduced against the running collector before being written down.

### D1 — The anchor (Critical)

`collector.py:279`:

```python
current_a = view.get("amps_actual") or view.get("charge_amps") or tun.min_a
```

When the car is not charging, `amps_actual` is `0` — falsy — so the control law
anchors to `charge_amps`, the owner's standing **48 A**. `control()` then returns
`clamp(48 + step, 5, 48) = 48`, and the write is suppressed by the
already-holds-this-value guard at `collector.py:372`, so the car simply resumes
at 48 A.

Reproduced with 6 kW of surplus (`scratchpad/verify_anchor.py`):

```
tick 2  charge_start            car resumes at 48 A
tick 3  set_charging_amps(40)   grid +5.28 kW
tick 4  set_charging_amps(32)   grid +3.40 kW
tick 5  set_charging_amps(25)   grid +1.52 kW
tick 6  settles 25 A            grid −0.12 kW    (ideal 26 A)

imported over 9 ticks: 340 Wh
```

**~340 Wh of grid import on every engagement**, on the feature whose stated
purpose is importing zero. The adopt path is unaffected (Task 21 gave it a
down-bypass); this is specifically `idle → charge_start`, which had never run
against the real car.

### D2 — Stale-view phantom charging (Important)

`plugged` derives from `vehicle_data`, cached up to
`view_refresh_ticks × period_s` = **600 s**. Observed live 2026-07-27:

```
13:04:41  car actually Disconnected
13:06:30  command charge_start refused: car could not execute command: disconnected
13:06:30  state=charging      ← on a stale view
13:12:32  state=idle          ← fresh view finally arrived
```

The machine held `charging` for ~6 minutes against an unplugged car and spent one
billed command. Harmless this time; the same staleness on the tick *after* an
engagement would let a stale `amps_actual=0` collapse a just-commanded rate.

---

## 3. The model

### 3.1 Inputs and provenance

**[M]** measured · **[D]** derived · **[C]** configured · **[X] unobtainable —
gate on its absence, never infer it.**

```
grid_w        [M]  live_status, 60 s refresh, >0 = import.  THE ONLY EXACT SIGNAL.
solar_w       [M]  live_status, 10-30 s refresh.
load_power    [D]  Tesla-derived, identically solar+grid on a battery-less site.
                   NOT a third channel. Never treat as metered.
car_w         [D]  amps_actual x volts, self-reported, stale <= 600 s.
                   Display and attribution only. NEVER enters the control law.
house_w       [D]  solar_w + grid_w - car_w. Carries both channels' skew;
                   usable for a 6 kW step only with 2-tick confirmation.
hvac          [M]  Nest ThermostatHvac.status {OFF,HEATING,COOLING} (Tier 2).
T_in, T_sp    [M]  Nest traits (Tier 2).
T_BASE_F      [C]  owner's normal cool setpoint. CANNOT be read - no schedule trait.
OCCUPANT_CLASS[C]  {normal, vulnerable}. BLOCKING: unanswered != permission.

per-circuit AC power              [X] no submetering
site-side car power channel       [X] live_status.wall_connectors == []
Nest schedule / hold / setpoint TTL [X] no trait exists
setpoint provenance               [X] our write, the dial, the app, and the
                                      schedule are indistinguishable
occupancy                         [X] no sensor exists or can exist
```

`samples.inside_temp` / `outside_temp` are the **car's** sensors, not the
house's. Never use them for the thermal model.

### 3.2 Regime table

Bands from `grid_w` alone, no disaggregation:
**IMPORT** `grid_w > +250 W` · **SUB-FLOOR** `0 < export_w < 1275 W` ·
**ABSORBABLE** `export_w >= 1275 W` with headroom · **ORPHANED** `export_w > 0`,
no headroom.

| # | Car | Surplus | CAR action | AC action |
|---|---|---|---|---|
| 1 | present & hungry | IMPORT, `hvac==COOLING` | **hold `min_a`, never stop**, freeze breach counters | drift ceiling allowed |
| 2 | present & hungry | IMPORT, `hvac==OFF` | servo down → grace → stopped | return `T_BASE` |
| 3 | present & hungry | SUB-FLOOR | hold `min_a`, accept <1.2 kW import | return `T_BASE` |
| 4 | present & hungry | ABSORBABLE | servo normally | return `T_BASE` |
| 5 | present, at limit | ABSORBABLE/ORPHANED | `raise_decision()` first, then servo | `T_BASE` |
| 6 | present, full/unplugged | ORPHANED | nothing available | `T_BASE`, **no pre-cool** |
| 7 | absent | ORPHANED (33 kWh/day) | nothing available | `T_BASE`, **no pre-cool** |
| 8 | any | night / no export | off-window | `T_BASE`, no night pre-cool |
| 9 | any | `location == unknown` | **freeze, no commands** | return `T_BASE` |
| 10 | any | Nest stale >10 min | unaffected — car runs on `grid_w` | restore `T_BASE` if dirty |

**Rows 6 and 7 hold nearly all the surplus and are exactly where thermal action
buys zero miles.** Row 1 is the only cell where the thermostat earns anything,
and its dominant win — the ride-through — needs no setpoint write at all.

---

## 4. Scope, in build order

Sequencing is forced: Tier 3 cannot exist without Tier 2.

### Tier 0 — defect fixes (no new capability)

- **T0.1** Anchor `current_a = amps_actual if charging_state in LIVE_CHARGING_STATES else 0`.
- **T0.2** On `idle → charging`, write `decision.unramped_target_a` open-loop
  rather than ramping. Justified because the measurement was clean: with the car
  at 0 A, `grid_w` measures the house exactly. **This is the owner's request #1**
  — and `idle` already *is* zero draw, so it costs no command, no contactor
  cycle, and no wake.
- **T0.3** Settle guard: one tick after an engagement, suppress writes and breach
  counting, so a stale `amps_actual=0` cannot collapse the rate just commanded.
- **T0.4** Refuse `charge_start` when `plugged` derives from a view older than
  `period_s × 2`; force a view refresh on the engagement tick instead.

### Tier 1 — ride-through (no thermostat, largest win)

- **T1.1** In `charging`/`grace`, when the import is present but the car is
  already at `min_a`, **hold** rather than progressing to `stopped`, bounded by
  BOTH an energy budget (`grace_budget_wh`, default 150 Wh) and the existing
  `grace_s` time cap. Neither bound alone is sufficient: time alone charges the
  same for a 100 W dip and an 1100 W dip; energy alone is unbounded against a
  compressor that runs for hours.
- **T1.2** Asymmetric ramp: downward corrections bypass `ramp_a` everywhere, not
  only on adopt. Reducing draw is always safe.
- **T1.3** After a ride-through, do not serve the full `restart_hold_s`. Measured
  2026-07-27: the compressor stopped at 13:00 and 4.7 kW exported while the car
  sat at 0 A waiting out the lockout.

Recomputed honestly, T1 is worth **~3.5:1** (spend ~0.12 kWh holding at 5 A
through a ~6 min cycle, save ~0.42 kWh of export otherwise missed during the
lockout) — not the 20:1 first claimed, which rested on a mis-scoped window.

### Tier 2 — Nest, read-only

- Device Access Console registration ($5 one-time), GCP project, OAuth.
  **Consent screen must be "In production" or the refresh token expires every
  7 days.**
- Read `ThermostatHvac.status`, `Temperature.ambientTemperatureCelsius`,
  `ThermostatTemperatureSetpoint.coolCelsius`, `Connectivity`.
- Pub/Sub push primary, `devices.get` poll backstop (10 QPM project cap).
- Feed-forward only: the car loop anticipates compressor cycles. **Zero writes.**
- Degrade to "do nothing" on any Nest failure — never to "stuck".

### Tier 3 — Nest setpoint raise (owner-elected; all the risk lives here)

Writes UP only, inside the envelope in §5.

### Tier 4 — away-day plug-in reminder

Notify when tomorrow forecasts sunny, the car is home, and unplugged. Targets the
32 kWh/day exported on away days — the largest single opportunity measured, and
independent of everything above.

---

## 5. The safety envelope

### 5.1 Invariants that hold with the daemon DEAD

Properties of the **written value**, not of any running process. They survive a
crash, a closed lid, an internet outage, and a token expiry — failures that kill
the daemon and any watchdog simultaneously, since they share a token file.

| Invariant | Value |
|---|---|
| `HARD_MAX_WRITE` | **78.0 °F**, and never above `T_BASE + 3` |
| `HARD_MIN_WRITE` | **`T_BASE`** — never writes a cool setpoint below the owner's baseline, ever |
| `ThermostatMode` | **never written** |
| `ThermostatEco` | **never written** |
| `SetHeat` | never, except a bit-identical echo inside `SetRange` with `heatCelsius` seen <10 min ago |
| `OCCUPANT_CLASS == vulnerable` | `HARD_MAX_WRITE = T_BASE` ⇒ **read-only; the thermostat half does nothing but read** |
| `OCCUPANT_CLASS` unanswered | **system will not arm** |

The software **cannot make the house colder than if it had never existed.** That
asymmetry is bought by never pre-cooling — which §3.2 rows 6–7 show is worthless
here anyway.

**Worst case:** writes 78 °F at 14:00 on a 97 °F day, then dies forever. The
house holds at 78 °F at the thermostat; an upstairs room reaches 82–86 °F with
stratification. Bounded, not runaway. 78 °F is the DOE's own published summer
recommendation. It is a comfort event, not a safety event, for a healthy adult —
which is exactly why a declared vulnerable occupant removes all write authority.

### 5.2 While the daemon runs

```
Band            floor T_BASE, ceiling min(T_BASE+3, 78.0)
Window          raises only 11:00-16:30 local
Minimum move    1.0 C   (below Nest's ~1.1 K maintenance band nothing happens)
Dwell           >=15 min between writes; <=4 writes/day
Deviation       <=150 min continuous, <=240 min/day, then >=60 min at T_BASE
Abandon now     T_in > commanded + 1.0 C for 15 min (AC losing ground)
                RH > 60% for 30 min (dry-climate premise broken)
                Nest offline or ambient stale >10 min
                hvac frozen (mirrors solar.grid_is_stuck)
                override detected
Override        Pub/Sub primary + 5 min poll backstop. Echo tolerance 0.15-0.20 C
                -- NOT 0.3 C, because a 1 F human nudge is 0.556 C and must not be
                swallowed. Scheduled transitions are indistinguishable from a
                human hand: treat both as override.
Stand-down      later of 4 h or next 04:00. Two overrides in 24 h => raise
                disabled until explicitly re-armed in the UI.
Visibility      persistent banner whenever a deviation is active: commanded
                setpoint, baseline, reason in words, minutes elapsed and
                remaining, one-tap Release now. comfort_ticks table logging every
                write AND every refusal -- the refusals are the valuable half.
```

**The governing rule: the owner must never discover the software's influence by
feeling hot.**

Humidity is logged, not modelled — ignoring it costs <1 °F at this site. The
>60% guard asserts the premise still holds; in a Denver July it should never
fire, and that is the point.

---

## 6. Expected value

Miles at 3.5 mi/kWh. "Qualifying day" = car home, plugged, hungry, in the solar
window — roughly ⅓ of days.

| Item | mi/day amortized | Risk | Comfort cost |
|---|---|---|---|
| T1 ride-through | **4–7** | very low | zero |
| Charge-limit raise (already built **and already enabled**) | 2–4 | zero | zero |
| T2 read-only Nest | 1–3 | low | zero |
| T3 setpoint raise | **2–4** | **medium-high** | house up to 3 °F warmer, ≤150 min |
| Night ventilation (**not software**) | **5–17** | zero | zero or positive |

**T3 is ~20–25% of the available gain and 100% of the safety exposure.** Recorded
here because the owner elected it with that trade stated.

**Night ventilation — a whole-house fan, or opening windows on Front Range
nights — is worth more than every software item combined.** It is not software
and is recommended to the owner directly.

---

## 7. Owner-supplied configuration

Answered 2026-07-27. None of these are readable from any API.

| Setting | Value | Consequence |
|---|---|---|
| Thermostat | **Google Nest**, via Device Access | Cloud-only; owner is registering |
| Import tariff | **flat rate, no TOU** | No cost inversion from deferring cooling |
| `T_BASE_F` | **75 °F** | Ceiling = `min(75+3, 78)` = **78 °F**, exactly the DOE summer recommendation |
| `OCCUPANT_CLASS` | **normal** (nobody heat-sensitive) | Write authority enabled within the §5 envelope |

## 8. Open questions

1. **Export credit.** Decides whether any of this is worth money or only ledger
   points. One bill. Still unanswered, and it is the one that matters.
2. **Single-stage vs multi-stage compressor.** Verify from the step distribution
   before assuming the ~6 kW step is one unit.
3. **Nest ambient resolution.** Unknown; measure in the first hour of Tier 2.

## 9. Deployment note

Schema migration adds columns but **cannot change values in an existing row**.
`grace_s` was left at the old shipped default of 180 s on the live database
after the ride-through landed, which would have let the time cap bind before
the energy budget and silently disable the feature. Bumped to 900 s on
2026-07-27. Any future change to a CONFIG_DEFAULTS value that already has a
column needs the same explicit data migration.
