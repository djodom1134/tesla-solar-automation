# Manual override pauses solar control

**Status:** approved 2026-08-29

## The problem

The solar controller and the owner both write `charge_current_request`, and
today the controller always wins. Move the charge-rate slider in the Tesla app
or on the car's own screen while solar control is engaged, and the next tick
writes the solar-derived rate straight back over it. There is no way to say
"leave it alone for now" short of turning the whole feature off in the setup
page — and no way to see, from any screen, which of the two is currently
driving the car.

Two changes, one feature:

1. An override of the charge rate **pauses** automated solar control until
   charging is stopped and started again, or until it is re-enabled from the
   car page. On by default; a setting turns it off.
2. Every page that shows the controller says which **mode** charging is in.

## Decisions taken up front

These were settled before design and are load-bearing; changing one changes
the rest.

| Question | Decision |
|---|---|
| What counts as an override? | **A change to the charge rate only.** Not the charge limit, not a manual stop. "Overrides the charge rate" is what was asked for, and it is the one value the controller both writes and reads every tick, so detection can be exact rather than inferred. |
| What clears the pause? | **Any stop→start observed at the car**, plus the explicit control on the car page. Unplug/replug passes through `Disconnected` and so counts too. |
| What happens to a raised charge limit? | **Put it back, then hands off.** Undoing our own change is not fighting the owner; leaving it raised is, since the car would keep charging past the limit they set, at the rate they just chose. |
| What happens to the recorded `original_amps`? | **Dropped.** The rate the owner just set *is* the standing rate now; there is nothing left to restore. |
| Does `enabled` change? | **No.** `manual` is derived, exactly as `now` is. The HA switch and the setup checkbox keep their present meaning. |

## Detecting the override

The naive test — "the car reports a rate different from the one we wrote" —
is wrong, and wrong in the expensive direction. A write takes time to reach
`vehicle_data`, and between writes the view is only refreshed every
`view_refresh_ticks` (default 5 ticks = 10 minutes). A mismatch is therefore
routine right after a write, and latching on it would disable automation for
the rest of the session over nothing.

Three ways to handle that were considered:

- **Timestamp guard** — record when the command went out, only compare against
  a view observed after it. Correct, but requires threading a view observation
  time through the loop, which nothing else needs.
- **Two-tick dwell**, as `breach_ticks` does. But that is four minutes at the
  default period during which the controller keeps writing over the rate the
  owner just chose — the exact fight this feature exists to end.
- **Acknowledge-then-diverge** — chosen.

The rule: track the last rate the controller successfully wrote
(`commanded_amps`) and whether the car has been **observed reporting it back**
(`commanded_ack`). An override is a car that had already acknowledged our
value and *then* reports a different one.

```
write 12 A          commanded=12  ack=0
car reports 48 A    commanded=12  ack=0   -- not yet propagated; NOT an override
car reports 12 A    commanded=12  ack=1   -- the car has adopted our value
car reports 32 A    commanded=12  ack=1   -- OVERRIDE. Latch at 32 A.
```

Latching requires the car to have said "12" and then said "32", which nothing
but an outside writer can produce. This is immune to propagation lag by
construction: no clock, no dwell, no threading, and the whole rule is a pure
function of three values.

A rewrite by the controller resets the handshake (`commanded=16, ack=0`), so
the tick where the car still reports the previous value cannot latch. A write
the car never acknowledges never arms the latch at all — which is correct: we
never established control, so there is nothing to be overridden.

Force mode (`charge_mode == "now"`) never records `commanded_amps`, so a
forced charge cannot trigger a pause. A force is the owner explicitly asking
for maximum rate; `force_plan` already owns that conversation.

## Schema

One column on `solar_config`, via `CONFIG_NEW_COLUMNS` — `CREATE TABLE IF NOT
EXISTS` is a no-op against the owner's live table, so the migration list is
the only path onto it:

```sql
pause_on_override INTEGER NOT NULL DEFAULT 1
```

Four on `solar_state`, via `STATE_NEW_COLUMNS`:

```sql
commanded_amps  INTEGER  -- last rate the controller successfully wrote
commanded_ack   INTEGER  -- has the car been seen reporting it back
override_amps   INTEGER  -- the rate the owner set; NOT NULL means paused
override_since  INTEGER  -- when it latched, for display only
override_armed  INTEGER  -- charging was seen to stop; a start now resumes
```

`override_amps IS NOT NULL` **is** the paused state. One source of truth,
which also carries the number the UI needs to show. A separate boolean beside
it could disagree with it; this cannot.

## Pure logic (`solar.py`)

