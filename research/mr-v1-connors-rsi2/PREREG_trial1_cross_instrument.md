# Mean-Reversion V1 (Connors RSI(2)) — multi-index cross-instrument out-of-sample, pre-registration

**Written 2026-08-02, BEFORE running the test-set ({NQ, RTY, DAX}) evaluation.** The tuning set,
parameter grid, objective, cost model, and success criteria below are locked. The test set is
read exactly once, with a single frozen parameter set, after tuning is complete. If anything below
must change after seeing test results, it becomes a NEW pre-registration with the trial count
incremented.

**Trial count: #1** — "daily Connors RSI(2) mean-reversion is a representative, cost-surviving,
generalisable edge across upward-biased equity indices."

## Governing design doc
`Adaptive Microstructure Trading Framework` (Quantitative Trading Research Notes), Version 1.
Binding principles: *"simple models justify complex models"* (this daily V1 is the gate before the
L2 order-book V2); *"robust execution modelling is mandatory"* (a frozen futures cost model is
included — no zero-cost runs); *"every variable must have an economic rationale."*

## Hypothesis (design doc, V1)
Fama–French `Pt = Qt + Zt` (permanent random walk + stationary AR(1) temporary component). In an
upward-biased index (drift in `Qt`), RSI(2) < 5 while Close > SMA200 marks a temporary `Zt`
dislocation — a liquidity vacuum from localised panic selling — that reverts toward the 5-day SMA.
Long-only (the short mirror carries heightened fat-tail risk in upside-biased markets). Rules
frozen in `python/MeanReversion1.py` (validated by `--selftest`), risk per Mandelbrot/Taleb
(1%-risk ATR sizing, hard stop, max 5 concurrent positions).

## Why cross-instrument, and why the trade count is the real bar
A daily swing on ONE index fires ~5 trades/yr (~53 in a decade) — far below the **≥100 trades**
needed to distinguish an edge from noise (smoke test: EP full-sample, zero-cost = 53 trades,
0.60% CAGR, PF 2.84 — real per-trade edge, statistically empty in aggregate). Sharpe/Calmar can be
meaningless on such thin, lumpy samples. So representativeness comes from **breadth**, and
generalisation is tested **across instruments**: one global parameter set tuned on two indices,
then applied to three unseen ones.

## Frozen data definition (do not tune)
Source `data/L1/` daily exports. Cleaning (frozen, applied to every instrument): `time`
(epoch seconds, UTC) → date; **Monday–Friday only** (drop Sat/Sun globex/partial bars that distort
RSI/ATR/SMA); one bar per session; columns Date/Open/High/Low/Close/Volume(=tickvol). Written to
`data/daily/<SYM>.csv`.

| role | instrument | file | coverage |
|---|---|---|---|
| **TUNE** | S&P 500 (EP) | `EP_PERIOD_CURRENTDAILY2000-2026.csv` | 2016-01 → 2026-07 |
| **TUNE** | Dow (YM) | `Daily/YMDaily.csv` | 2016-01 → 2026-07 |
| **TEST** | Nasdaq-100 (NQ) | `Daily/ENQDailyOHLC.csv` | 2016-01 → 2026-07 |
| **TEST** | Russell 2000 (RTY) | `Daily/RTYDaily.csv` | 2016-01 → 2026-07 |
| **TEST** | DAX (DD) | `Daily/DDDaily.csv` | 2018-05 → 2026-07 |

Identities confirmed by price level. Full history per instrument is used (no temporal sub-split) —
that is how the sample reaches the ≥100-trade bar.

## Frozen execution / cost model (mandatory)
Trading is **futures via a prop-firm account**. `cost_bps = 1.0` **per side** (≈ 2 bps
round-trip) — deliberately conservative; true e-mini round-turn (commission + ~1 tick) is ~0.3–0.6
bps of notional. All performance below is **net**.
**Documented deviations (proxy, not exact):** the engine sizes fractional shares off %-notional,
not whole futures contracts with fixed $ tick value, and does not model prop-firm contract/drawdown
limits or overnight financing. Acceptable for measuring *signal edge*; revisit before any live
sizing. Small optimistic bias, noted not hidden.

