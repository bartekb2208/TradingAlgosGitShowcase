# Band-fade mean reversion (AUDNZD)

**Verdict: falsified. PF 0.56 against a pre-registered falsifier of 1.05; abandoned without a sweep.**
**Code:** [`cbots/MeanRevBandFadeBot.cs`](../../cbots/MeanRevBandFadeBot.cs) · **Results:** [`RESULTS.md`](RESULTS.md)

> Research only. Not financial advice.

Two-sided mean reversion on a cointegrated FX cross: enter when price is stretched about two standard
deviations from its 20-bar mean *and* RSI(2) shows exhaustion, unless a higher-timeframe ADX
says the market is trending. Exits are deliberately plain: mean touch, a 48-bar time stop, or a
hard ATR stop. No breakeven, no trail, no partials.

The run that killed it used the wrong date window by mistake (an earlier campaign's dates were
left in the platform's date picker). [`RESULTS.md`](RESULTS.md) keeps that on the record and
explains why it doesn't change the verdict: the two years that fall inside the registered window
are the two worst.

The failure has a clean anatomy, also in the results: entries at ±2σ happen when the band is
wide; by the time price touches the mean the band has contracted, so wins are systematically about
half the size of losses (−0.17R per trade before swaps).
