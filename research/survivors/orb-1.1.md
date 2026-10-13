# ORB 1.0 → 1.1 — opening-range breakout on US100

**Status: survived a pre-registered out-of-sample test on 4 of 5 gates. Real, modest, and much
weaker than in-sample suggested.**
**What's published:** process and results only. The source code and the frozen parameter set are
deliberately withheld (see [the README](../../README.md#whats-not-here-and-why)).

> Research only. Not financial advice. Nothing here is a recommendation to trade this strategy.

## The idea, at concept level

An intraday opening-range breakout on the US100 CFD, five-minute bars. The range forms over a
short window after the New York cash open; a breakout trades only when the move is large
relative to recent volatility (a Larry Williams–style expansion filter), the range itself isn't
abnormally wide or narrow relative to the previous day, and the exit is a fixed R-multiple target
with a breakeven move once the trade is far enough in profit.

v1.0 was the version tested against four Larry Williams additions in the
[ablation study](../orb-larry-williams-ablation/README.md); none of them beat it, so it was
carried forward unchanged. v1.1 made two changes:

1. **The range-width filter became volatility-normalised** (a percentage of the previous day's
   range rather than a fixed number of points), removing the non-stationarity of an absolute
   threshold on an index that moved several thousand points over the test period.
2. **"Cushion" sizing for funded accounts.** Risk per trade scales with the distance between
   equity and the account's hard drawdown floor (CPPI-style), so a losing streak automatically
   shrinks size before it can breach a prop-firm limit.

## How it was selected (in-sample, Nov 2020 → Nov 2023)

A 140-pass grid over four dimensions plus a 20-pass extension at one boundary. **All 160 passes
were profitable**, which is the first robustness signal: the edge didn't depend on a lucky cell.

The parameter set was chosen from **per-dimension marginals of Calmar at the plateau centre**,
not from the single best pass. That choice was written down before the out-of-sample run along
with a warning: ranking by net profit (the platform's default "fitness") would have picked the
high-trade, low-quality corner of the grid, 528 trades at PF 1.32 with 12%+ drawdown.

In-sample result of the frozen set: **+91.5% over 3 years, PF 1.53, 351 trades, 5.29% balance
drawdown, Calmar 5.76.**

## The pre-registered out-of-sample test (Nov 2023 → Jul 2026)

Gates written before the run, one shot, result final:

| # | Gate | Result | |
|---|---|---|---|
| 1 | Profitable | +31.35% | ✅ |
| 2 | PF ≥ 1.25 | 1.34 | ✅ |
| 3 | Balance drawdown ≤ 9% | 8.71% | ✅ (0.29 pp margin) |
| 4 | **Calmar ≥ 2** | **1.35** | ❌ |
| 5 | Positive in ≥ 2 of 3 calendar years | 2024 −3.1%, 2025 +19.5%, 2026 +8.6% | ✅ |

| | In-sample | **Out-of-sample** |
|---|---|---|
| Return | +91.5% (3 y) | **+31.35%** (2.67 y) |
| Profit factor | 1.53 | **1.34** |
| Trades | 351 | **238** |
| Win rate | 59% | **53.8%** |
| Max balance drawdown | 5.29% | **8.71%** |
| **Calmar** | **5.76** | **1.35** |

**Calmar fell 4.3×.** That's the expected signature of choosing the peak region of a 160-pass
grid, and the risk was flagged in advance. Stricter Calmar conventions (CAGR over balance or
equity drawdown) give 1.23 and 1.10, so the gate fails on every definition.

## What the result changed

- **The short side is the profitable side.** Shorts made 72% of OOS profit (PF 1.49 vs 1.19 for
  longs). A small live evaluation had suggested the opposite (10 shorts lost, PF 0.17); on 238
  trades that was variance, not a broken component. The note in the results says it plainly:
  don't "fix" the short side.
- **Judging a live run against the right benchmark.** The live evaluation (18 trades, PF 0.56)
  looked significantly bad against the *in-sample* expectation (t ≈ −2.4, p ≈ 0.03). Against the
  *out-of-sample* expectation, which is what the strategy should actually deliver, it's
  t = −1.66, p ≈ 0.10: an ordinary bad run.
- **Risk had to come down before any prop account.** Peak-to-trough equity drawdown was 9.78%
  against a 10% overall limit. Calmar doesn't change with size, so the fix is halving risk, which
  makes the strategy survivable rather than better.
- **A caveat found in the export:** commissions weren't applied in the OOS run (swaps were). A
  rough estimate cuts net to about +27.5% and Calmar to about 1.18, which makes the failed gate
  worse, not better. Recorded as "re-run with commissions before any sizing decision".

**The OOS window is now spent.** Any parameter change informed by it would turn the whole
exercise back into an in-sample fit.