Two functions, no clock, no database, no network — testable the way
`advance()` and `force_plan()` are.

```python
def override_step(commanded_amps: int | None, commanded_ack: bool,
                  charge_amps: int | None) -> tuple[bool, int | None]:
    """Returns (ack_next, override_amps or None). See the handshake above."""

def override_cleared(armed: bool, charging_state: str | None,
                     ) -> tuple[bool, bool]:
    """Returns (armed_next, cleared). Charging leaving the live set arms;
    returning to it while armed clears the pause."""
```

`LIVE_CHARGING_STATES` (`{"Charging", "Starting"}`) is reused unchanged, so
`Complete`, `Stopped`, `Disconnected` and `NoPower` all arm.

## Mode

`charge_mode` gains a fourth value and a second argument:

```python
charge_mode(conf: dict, st: dict, now: float) -> "now" | "off" | "manual" | "solar"
```

Precedence, in order:

1. `now` — a live force outranks everything. Forcing is the owner taking
   control back explicitly, and `PUT /charge-mode` clears the latch.
2. `off` — `enabled` is 0. The owner has turned the feature off; that they
   also once moved a slider is not worth reporting.
3. `manual` — paused by an override.
4. `solar`.

The state argument is what makes this an interface change: the latch is
per-vehicle and belongs in `solar_state`, not in `solar_config`, which is the
web app's table and takes exactly one collector write (`force_charge_until`).
Callers updated: `solar_routes._mode_payload`, `mcp_server` (two call sites),
`tests/test_charge_mode.py`.

## Collector behaviour

Structured as force mode already is — a branch that bypasses `advance()`
rather than a new state inside it.

**On the latch tick:**

- Restore the charge limit *only* if the controller raised it and the car
  still holds the raised value — the condition `_restore` already applies.
- **Never rewrite amps.** This needs an `amps=False` variant of `_restore`;
  the existing unconditional amps write would put back the rate the owner
  just replaced, which is precisely the fight being ended.
- Clear `dirty`, `original_amps`, `original_limit`, `raised_to`, `engaged_at`.
- Reset the machine to `idle`.

**While paused:**

- `advance()` does not run. No vehicle commands are issued at all.
- Ticks are still **logged**. `green.charged_split` derives the whole
  solar/grid attribution from `solar_ticks`, and a manual charge that went
  unlogged would be invisible to the banked-solar ledger and to "miles added
  today" — the same reasoning that keeps force-mode ticks logged.
- The machine state is `idle` and the mode is `manual`. That reads correctly:
  the solar machine is not driving anything.

**Resuming:** `charging_state` leaving the live set sets `override_armed`;
returning to it clears `override_amps`, `override_armed`, `commanded_amps` and
`commanded_ack` together. A new session starts with a clean handshake.

**When `pause_on_override` is 0** the handshake is not even tracked and
behaviour is byte-for-byte what ships today.

## UI

**Setup page**, in the Solar charging card, under the enable checkbox:

> ☑ Pause solar control if I change the charge rate in the app or the car

with a hint naming both ways back — stop and restart charging, or the button
on the car page.

**Car page**, on the Solar charging card. Today one pill shows
`enabled ? state : "off"`, conflating mode with machine state. It becomes two:
a **mode** pill (`Solar` / `Now` / `Manual` / `Off`) and the existing state
pill. When paused, a line reads what happened and offers the way back:

> **Manual** — you set 32 A at 14:12. Solar control is paused.
> [ Resume solar control ]

The button calls the existing `PUT /charge-mode {"mode": "solar"}`, which
clears the latch. No new route.

`GET /solar/status` gains `mode`, `override_amps` and `override_since`;
`demo.py` gains them too, so the demo card renders the feature.

## Testing

- `override_step`: propagation lag does not latch; adoption then divergence
  does; a controller rewrite resets the handshake; a never-acknowledged write
  never latches; `charge_amps` of `None` is inconclusive, never an override.
- `override_cleared`: `Complete` arms; `Charging` while unarmed does not
  clear; arm-then-charge clears; `Disconnected` arms.
- `charge_mode`: the four values and their precedence, including that a live
  force outranks a pause and that `off` outranks it.
- Collector: the latch restores the limit but **not** the amps, then issues no
  further commands; ticks keep being logged while paused; `pause_on_override=0`
  reproduces today's behaviour.
- Routes: the new status fields; `PUT /charge-mode {"mode":"solar"}` clears the
  latch.

## Out of scope

The charge **limit** being changed externally, and a manual stop, are not
overrides — see the decisions table. Neither is a rate change made while the
controller is idle: with nothing commanded there is no handshake, and a car
the controller was not driving cannot be taken away from it.
