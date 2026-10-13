# Fair Value Theory V1 (FVT V1) — design

**Date:** 2026-08-03 · **Status:** approved, not yet implemented
**Track:** FVT V1 — a separate algorithm family, parallel to MR V1, MR V2 and VR V1.

> NOT FINANCIAL ADVICE. Research only.

## Track identity

FVT V1 is a **1-minute OHLC bar strategy**. It requires no order book, no tape and
no L2/L3 data. It therefore does **not** use the NautilusTrader rig that `v2/` and
`vr/` are built on, and it shares no thresholds, no pre-registration and no results
with them. It gets its own artifact directory (`research/fvt-v1/artifacts/`) and its own
engine version line.

It borrows exactly two things from the existing repo: the reporting/falsifier
shape of `vr/vr_stats.py`, and the MT5 epoch-seconds CSV convention documented in
`python/mt5_adapter.py`.

## Source

FX Replay's strategy sheet, *"JJ Simon's Fair Value Theory NQ Strategy"*
(`https://fxreplay.com/strategies/jj-simons-fair-value-theory-nq-strategy`, PDF
linked from that page). The rules as the document states them:

- **Instrument / timeframe:** NQ, 1-minute chart.
- **Entry windows:** 09:30–11:00 and 14:00–15:00 New York.
- **Fair value:** "If there is no 'new' information for the market to price in, it
  is most likely going to revert back to 'fair value.'" JJ uses the **09:30 open
  price** and the **14:00 NY price** as fair value.
- **Timing:** continuations *away* from fair value for the first ~10–15 min of a
  window; mean reversions *back toward* fair value for the remainder.
- **Entry model:** displacement candle + BOS or MSB → market entry, target 1.5R,
  **no trade management, no partial exits**.
- **MSB:** "Close past wick H/L of recent price leg." BOS is the same mechanic but
  indicates continuation; MSB indicates possible reversal.
- **Displacement candle:** closes decisively with a small counter-wick; mechanically,
  counter-wick < 20% measured with a Fibonacci retracement set to 0, 0.2, 1. "Large
  candle size" is explicitly discretionary, "so it's optional."
- **Stop distance from ATR:** ATR > 20 → 50 pt SL / 75 pt TP; ATR 7–20 → 25 / 37.5;
  ATR < 7 → 16.5 / 24.75. With 1, 2 or 3 NQ contracts this is ≈ $1,000 risk/trade.
