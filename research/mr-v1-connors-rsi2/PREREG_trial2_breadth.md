# Mean-Reversion V1 (Connors RSI(2)) — zero-tuning breadth test, pre-registration

**Written 2026-08-02, BEFORE running the fresh-instrument evaluation.** No parameter is
optimised anywhere in this trial — every value is fixed a priori from the design doc. The test is
read exactly once. If anything below must change after seeing results, it becomes a NEW
pre-registration with the trial count incremented.

**Trial count: #2** — "daily Connors RSI(2) mean-reversion is a representative, cost-surviving
edge across upward-biased equity indices, at fixed (un-tuned) parameters."

## Why this trial exists (result of trial #1)
Trial #1 (`PREREG_trial1_cross_instrument.md`) tuned on {EP, YM}, froze rsi=5/atr=2.5/hold=7, and tested on
{NQ, RTY, DAX}. Verdict: **FAIL — solely on trade count (94 < 100)**; all three test instruments
were net-profitable after costs (pooled PF 1.56; DAX 1.46, NQ 1.03, RTY 2.65). Two lessons drive
trial #2:
1. **Tuning did nothing** — the grid selected the plain design-doc defaults. So optimisation is not
   the lever; it only adds a degree of freedom to blame. Trial #2 removes it entirely.
2. **The bar was missed on sample size, not on edge.** The fix is more *breadth*, on instruments
   we have not yet examined, so the ≥100-trade claim is clean.

## Hypothesis
Same as V1 (design doc): in an upward-biased index, RSI(2) < 5 while Close > SMA200 marks a
temporary oversold dislocation that reverts to the 5-day SMA. Long-only. Rules frozen in
`python/MeanReversion1.py` (`--selftest` green).

## Fixed parameters (a priori — NOT tuned, no grid)
Every value below is set from the design doc / engine defaults before any data is seen. There is
no in-sample set and no optimisation:

| param | value | source |
|---|---|---|
| `rsi_period` | 2 | Connors RSI(2) |
| `rsi_entry` | 5 | design doc "RSI(2) < 5" |
| `trend_period` | 200 | design doc 200-day SMA filter |
| `exit_sma_period` | 5 | design doc 5-day SMA target |
| `use_rsi_exit` | False | primary target only |
| `max_hold` | 7 | design doc "5–7 trading days" (upper bound) |
| `atr_period` | 14 | Wilder standard |
| `atr_stop_mult` | 2.5 | engine default |
| `risk_frac` | 0.01 | Mandelbrot/Taleb ≤1% per trade |
| `max_positions` | 5 | design doc correlated-drawdown cap |
| `max_position_frac` | 0.20 | engine default notional cap |
| `cost_bps` | 1.0 / side | futures/prop firm, conservative (trial #1) |

## Frozen data definition (do not tune)
Same cleaning as trial #1 (epoch→date, **Mon–Fri only**, one bar/session, `Date,Open,High,Low,
Close,Volume`), applied to every instrument via `mt5_adapter.clean_file`.

## Datasets and roles (fixed)
The test set must be instruments **NOT examined in trial #1** — otherwise their favourable
behaviour is already known and inclusion is a soft selection. {EP, YM, NQ, RTY, DAX} are therefore
**excluded from the pass/fail set** (they may appear only in a separate context panel).

- **PRIMARY test set (clean OOS):** newly-exported upward-biased equity indices, **pooled as one
  portfolio**, targeting **≥ 100 aggregate trades**. **LOCKED 2026-08-02, before the run:
  FTSE 100 (`^FTSE`), CAC 40 (`^FCHI`), Nikkei 225 (`^N225`)** — Yahoo daily cash indices,
  2016-01 → 2026-07, saved to `data/yahoo/`, already Mon–Fri / one-bar-per-session / date-stringed
  so read natively (no `mt5_adapter` needed). FTSE was unavailable from MT5, so all three came from
  Yahoo. (Drawn from the pre-listed candidate pool: FTSE, CAC, EURO STOXX 50, Nikkei, ASX 200,
  TSX 60, IBEX, SMI.)
  - Geographic spread is deliberate: non-US indices are **less correlated** with the US set than
    NQ/RTY/DAX were with EP/YM, which directly weakens trial #1's correlation confound.
  - Note: FTSE has weaker structural drift than the US indices — a *harder*, more honest test of
    the upward-bias assumption.
- **SECONDARY (context only, not gating):** the same fixed params pooled over the FULL universe
  (fresh + the five from trial #1) — reported as the portfolio-level result. Trivially clears 100
  trades; shows aggregate edge but is not the clean claim.

## Success criteria (fixed in advance; PRIMARY fresh-instrument set, net of costs)
Zero-tuning V1 is **SUPPORTED** iff ALL hold on the pooled fresh-instrument portfolio:
1. **≥ 100 aggregate trades**, AND
2. **Pooled net profit factor > 1.0**, AND
3. **≥ ⅔ of the fresh instruments individually net-profitable (PF > 1.0)**, and none badly broken
   (no instrument with PF < 0.8).

Sharpe/Calmar/CAGR/MaxDD reported for context, **not gating** (user considers them unreliable on
thin, lumpy samples).
Otherwise: **NOT SUPPORTED** — daily V1 does not generalise at a representative sample size on
un-tuned parameters, which justifies moving effort to the L2 V2.

## Falsification / anti-p-hacking
- Run **once**. If it fails, do **not** swap the parameter values, drop a weak instrument, or add
  instruments and re-run — any of those is a **new** pre-registration (trial #3).
- Because there is **no optimisation**, the only researcher degrees of freedom are the instrument
  list and the fixed parameter values — both frozen here, before any fresh data is seen.

## Explicitly NOT claimed
- Still no regime filter (Ehlers Hilbert discriminator = future scope).
- Fills at close/stop; slippage only via `cost_bps`; futures sizing/financing/prop-limits
  approximated by the %-notional engine.
- Correlation among equity indices is reduced by geographic spread but not eliminated.
- Daily variant only; L2 order-book V2 (Databento → NautilusTrader) stays deferred until v1 is
  completely done.

## Implementation (thin; reuses the trial-#1 harness)
1. Export the PRIMARY fresh instruments into `data/L1/Daily/` (same MT5 daily-export format).
2. **`python/breadth_test.py`**: map fresh symbols → files; `clean_file` each into
   `data/daily/breadth/`; load as one portfolio via `load_data`; run `Backtester` with the
   fixed `Config` above (no grid, `cost_bps=1.0`); compute pooled + per-instrument metrics; reuse
   `crossval.verdict` (criteria 1–3); emit `breadth_report.txt`, `trades_log.csv`,
   `equity_curve.png`, `README.md` → `research/mr-v1-connors-rsi2/artifacts/trial2/`.
3. Tests (module `--selftest`): symbol→file mapping present; fresh set disjoint from the trial-#1
   five (leakage/independence guard); verdict wiring.

**Blocked on:** export of ≥2 fresh upward-biased indices (FTSE + one more). Harness is otherwise
ready — `mt5_adapter`, engine, and `verdict` are reused unchanged.
