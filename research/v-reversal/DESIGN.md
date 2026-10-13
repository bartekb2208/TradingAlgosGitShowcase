# V-Reversal V1 (VR V1) — design

**Date:** 2026-08-03 · **Status:** approved, not yet implemented
**Track:** VR V1 — a separate algorithm family, parallel to MR V1 and MR V2.

> NOT FINANCIAL ADVICE. Research only.

## Track identity

VR V1 is **not** a variant of MR V2. It gets its own pre-registration
(planned as a separate document, never written: the track closed at the signal stage), its own results document
(`research/v-reversal/RESULTS.md`), and its own engine version line. It shares only
the Nautilus data pipeline (`python/v2/data_pipeline.py`).

It inherits **nothing** from MR V2 — not its thresholds, not its prereg, not its
results. The two hypotheses happen to be long-reversion trades on the same
instrument; that is not a reason to pool them.

## Source

Cody Bengtson's *V-Reversal Setup* (2023; third-party document, not redistributed here). Long case, paraphrased:

1. **Thin impulse down** into a zone. `thin_origin` = the high it fell from. Thin
   areas break back through cleanly — this is the precondition.
2. **Consolidation**, ranging 6–8 ticks.
3. **Blowoff sell** out of the zone → the V low. Volume on the extreme far above
   the zone average, all arriving inside a single second.
4. **Sell imbalance at the extreme**: more than 3,000 contracts across the lowest five
   ticks, leaving *trapped sellers*.
5. **V retrace**: the whole drop is won back, roughly as fast as it was
   made.
6. **Entry** passive (back inside the zone) or on a confirmed break of `cons_hi`.
7. **Stop** at `cons_lo` (narrow) or the V low (wide).
8. **Target** `thin_origin` minus a buffer — an **absolute price**.

### Existing code
`python/vr/v_reversal_prototype.py` is a design sketch, not working code: it imports
`from quantlab.backtest import sizing as sz`, and **`quantlab` does not exist in
this repository**. It has never run here. Its own header states that a bar-based
version is *"a SCREEN, not a verdict"* because bars cannot see the bottom-5-tick
imbalance, the one-second burst, or trapped-trader inference — and says the
footprint phase must replace it with real aggressor imbalance from tick data.

That footprint phase is now available: L3 MBO plus a working Nautilus engine. VR
V1 therefore builds **directly on L3**, and `v_reversal.py` is superseded rather
than completed.

## The core design problem: the doc is in candles, the data is an event stream

Every structural term needs an event-native definition. No time-bucketing: the
doc's blowoff explicitly happens within one second, and a fixed bucket grid
would smear exactly the event the setup turns on.

| doc term | event-native definition | parameter |
|---|---|---|
| thin impulse | price traverses ≥ N ticks with volume-per-tick below the trailing median; `thin_origin` = the high it left | `thin_min_ticks`, `thin_vpt_pctile` |
| consolidation | price range ≤ `cons_max_ticks` sustained ≥ `cons_min_secs`; yields `cons_hi`, `cons_lo`, zone volume | `cons_max_ticks` (6–8), `cons_min_secs` |
| blowoff | aggressive-sell volume within a ≤1 s window ≥ `blowoff_mult` × zone average, printing at/near the extreme | `blowoff_mult`, `blowoff_window_ms` |
| trapped sellers | resting bid size summed over the lowest 5 tick levels ≥ `trap_contracts` — **L3-only evidence** | `trap_contracts`, `trap_levels` |
| V retrace | ≥ `retrace_pct` of the blowoff leg recovered within ≤ `speed_ratio` × the leg's own duration | `retrace_pct`, `speed_ratio` |

### State machine

```
SEEKING ──thin impulse──► THIN_SEEN ──range holds──► CONSOLIDATING
   ▲                                                      │
   │                                                 blowoff + trap
   │                                                      ▼
   └──── timeout / invalidation ◄── RETRACING ◄──────── BLOWOFF
                                        │
                                  retrace_pct met
                                        ▼
                                     ARMED ──entry rule──► IN_TRADE
```

Evaluated on every trade tick and book update using rolling time windows, reusing
the machinery `V2StrategyBase` already proves out (rolling volume deques, EWMA
intensity, percentile trackers). Each state transition is a pure predicate and is
unit-testable without an engine.

## Parameterised judgment

The setup is discretionary at several points. Each such call becomes an explicit
switch, labelled in the module header the way `OrbFirstMoveBot` labels its
judgment calls:

| switch | options |
|---|---|
| `entry_mode` | `passive_zone` · `breakout_confirmed` · `both` |
| `stop_mode` | `narrow_cons_lo` · `wide_v_low` |
| `target_mode` | `thin_origin_buffered` · `scalp_trail` |

**Deliberately not implemented: the falling-knife entry.** The source doc says
its risk cannot be defined. Encoding it would mean inventing a rule
the source explicitly declines to give.

**Targets are absolute prices, never R multiples.** R is only ever a sizing input.
This is stated in the source and is a hard constraint on the implementation.

## Two layers, from day one

The decisive efficiency lesson from the MR V2 work: offline pandas replays over
the L2 tape answered structural questions in **seconds** that the Nautilus engine
took **~25 minutes** to answer. VR V1 is built with both from the start:

| layer | purpose | speed |
|---|---|---|
| **offline detector** (`vr_detect.py`) | tune FSM thresholds, count structures, measure forward moves | seconds/session |
| **Nautilus strategy** (`vr_strategy.py`) | execution truth — fills, queue position, commission | ~4 min/session |

