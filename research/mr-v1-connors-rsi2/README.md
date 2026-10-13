# Mean-Reversion V1 — Connors RSI(2) on daily index data

**Verdict: not supported. Both pre-registered out-of-sample trials missed the 100-trade bar
(94 and 93 trades); the cleaner, un-tuned trial had PF 1.17 with one index net-losing.**

> Research only. Not financial advice.

| File | What it is |
|---|---|
| [`DESIGN.md`](DESIGN.md) | Engine design: data, indicators, sizing, the backtest loop |
| [`PREREG_trial1_cross_instrument.md`](PREREG_trial1_cross_instrument.md) | Trial 1: tune on {EP, YM}, freeze, test once on {NQ, RTY, DAX} |
| [`PREREG_trial2_breadth.md`](PREREG_trial2_breadth.md) | Trial 2: zero tuning, fresh indices {FTSE, CAC, N225} |
| [`RESULTS.md`](RESULTS.md) | Every experiment and the verdict |
| [`artifacts/`](artifacts/) | Reports, equity curves, the tune grid and the frozen parameters as written before the test |

**Code:** [`python/MeanReversion1.py`](../../python/MeanReversion1.py) (engine),
[`python/crossval.py`](../../python/crossval.py) (trial 1),
[`python/breadth_test.py`](../../python/breadth_test.py) (trial 2),
[`python/mt5_adapter.py`](../../python/mt5_adapter.py) (cleans MT5 daily exports).

The design choice worth looking at is in `crossval.py`: **leakage is prevented structurally.**
Tune and test data live in separate directories and the test directory is never opened during
tuning, so the out-of-sample set can't leak in through a careless path.

Trial 1's result is the more interesting failure. The edge did generalise across instruments
(pooled PF 1.56, all three test indices net-profitable) but the sample stopped six trades short of
the bar set in advance, so it's recorded as a fail. Lowering the bar after seeing 94 would have
been easy and would have made the pre-registration meaningless.

The two equity curves in `artifacts/` (one per trial, real index data) are produced by the engine itself.
