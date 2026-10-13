# cBots (C# / cAlgo.API)

Ten cTrader Automate bots, about 6,900 lines of C#. **Every one of them failed its test or was
abandoned untested**, which is why it's published. The bots that survived aren't here.

> Research only. Not financial advice. Don't run these on a live or funded account.

| Bot | Idea | Outcome |
|---|---|---|
| [`OrbFirstMoveBot.cs`](OrbFirstMoveBot.cs) | Mechanised discretionary opening-range framework, m1 | Falsified, PF 0.66. **Read this one first:** it carries the execution-safety layer from the [−78% post-mortem](../research/orb-first-move/README.md) |
| [`TpoValueFadeBot.cs`](TpoValueFadeBot.cs) | Fade failed probes beyond the prior session's value area back to POC, risk-capped scale-in ladder | Falsified; the "profitable" first run was [one 596-lot position](../research/tpo-value-fade/README.md) |
| [`MeanRevBandFadeBot.cs`](MeanRevBandFadeBot.cs) | ±2σ z-score + RSI(2) fade on AUDNZD, higher-timeframe ADX veto | [Falsified](../research/band-fade/RESULTS.md), PF 0.56 |
| [`OrderFlowJumpDiffusionBot_CVD.cs`](OrderFlowJumpDiffusionBot_CVD.cs) | Merton jump-diffusion with online Lee–Mykland jump detection, confirmed by tick-rule cumulative delta | Failed (early; no report kept) |
| [`OrderFlowJumpDiffusionBot_DOM.cs`](OrderFlowJumpDiffusionBot_DOM.cs) | Same jump layer, confirmed by depth-of-market imbalance | Never tested: DOM doesn't exist in cTrader's backtester |
| [`TrendVolBreakoutBot.cs`](TrendVolBreakoutBot.cs) | Donchian breakout with ATR volatility-scaled risk, regime EMA and ADX chop filter | Failed (early; no report kept) |
| [`IctLiquiditySweepFvgBot.cs`](IctLiquiditySweepFvgBot.cs) | ICT liquidity sweep → displacement → fair-value gap, GBPJPY M15, NY killzones | Failed (early; no report kept) |
| [`SmcStackBot.cs`](SmcStackBot.cs) | Full Smart Money Concepts pipeline: H4 structure bias → M15 sweep → zone → LTF entry | Abandoned untested |
| [`OvernightDriftBot.cs`](OvernightDriftBot.cs) | NY Fed Staff Report 917 (Boyarchenko, Larsen & Whelan): long S&P during the European open, conditioned on closing order flow | Abandoned, no recorded verdict |
| [`EmaCrossTrendBot.cs`](EmaCrossTrendBot.cs) | Long-only dual-EMA trend following on BTC, cross-tested on ETH/SOL | Abandoned, no recorded verdict |

The order-flow pair, `TrendVolBreakoutBot`, `IctLiquiditySweepFvgBot` and `SmcStackBot` were the
first bots written and predate the [methodology](../docs/methodology.md). `OvernightDriftBot` and
`EmaCrossTrendBot` were dropped later without a write-up. None of these has a pre-registration or
a surviving report, and the table doesn't pretend otherwise.

## Things worth reading in the code

- **Execution safety** (`OrbFirstMoveBot`): ceilinged pip stop on the order, exact stop set by
  price after the fill, `StopLoss` verified, immediate flatten if a position can't be protected,
  notional cap, fast-tape entry guard, and bars resolved by timestamp instead of index.
- **Online jump detection** (`OrderFlowJumpDiffusionBot_*`): the Lee–Mykland statistic
  `|r| / σ_local` against a Gumbel critical value, with `σ_local` a jump-robust **bipower**
  volatility over a window that ends *before* the tested return, and Merton parameters re-estimated
  on a rolling window.
- **Self-contained DST handling**: the bots compute US (and, for `OvernightDriftBot`, EU) daylight
  saving transitions themselves, including the weeks when the two are out of sync, rather than
  trusting the platform's time zone.
- **Risk plumbing throughout**: fixed-fractional sizing at the structural stop, spread filters
  expressed relative to the stop or ATR (so they work across instruments), daily-loss caps,
  losing-streak halts, max trades per day, one position at a time.

## Compiling

cTrader compiles each cBot from its own source, so there's normally no project file. Helper types
are prefixed per bot (`Ofm…`, `Tvf…`, `Cvd…`) so that all ten can share one assembly without name
clashes, which is what makes a single compile check possible:

```bash
dotnet build cbots
```

That builds every bot against the real `cAlgo.API` from the
[`cTrader.Automate`](https://www.nuget.org/packages/cTrader.Automate) NuGet package (net6.0, the
runtime cTrader uses). It currently succeeds with **5 warnings**, all obsolete-API calls in the two
early order-flow bots, left as they were written. CI runs the same build on every push.

To backtest one, paste its source into a new cBot in cTrader Automate.
