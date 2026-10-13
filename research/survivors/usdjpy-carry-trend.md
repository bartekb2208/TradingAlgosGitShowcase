# UsdJpyCarryTrend — long-only trend-following on the yen-carry drift

**Status: passed all four pre-committed out-of-sample gates; graduated to forward demo as a
small diversifier. Profit is heavily concentrated in one year.**
**What's published:** process and results only. The source code and frozen parameters are
withheld (see [the README](../../README.md#whats-not-here-and-why)).

> Research only. Not financial advice. Nothing here is a recommendation to trade this strategy.

## The idea, at concept level

USDJPY has paid a large positive carry to longs for years, and FX time-series momentum is one of
the better-documented effects in the literature (Lustig et al. 2011, Koijen et al. 2018 on carry;
Moskowitz, Ooi & Pedersen 2012 and Menkhoff et al. 2012 on FX momentum). The hypothesis: a
long-only trend follower on USDJPY H1 harvests both, provided it gets out of the way when the
carry trade unwinds.

Mechanics, at concept level: a moving-average trend filter; entry on a pullback followed by a
resumption close; an ATR-based initial stop; an ATR chandelier trail with no fixed target. Prop-
firm plumbing sits around it: a carry filter (long only while the swap is positive), a news
blackout that pulls the ForexFactory calendar live, a rollover blackout, and daily-loss and
losing-streak caps.

## How it was built (in-sample, Apr 2020 → Apr 2023)

Optimisation ran in stages, each frozen before the next:

- **Stage A** compared three exit architectures (fixed target, partial-plus-runner, trail-only).
  Trail-only won.
- **Stage B** gridded entry and trail parameters. The best trail value sat on the edge of the
  grid, which is a warning sign: an optimum on a boundary may just mean the grid was too small.
- **Stage C** extended the grid past that boundary on tick data. Net-to-drawdown fell steadily
  past the original value in both entry rows, so the optimum was genuinely interior. The
  pre-registered plateau centre was kept **over a statistically tied neighbour**, to avoid picking
  between equals after looking.

## The out-of-sample test (Apr 2023 → Jul 2026, walled off before optimisation began)

| Metric | Result | Gate |
|---|---|---|
| Net profit | +13.03% on 100k | > 0 ✅ |
| Profit factor | 1.23 | > ~1.15 ✅ |
| Trades | 176 (39.2% win) | — |
| Balance drawdown | 5.12% (equity 6.59%) | ≤ ~9% ✅ |
| July 2024 carry unwind | small bleed over 6 trades, then stood aside ~7 weeks | small bleed ✅ |
| Sharpe (daily, annualised) | 0.63 (in-sample was 1.11) | — |

**A confounded first run, kept on record.** The first OOS run had the news filter in a mode that
force-flattened positions on recurring Wednesday/Friday slots, which the in-sample run never had.
It failed the PF gate (1.09). The re-run, config-identical to in-sample, is the one that counts,
and the difference between them prices the news force-flat at roughly **half the edge**.

## What the analytics after the test showed

- **News avoidance is expensive.** Trades that spanned real NFP or FOMC days carried about a
  third of total profit; the single best trade was entered on an FOMC day. Force-flat on news is
  kept only as compliance insurance on accounts that require it.
- **The realised drawdown was order-luck.** Shuffling trade order (10k iterations) gives a median
  max drawdown of 7.2%, 95th percentile 11.2%; bootstrapping gives a 15% chance of a 3-year loss
  and a 29% chance of any given year losing.
- **About 103% of OOS profit came from 2024.** The other years were roughly flat. That's the
  honest weakness here, and it's why the strategy is run as a small sleeve (~0.25% risk) rather
  than standalone.
- **An independent replication.** A deliberately minimal sibling (a plain breakout entry with the
  same exit idea, no filters) also survived the same window (PF 1.32) but had a **quarterly
  correlation of +0.87** with this one. Same pair, same direction, same regime: evidence the family
  is robust, not a second strategy. It was shelved as evidence rather than deployed.