The detection logic is **shared and tested once**, not reimplemented. The offline
layer is for calibration; only the engine layer produces P&L claims.

## Data plan

| role | sessions |
|---|---|
| ES exploratory | 6 (mirroring MR V2, so the tracks stay comparable) |
| ES sealed OOS | 28, read exactly once |
| NQ robustness | 23 (2026-04-06 … 05-06) |
| excluded | 2026-06-19 — Juneteenth half session, 20,715 trades vs a 260k–775k norm |

NQ carries an honest caveat: its window (Apr–May) does not overlap the ES window
(Jun–Jul), so instrument and regime are confounded. It can **support** a result;
it cannot cleanly **attribute** one.

## Falsifiers — REVISED 2026-08-03, after the first exploratory run

### Why the original F1 was wrong

The original read: *"< 100 qualifying V structures across the 6 exploratory ES
sessions → too rare."* That was a specification error, not a disappointing result.

It applied this project's **100-*trade* representativeness standard** — which
exists to size an *evaluation* sample — to a 6-session **calibration** subset.
100 structures over 6 sessions demands **17 per session**, for a setup that by
construction requires a full consolidation → blowoff → retrace cycle. The source
doc presents it as a notable occurrence, not a 17-a-day event. Measured rate once
the detector was made correct: **~1.5 structures/session**.

A bar that no correct implementation of the hypothesis could ever clear is not a
falsifier; it is a unit error. This is the third instance in this project of a
threshold being wrong because it was carried into the wrong frame — see also the
"3 points" vs "3 basis points" reading, and the ESZ23 3000-contract constant.

**This revision is recorded, not silent.** It re-specifies the bar explicitly and
relaxes **no** detection threshold.

### F1 — Sample adequacy (revised)

The rate must project to a testable evaluation sample:

> Let `r` = structures/session measured on exploratory, and `N` = sessions in the
> sealed evaluation set. Require **`r × N ≥ 100`**.

If it fails, the verdict is **"untestable at this project's representativeness
standard on the data available"** — a real and reportable outcome. It is **not**
licence to loosen thresholds until events appear.

Sealed sample: ES 28 + NQ 23 = **51 sessions**. At r ≈ 1.5/session this projects
to ~77 — **short of 100**, and that shortfall is itself a finding about the
hypothesis's testability on this dataset.

### F2 — Direction (unchanged)

Mean forward move after `ARMED` **≤ 0** on exploratory → the reversion direction
is wrong (as it proved for MR V2 hawkes). Close the track there; do not spend the OOS.

### F3 — Cross-instrument rate stability (NEW — anti-overfit)

NQ (23 sessions) is **completely untouched** and is the control for detector
overfitting. Structure rate on NQ must be within a factor of **3** of the ES rate.
A collapse means the detector has been fitted to ES microstructure rather than to
the setup.

> **Tick thresholds must be price-scaled before comparing.** 8 ticks is 2.64 bps
> on ES (~7582) but 0.82 bps on NQ (~24320) — a 3.2x difference. Comparing raw
> tick counts would show fewer NQ structures as a pure unit artifact and invite
> exactly the misreading F3 exists to catch.

### F4 — Cross-instrument direction (NEW — anti-overfit)

Mean forward move on NQ must carry the **same sign** as ES. Opposite signs mean
the ES direction is likely fitted, not real.

### F5 — Cost floor (was F3)

Any tradeable configuration must clear **gross > +0.40 ticks/trade** on ES
(commission $5.00 round turn / $12.50 per tick).

### Change budget (NEW — process guard)

Detection was modified **five times** during the first exploratory build (two
genuine bugs, three spec corrections), and the structure count moved
1 -> 5 -> 11 -> 0 -> 9. Each change was individually justified and none was a
threshold nudge — but "keep fixing until events appear" is the exact failure mode
these falsifiers exist to interrupt, and a count that moves with every change
cannot be distinguished from fitting by inspection alone.

**Rule: any further change to detection logic requires re-running F3 and F4 on NQ
before any ES result is believed.** NQ is the only untouched data, and therefore
the only thing that can adjudicate this.

## Testing

Embedded `--selftest`, house style (no pytest):

- each FSM transition predicate on synthetic event sequences;
- the full FSM on a hand-built synthetic V (thin → consolidation → blowoff →
  retrace) — must reach `ARMED`; and on near-misses that must not;
- `thin_origin`, `cons_hi`, `cons_lo` extraction correctness;
- stop and target selection under each switch combination;
- the absolute-price constraint: no code path may derive a target from an R
  multiple;
- offline detector and engine strategy must agree on structure detection over the
  same session — this is what keeps the shared logic honest.

## Open questions deferred to implementation

1. Short side (the mirrored Λ top). The source says shorts are the inverse; the
   sketch ran sign-flipped arrays through one code path. Decide during
   implementation whether v1 ships long-only.
2. Position sizing. Out of scope for v1 — same reasoning as MR V2: sizing
   multiplies an edge and cannot create one, so it comes after the cost floor is
   cleared.
3. Which risk controls from `docs/RISK_MANAGEMENT_OPTIONS.md` apply. VR V1's
   entries include a passive zone fill, so the MR V2 adverse-selection findings
   (§2.2, §2.4) are likely relevant — but that must be measured on VR's own data,
   not assumed from MR V2's.