- **Optional considerations** (the document's own "may be best" list): avoid the
  first 3 min after 09:30; session VWAP as confluence; skip the 2nd hour of the NY
  windows; 2nd attempts "may be ok, but best to collect the data to confirm"; Asia
  and London opens; 08:30 news reversions.

### Two errors in the source, corrected here

1. The sheet says "one point = $1 on NQ". **NQ is $20 per point** ($5 per 0.25
   tick). The $1,000-risk arithmetic only closes at $20: 50 pt × 1 × $20 = $1,000;
   25 × 2 × $20 = $1,000; 16.5 × 3 × $20 = $990. The document's own contract counts
   confirm $20, so the "$1" is a typo and is not implemented.
2. The prose definition of the counter-wick is circular — see below.

## The core design problem: the source is discretionary prose

Three rules are stated in a form that cannot be executed as written. Each gets an
exact definition here, derived from the document's own worked examples wherever
those examples constrain the answer.

### 1. Displacement candle — decoded from the example fibs

The prose ("counter-wick less than 20% of the distance from the candle's open to
counter wick h/l") is circular: for a bearish candle the distance from the open to
the counter-wick high *is* the counter-wick.

The two worked examples print exact fib prices, and they admit exactly one
anchoring:

| Example | fib 0 | fib 0.2 | fib 1 |
|---|---|---|---|
| p5 top (13,0xx era) | 13,086.25 | 13,081.25 | 13,061.50 |
| p5 bottom (25,4xx era) | 25,415.00 | 25,417.25 | 25,426.00 |

In both, `0.2 = 0 + 0.2 × (1 − 0)` holds to the tick, **fib 1 sits at the candle's
open** and **fib 0 sits at the extreme the candle travelled to** (the high for a
bullish candle, the low for a bearish one). The requirement is that the **close
lands inside the 0 → 0.2 band**, i.e. the residual wick left beyond the close is
under 20% of the open-to-extreme distance. That reading also makes the prose
sentence literally true: the "counter-wick" is the part of the move the candle gave
back before closing.

**Definition (implemented):**

- bullish: `high > open` and `(high − close) < 0.2 × (high − open)`
- bearish: `open > low` and `(close − low) < 0.2 × (open − low)`

A candle with `high == open` (bullish) or `open == low` (bearish) fails — no
denominator, no displacement.

The optional size filter (`require_large_candle`, **default off**): candle range
must exceed the maximum range of the previous 3 bars. Off by default because the
source calls it discretionary and optional.

### 2. BOS / MSB — "close past wick H/L of recent price leg"

- **Swing points:** fractal pivot with `swing_lookback = 2`. Bar *i* is a swing high
  if `high[i]` exceeds the highs of the 2 bars either side; swing low mirrors it.
- **Pivot tracking is continuous across the session's bar series, including
  pre-window bars.** Only this way does a usable level exist at 09:31 rather than
  09:35. `bias`, by contrast, resets to `none` at each window open — the levels are
  market structure, the bias is a per-window reading of it.
- **No lookahead:** a pivot at bar *i* is only *confirmed*, and therefore only
  usable, from bar *i + swing_lookback* onward. This is the single most important
  correctness constraint in the whole design and it gets a dedicated test.
- **Break:** bullish when a bar's **close** exceeds the most recent confirmed swing
  high; bearish when a bar's close is below the most recent confirmed swing low.
  Close, not wick — the source is explicit.
- **Classification:** the run maintains a `bias ∈ {up, down, none}`, reset to `none`
  at each window open. A break that agrees with `bias` is a **BOS**; a break that
  opposes it is an **MSB**. A break while `bias == none` is classified **BOS**.
  After any break, the level is consumed (it cannot re-trigger) and `bias` becomes
  the break direction.
- Both BOS and MSB are tradeable per the source's entry model; the label is carried
  on the trade record.

### 3. Signal = one bar that is both

The displacement candle must itself be the candle whose close breaks the structure
level. This is not an inference from the prose — in the second worked example the
MSB level (25,417.25) lies **inside** the displacement candle's range
(25,415.00–25,426.00), so the break and the displacement are the same bar.

Configurable as `confirm_within_bars` (default `0` = same bar) so the looser reading
is reachable without a rewrite, but `0` is the pre-registered baseline.

## Sessions, fair value and the phase gate

- All timestamps resolved in `America/New_York`. US DST handled by the timezone
  database, not by hand.
- **Morning window:** signals allowed on bars from 09:30 up to and including 10:59.
  Fair value = the **open of the 09:30 bar**.
- **Afternoon window:** 14:00 up to and including 14:59. Fair value = the **open of
  the 14:00 bar**.
- **Phase 1** = the first `phase1_minutes` (default 15) of a window. **Phase 2** =
  the remainder.
- A session missing its 09:30 or 14:00 bar (holiday, early close, data gap) is
  skipped for that window and counted in the report.

**Direction gate (hard, per approved design).** Let `d = +1` for a bullish signal,
`−1` for bearish, and `s = sign(close − fair_value)` on the signal bar.

- Phase 1 requires `d == s` — the trade points **away** from fair value.
- Phase 2 requires `d == −s` — the trade points **back toward** fair value.
- `close == fair_value` exactly → skip the signal.

This is consistent with the source's "you can trade mean reversions either side of
fair value": the gate is relative to fair value, not to a fixed direction.

## Execution and risk

- **Entry:** market at the **open of the bar after** the signal bar, plus
  `slippage_ticks` (default 1). A signal is never filled on its own bar.
- **Brackets:** fixed point distances from the fill price. No trailing, no
  breakeven, no partials, no time stop — "no trade management" is a rule of the
  strategy, not an omission from it.
- **Intrabar ambiguity:** when a single bar's range contains both the stop and the
  target, the **stop is assumed to fill first**. The fraction of trades resolved by
  this assumption is reported as a headline figure, not a footnote — with 1.5R
  brackets on 1-minute bars it is the dominant fidelity risk, and if it is large the
  bar-based result is not trustworthy on its own.
- **Gap-through:** if the next bar opens beyond a bracket, the fill is at the open
  (the worse price), never at the bracket.
- **Concurrency:** one position at a time. Signals arriving while in a position are
  discarded and counted.
- **Window end:** an open position is **not** closed at window end — no management
  rule exists. All positions are force-flat at 16:00 NY (`exit_reason =
  session_close`), and that frequency is reported.
- **Sizing:** `contracts = floor(risk_usd / (sl_points × usd_per_point))`. If that
  is 0, the trade is **refused**, not rounded up into over-risk. On NQ this formula
  *reproduces* the source's contract column exactly — 1000/(50×20) = 1,
  1000/(25×20) = 2, 1000/(16.5×20) = 3.03 → 3 — so there is one sizing rule in the
  code, not a formula plus a hard-coded table. A test asserts the reproduction.
- **Costs:** `commission_per_side` default $2.50/contract (≈$5 round turn, matching
  the VR rig). Gross and net are both reported.

### Two stop rules, both run

The literal tier table is only ≈2×ATR *on average* — at ATR 8 the 25-point stop is
3.1×ATR; at ATR 20 it is 1.25×ATR. They are genuinely different rules, so both are
implemented and both are measured.

- **`literal` mode** — the source's tier table verbatim. ATR is **ATR(14) on 1-minute
  bars**, Wilder smoothing. Verified against the source: measured on real NQ 1m bars,
  09:30–11:00 ATR(14) runs at a median of 18–29 points, squarely inside the sheet's
  tiers, which confirms the timeframe and period the sheet leaves unstated.

  | ATR(14), 1m | SL | TP | NQ contracts |
  |---|---|---|---|
  | > 20 | 50.00 | 75.00 | 1 |
  | 7 – 20 | 25.00 | 37.50 | 2 |
  | < 7 | 16.50 | 24.75 | 3 |

  Boundaries: `atr > 20` → high tier; `7 <= atr <= 20` → mid tier; `atr < 7` → low.

  **ATR is evaluated on the signal bar, inclusive**, over a *continuous* 1-minute
  series that includes pre-window bars, so it is already warm at 09:30. If fewer
  than `atr_period` prior bars exist, the signal is skipped and counted in
  `skipped_not_warm`.

  **ATR state carries across sessions within a split; structure state does not.**
  The NQ L2 tape is RTH-only (verified: first print 09:30:00, last 15:59:59, no
  premarket), so a per-session ATR reset would leave NQ un-warm until 09:44 and
  silently delete all of morning phase 1 — the continuation half of the thesis.
  Carrying ATR is also what a live 1-minute chart actually does: ATR(14) does not
  reset at the open, and the first true range of a session legitimately includes
  the overnight gap. Structure/pivot state *does* reset per session, because "recent
  price leg" means recent in the session being traded.

  Known asymmetry, reported not hidden: ES has genuine premarket bars, NQ does not,
  so NQ's first usable swing level is ~09:34 against ES's 09:30, and NQ's first
  session (2026-04-06) has no ATR seed at all. The counters make this visible.

- **`atr_multiple` mode** — `SL = 2.0 × ATR(14)`, `TP = 1.5 × SL`, size from
  `risk_usd`. This is the portable form of the same idea and the only one that
  transfers across instruments.

**Baselines:** `literal` on NQ (it is the instrument the table was written for);
`atr_multiple` on ES (the literal 50-point stop is ~15 ATR on ES and would be
meaningless). Both modes are additionally run on the NQ sample so the cost of the
tier discretisation is measured rather than assumed.

## Data

There is no multi-year NQ 1-minute series in this repository, and there is no cheap
way to obtain one:

- **yfinance cannot supply it.** Probed 2026-08-03: Yahoo hard-caps 1-minute data at
  8 days per request and ~30 days of total lookback. `NQ=F` at `interval="1m"`
  returns ~8,200 bars covering 2026-07-26 → 2026-08-03 and errors on anything older.
  2m and 5m reach 60 days but are not the strategy's timeframe.
- **The L2/L3 NQ tape** covers 23 sessions (2026-04-06 → 2026-05-06).

### Validation scheme: 1-fold holdout, per instrument

One fold. Chronological. Two splits — **Test** (config is frozen here) and
**Validation OOS** (touched exactly once, after the freeze). No k-fold, no
walk-forward, no nested resampling. Leakage is structural, as in `crossval.py`:
the validation split is a separate loader path and is never opened during
development.

| Instrument | Split | Source | Span | Sessions |
|---|---|---|---|---|
| **ES** | Test | `data/L1/EP_PERIOD_CURRENT_M1.csv` | 2016-01-01 → 2021-12-31 | ~1,510 |
| **ES** | Validation OOS | same file | 2022-01-01 → 2026-07-22 | ~1,140 |
| **NQ** | Test | resampled from `data/L2/NQ_*_trades.parquet` | 2026-04-06 → 2026-05-06 | 23 |
| **NQ** | Validation OOS | yfinance append-only cache | 2026-07-26 → forward | ~6, grows each harvest |

The NQ cut point is not arbitrary — the two NQ sources are already chronologically
disjoint with a ~3-month gap, so the split falls out of the data rather than being
chosen. That removes the one degree of freedom a hand-placed cut would have added.

**The NQ validation split starts undersized and says so.** At ~6 sessions it cannot
support a verdict. Its falsifier reports `NOT EVALUABLE` until the append-only
yfinance cache reaches `min_validation_sessions = 20`, at which point it is scored
once. The cache accumulates every time the harvester runs, so this resolves itself
with calendar time and no further design work — but the report must never present a
6-session number as a result.

**Division of labour between the two instruments.** ES carries the *statistical*
falsification of the entry model: it is the only sample large enough for F1–F5 to
mean anything. NQ carries the *instrument* verdict — it is what JJ's claim is
actually about, and the literal tier table only applies there. A pass on ES with a
failure on NQ means the entry model works but the specific claim does not. The two
are reported separately and never pooled.

The ES file was verified NY-aligned before being adopted: converted from epoch
seconds to `America/New_York`, the two highest average-tick-volume minutes of the
day are 15:59 and 09:30, and 2,720 sessions carry a 09:30 bar over
2016-01-01 → 2026-07-22.

**ES is an adjacent-instrument proxy, and the design says so out loud.** JJ's claim
is NQ-specific. ES is used because 2,720 sessions can falsify a hypothesis and 23
cannot, and because the `atr_multiple` form of the rule is instrument-neutral. A
positive ES result is evidence about the *entry model*, not proof about NQ.

## Tags, not filters

Three variants are recorded on every trade record rather than being separate runs.
Each is then recoverable by filtering `trades.csv`, with no re-run and no parameter
sweep:

- `phase` ∈ {1, 2}
- `structure` ∈ {BOS, MSB}
- `attempt_n` — the 1-based index of the trade within its window (the source says
  "2nd attempts may be ok, but best to collect the data to confirm", so
  first-attempt-only is a filter on the log, not another backtest)

### Trade record

One row per round trip in `trades.csv`. Every falsifier and every tag-filtered
variant must be computable from this table alone:

`session`, `symbol`, `window` (am/pm), `phase`, `structure`, `attempt_n`,
`direction`, `signal_ts`, `entry_ts`, `entry_px`, `exit_ts`, `exit_px`,
`exit_reason` (target/stop/session_close), `fair_value`, `atr`, `tier`,
`sl_points`, `tp_points`, `contracts`, `gross_usd`, `commission_usd`,
`net_usd`, `bars_held`, `ambiguous_bar` (bool), `gapped_fill` (bool — a stop that
filled through its level rather than at it).

## Module layout

`python/fvt/` — pure core, thin drivers, mirroring `vr/`:

| File | Responsibility |
|---|---|
| `fvt_data.py` | Bar access only. ES M1 CSV → NY-localised 1m OHLC; NQ 1m resampled from the L2 trade tape; append-only yfinance NQ cache. Exports `load_bars(symbol, source)` and session iteration. |
| `fvt_core.py` | Pure logic, zero I/O: `FVTConfig`, pivots, displacement test, BOS/MSB state machine, fair-value anchor, phase gate, signal emission. Unit-testable without an engine. |
| `fvt_engine.py` | Bar loop: entry at next-bar open, bracket resolution, ambiguity accounting, commission, session flat. Emits trade records. Also carries the **tick-arbitration mode** used by F6: for the NQ sample only, an ambiguous bar is re-resolved by walking that minute's actual trade prints in order instead of assuming the stop. |
| `fvt_stats.py` | Runs the samples → `research/fvt-v1/artifacts/{stats.json,report.txt,trades.csv}`, computes falsifier verdicts. |

Every module carries a `--selftest` flag, per repo convention.

**Why not NautilusTrader.** The rules are bar-close 1m OHLC with no book
dependency. Nautilus is in `v2/`/`vr/` because those need L3 queue position; here it
would cost minutes per session across 2,720 sessions and buy nothing. The accepted
trade-off is that fill resolution is hand-written rather than inherited — which is
why the ambiguous-bar rule gets an explicit test and an explicit reported number.

## Configuration

`FVTConfig` (dataclass). Pre-registered baseline values:

| Parameter | Default | Note |
|---|---|---|
| `swing_lookback` | 2 | fractal pivot half-width |
| `displacement_max_counter_wick` | 0.20 | the source's 20% |
| `require_large_candle` | `False` | source calls it discretionary |
| `large_candle_lookback` | 3 | only used when the above is on |
| `confirm_within_bars` | 0 | 0 = displacement and break on the same bar |
| `morning_window` | 09:30–11:00 | NY |
| `afternoon_window` | 14:00–15:00 | NY |
| `phase1_minutes` | 15 | source says "~10–15m" |
| `skip_first_minutes` | 0 | the "avoid first 3m" note is optional; off in baseline |
| `hard_phase_gate` | `True` | approved decision |
| `stop_mode` | `literal` | set per instrument by the caller: `literal` for NQ, `atr_multiple` for ES |
| `atr_period` | 14 | 1-minute bars, Wilder |
| `atr_stop_mult` | 2.0 | `atr_multiple` mode only |
| `rr_target` | 1.5 | source: fixed 1.5R |
| `risk_usd` | 1000.0 | source |
| `usd_per_point` | 20.0 (NQ) / 50.0 (ES) | contract spec |
| `commission_per_side` | 2.50 | per contract |
| `slippage_ticks` | 1 | |
| `max_trades_per_window` | `None` | unlimited; `attempt_n` tag carries the variant |
| `min_validation_sessions` | 20 | below this, the NQ validation split reports `NOT EVALUABLE` |

## Falsifiers

Pre-registered, computed into `stats.json` with PASS/FAIL verdicts so the stop
conditions live in the artifact rather than in someone's memory.

| ID | Split | Rule |
|---|---|---|
| **F1** | ES Test | Sample adequacy: ≥ 300 trades. Below that the sample decides nothing. |
| **F2** | ES Test | Cost floor: gross expectancy per trade > 2 × round-turn commission. |
| **F3** | ES Test | Core claim: **net profit factor ≥ 1.10**. Below → abandon without a parameter sweep. |
| **F4** | ES Test | Win rate vs the **40.0% breakeven** that a fixed 1.5R mathematically requires, reported side by side with the actual. |
| **F5** | ES Test | Phase discrimination: phase 1 and phase 2 expectancies must be distinguishable. If both are ~breakeven and indistinguishable, the fair-value premise is doing no work and this is just displacement scalping. |
| **F6** | NQ Test | Fill fidelity: ambiguous-bar fraction < 20%, **and** the tick-arbitrated replay agrees in sign with the 1m-bar replay. |
| **F7** | ES Validation | Profit factor ≥ 1.05 with same-sign expectancy. One shot, after the freeze. |
| **F8** | NQ Validation | Same rule as F7, on the instrument the claim is actually about. Reports `NOT EVALUABLE` until the cache holds ≥ 20 sessions — an undersized split produces no verdict, not a weak one. |

F3 is the hard falsifier: fail it and the track stops, with no parameter sweep to
rescue it. F7 and F8 are the one-shot confirmations. F1, F2 and F6 are validity
gates — failing one means the result is *uninterpretable*, which is a different
thing from the strategy being bad, and the report must not conflate them.

## Testing

TDD, tests before implementation, plus `--selftest` in every module.

1. **Source-example regression (the important one).** The displacement rule must
   reproduce both worked examples from the source PDF to the cent:
   `(0, 0.2, 1) = (13,086.25, 13,081.25, 13,061.50)` and
   `(25,415.00, 25,417.25, 25,426.00)`. This is what pins the fib decoding to the
   primary document; if it ever fails, the decoding is wrong and everything
   downstream is void.
2. **No lookahead.** A pivot at bar *i* is unusable before bar *i + swing_lookback*;
   a signal on bar *i* never fills before bar *i+1*'s open. Asserted by replaying a
   truncated series and requiring identical signals on the overlap.
3. **Intrabar ambiguity** resolves to the stop, and the ambiguity counter increments
   exactly once per such trade.
4. **Gap-through** fills at the open, not at the bracket.
5. **Sizing** never rounds up; a sub-one-contract requirement refuses the trade.
6. **DST.** The 09:30 and 14:00 anchors resolve correctly across both US DST
   transitions (spring forward and fall back).
7. **Phase gate.** A signal at exactly fair value is skipped; the phase boundary at
   exactly +15 minutes falls in phase 2.
8. **Structure classification.** A constructed series produces the expected
   BOS/MSB labels and level consumption.

## Explicitly out of scope for V1

Every one of these is in the source's optional "considerations" list and none is in
its trade checklist. They are parameters or later work, not V1 behaviour:

- Session VWAP confluence
- Asia / London open windows
- 08:30 news reversion trades
- Additional continuations after price returns to fair value
- Skipping the 2nd hour of each NY window (reachable by narrowing the window params)
- Any parameter sweep or optimisation before F3 is passed

## Known defects (V1)

Three known limitations ship with V1 rather than being fixed retroactively, because
retuning any of them after the data was seen would itself be a form of leakage. They
are recorded here so the next version inherits the lesson instead of the mistake.

1. **F2's cost floor is unit-inconsistent.** The pre-registered threshold,
   `2 * 2 * commission_per_side` ($10.00 at the default $2.50/contract/side), is a
   ONE-contract figure, but ES Test trades size up with `risk_usd`/ATR and average
   roughly nine contracts each (~$45 of round-turn commission per trade) — so the
   gate is about 4.5x more lenient than "twice the round-turn cost" is meant to
   imply. The threshold was deliberately NOT retuned once the data was seen; that is
   exactly what pre-registration exists to prevent. V1 therefore ships with the
   lenient gate as pre-registered, alongside an ADVERSE/below-floor distinction in
   F2's note (`falsifiers()` in `fvt_stats.py`) so that a positive-but-below-floor
   result is never misreported as evidence either way. Any future FVT version must
   compare gross expectancy per trade against
   `2 * 2 * commission_per_side * mean(contracts)`, not the 1-contract number.

2. **The ES export contains timestamp-shifted days.** Roughly 200 days in
   `data/L1/EP_PERIOD_CURRENT_M1.csv` are shifted 5 hours early — a
   local-time-as-epoch artifact — with the tell being a last bar at 10:59 or 11:59
   NY instead of 16:59, an intraday range that peaks around 03:30/04:30 instead of
   08:30/09:30, and Saturday-dated bars appearing in the same week. V1 screens these
   out (`_screen_misaligned_days` in `fvt_data.py`) by dropping any day whose last
   bar precedes 12:00 NY, plus Saturday-dated days that already end before 16:00.
   The original predicate cut at 16:00 rather than 12:00 and could not distinguish a
   shifted day from a legitimate early-close half-session (Black Friday, Christmas
   Eve, July 3rd and similar) before being tightened to the 12:00 cut used now —
   the 16:00 cut discarded 16 real trading sessions along with the artifact.

3. **`confirm_within_bars` is inert.** `FVTConfig.confirm_within_bars` exists as a
   field, but no logic in `fvt_core.py` reads it; only the same-bar (`0`) reading —
   displacement candle and structure break on the same bar — is implemented. That is
   the pre-registered baseline and the only reading the source document's own
   worked examples support, so the omission is a scope limit rather than a bug, but
   the field should not be mistaken for a working knob.

4. **F5 implements half of its stated rule.** The spec's intent is to falsify when
   phase 1 and phase 2 are *both ~breakeven and* indistinguishable, but the
   implementation tests indistinguishability alone. F5 can therefore report PASS on
   two deeply unprofitable phases — as it does in the V1 run, where it passed at a
   25.19% gap between expectancies of −$255.65 and −$191.24. The threshold was
   deliberately not retuned after the data was seen; V1 ships the lenient rule plus
   a note on F5 stating when a PASS must not be read as support. Any future FVT
   version must implement the conjunction: a phase pair is only discriminating if
   the phases are distinguishable **and** at least one of them is profitable.