## Frozen strategy + parameter search (TUNE set only)
Optimised parameters (grid, tune set only) — 27 combinations, ranges justified:

| param | grid | economic rationale |
|---|---|---|
| `rsi_entry` | {2, 5, 10} | doc: RSI(2)<5 standard, <2 aggressive; 10 = looser oversold |
| `atr_stop_mult` | {2.0, 2.5, 3.0} | Mandelbrot fat-tail hard-stop distance |
| `max_hold` | {5, 7, 10} | doc: "exit if no revert within 5–7 days"; 10 = added patience |

All other params fixed at MeanReversion1 defaults (trend 200, exit SMA 5, ATR 14, risk 1%,
max 5 positions, 20% notional cap). Tune instruments {EP, YM} are run as ONE portfolio per combo.
- **Objective:** maximise **net profit factor** on the pooled tune set, subject to a **min-trades
  guard (≥ 10 trades)**; tie-break by **most trades**, then total net return. (Sharpe/Calmar are
  *reported, not optimised* — the user considers them unreliable on thin samples.)
- **Freeze** the single winning param set → `frozen_params.json`, before the test set is read.
- Note: a single GLOBAL set is deliberate — this tests generalisation. Per-instrument optimisation
  is a legitimate but DIFFERENT hypothesis (separate pre-registration), not this trial.

## Success criteria (fixed in advance; TEST set {NQ, RTY, DAX}, net of costs)
Run the frozen set on the three test instruments as one portfolio. V1 is **SUPPORTED** iff ALL hold:
1. **≥ 100 aggregate trades** across the test set (representativeness — the primary bar), AND
2. **Pooled net profit factor > 1.0**, AND
3. **≥ 2 of 3** test instruments individually net-profitable (PF > 1.0), and none badly broken
   (no instrument with PF < 0.8).

Sharpe, Calmar, CAGR, MaxDD, win rate, expectancy are all **reported for context but do NOT gate.**
Otherwise: **NOT SUPPORTED** — daily V1 does not generalise across indices at a representative
sample size, which itself justifies moving effort to the L2 V2.

## Falsification / anti-p-hacking
- The test set is run **once**. If criteria fail, do **not** re-tune on the test instruments and
  re-run — that converts the test into a search. A different rule set, grid, cost assumption, or
  tune/test partition is a **new** pre-registration, trial count +1.
- **Confound, stated honestly:** equity indices are highly correlated, so tuning on S&P+Dow and
  testing on NQ/RTY/DAX is a *moderate* generalisation test, not a strong one — the same market
  factor drives all five. A stronger future test adds cross-time and less-correlated instruments.

## Explicitly NOT claimed
- No regime filter (the doc's Ehlers Hilbert trend/cycle discriminator is future scope).
- Fills at close/stop; no slippage beyond `cost_bps`; futures sizing/financing/prop-limits
  approximated (above).
- This is the **daily** variant; the latency critique and the L2 order-book V2 (Databento →
  NautilusTrader) are deferred until v1 is completely done.

## Implementation (engineering; strategy logic stays frozen in MeanReversion1.py)
1. **Data adapter** → `data/daily/{EP,YM,NQ,RTY,DAX}.csv` (epoch→date, Mon–Fri filter,
   session-dedupe). Reusable for future exports (FTSE, etc.).
2. **`--start`/`--end`** date-window flags on the engine (Config + `load_data` slice; non-breaking)
   — not needed for this split but cheap and useful; covered by selftest.
3. **`python/crossval.py`** driver: clean/load all five; grid-search on {EP,YM} portfolio with the
   net-PF objective + min-trades guard + tie-breaks; freeze one set; single test run on {NQ,RTY,DAX}
   portfolio; compute pooled + per-instrument metrics from the trades log; evaluate vs the 3
   criteria. Emits to `research/mr-v1-connors-rsi2/artifacts/trial1/`: `tune_grid.csv`,
   `frozen_params.json`, `test_report.txt`, `test_equity_curve.png`, `test_trades_log.csv`,
   `README.md` (verdict).
4. **Tests** (extend `--selftest`): epoch→date; Mon–Fri filter; session-dedupe; window slicing
   (no leakage outside `--start/--end`); grid enumeration; **leakage guard** — assert the test
   instruments are never loaded during tuning.
