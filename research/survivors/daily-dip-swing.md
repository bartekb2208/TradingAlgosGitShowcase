# DailyDipSwing — a daily-bar dip buyer, validated on 10 indices × 35 years

**Status: edge confirmed, and small. Calmar ≈ 0.36 at every leverage, so it is only usable as a
diversifier.**
**What's published:** process and results only. The MQL5 Expert Advisor, the independent Python
reimplementation and the frozen parameter set are withheld (see
[the README](../../README.md#whats-not-here-and-why)).

> Research only. Not financial advice. Nothing here is a recommendation to trade this strategy.

## The idea, at concept level

A Connors-family short-term mean-reversion rule (Connors & Alvarez, 2008): in a long-term
uptrend, buy after a short-term low; exit on the first strong close; keep a far disaster stop
that almost never fires. There's no academic paper for this exact rule. The closest anchor is
Nagel (2012), *Evaporating Liquidity* (Review of Financial Studies), which argues that short-term
reversal returns are compensation for providing liquidity, and should therefore be larger when
volatility is high. That prediction became a pre-registered falsification test.

## Stage 1 — one instrument, MT5 (US500 CFD, daily)

| | In-sample 2018–2022 | Out-of-sample 2023–2025 |
|---|---|---|
| Profit factor | **2.03** | **1.88** |
| Win rate | 80.0% | 73.2% |
| Max equity drawdown | 2.57% | 2.15% |
| Trades | 50 | 56 |

Falsifier was PF < 1.05; both windows cleared it. But the OOS window was bull-only, which makes it
necessary rather than sufficient.

**The exit A/B taught the main lesson.** Every embellishment made it worse. A managing stop, a
chandelier trail, and a trail-only variant (PF 1.11, which gave the bigger wins and destroyed the
edge) all underperformed the plain momentum exit. Mean reversion is spent in about three days;
holding longer hands the profit back.

## Stage 2 — 10 indices, 33–35 years each, 5,306 trades

The rules were ported from the MQL5 source into an independent Python engine with a 13-check
selftest (including "a signal on the final bar can't be traded"). **Only SPX ever informed a
parameter; the other nine indices are out-of-sample for the config.**

| idx | yrs | trades | win% | PF | t-stat |
|---|---|---|---|---|---|
| SPX | 35 | 589 | 73.2% | **1.83** | **5.23** |
| NDX | 35 | 609 | 70.1% | **1.73** | **4.81** |
| DJI | 34 | 520 | 69.4% | 1.40 | 2.77 |
| DAX | 35 | 620 | 65.6% | 1.39 | 2.76 |
| FTSE | 35 | 532 | 69.7% | 1.37 | 2.56 |
| STOXX | 17 | 252 | 71.8% | 1.43 | 1.94 |
| N225 | 35 | 503 | 64.8% | 1.26 | 1.84 |
| ASX | 33 | 628 | 63.4% | 1.14 | 1.15 |
| RUT | 35 | 563 | 63.6% | 1.07 | 0.57 |
| HSI | 35 | 490 | 64.7% | **0.99** | −0.07 |

### Is it an edge, or is it beta?

The null isn't buy-and-hold: the strategy is flat about 77% of the time. The null is **random
entries drawn only from days the trend filter allows**, with identical exits. That holds the trend
filter fixed and isolates what the dip condition adds. The statistic is mean return per trade,
because random entry sets overlap more and get skipped more, which would bias a net-profit
comparison about 15% in the strategy's favour.

- **8/10** indices beat the null; median edge **1.57×**
- **3/10** reject at p < 0.05 (SPX, NDX, FTSE)
- RUT and HSI show no edge at all

### Other pre-registered checks

- **Regime walk-forward:** nine bear windows per index (1990 → 2022). Zero of 76 regime-index cells
  breached the −8% limit, worst −4.28%. But it **loses in bears, it just loses little**. That's a
  pass on the stated terms, not evidence of bear-market skill, and the write-up says so.
- **Should we tune per index?** A 60-cell grid over trend length, stop width and dip depth, tuned
  on each index's oldest 60% and tested on the newest 40%. Tuning beat the frozen config on only
  3/10. More useful: **a median 99.2% of grid cells are profitable**. The edge is a plateau, not
  a spike. RUT (20% of cells) is the one market where the rule doesn't work.
- **Nagel falsifier:** rank correlation between pre-entry volatility and trade return is positive
  in **14/14** tests (median +0.134); strictly monotone terciles in only 4/14. Directionally
  supportive, weak in magnitude. The low-volatility 2010s were the weakest decade, which is what
  the mechanism predicts (that decade split is post-hoc and labelled as such).

## The number that decides tradability

| notional | CAGR | max DD | Calmar |
|---|---|---|---|
| 20% | 1.05% | 2.96% | 0.35 |
| 100% | 5.19% | 14.46% | 0.36 |
| 200% | 10.21% | 28.06% | 0.36 |

Calmar doesn't change with leverage: a 10% CAGR costs a 28% drawdown. Costs aren't the binding
constraint (the edge survives to about 35 bps round trip, versus the microstructure work in this
repo where cost was 57× the edge). The risk-adjusted return is.

**Recommendation recorded at the time:** keep the frozen config, trade SPX and NDX only, exclude
RUT and HSI, size as a diversifier beside uncorrelated strategies, and confirm against the real
broker instrument's fills before any capital.
