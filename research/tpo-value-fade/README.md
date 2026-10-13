# TPO value-area fade — the profitable backtest that was one position

**Verdict: falsified. Abandoned without a parameter sweep, as pre-registered.**
**What's published:** [`cbots/TpoValueFadeBot.cs`](../../cbots/TpoValueFadeBot.cs).

> Research only. Not financial advice.

## The idea

Auction market theory (Steidlmayer, Dalton): the prior regular session's time-at-price profile
defines a value area (VAH/VAL) and a point of control (POC). When price probes beyond value and
fails, closing back inside, it should rotate back toward the POC. The bot builds the prior New York
RTH profile from five-minute bars, fades failed probes back to POC with a server-side target, and
can scale in up to three times. Each rung is sized so that **the whole ladder at its shared stop
risks exactly the aggregate cap (0.5%)**, so the worst case is fixed before the first entry.

It replaced an earlier, falsified [band-fade](../band-fade/RESULTS.md) idea with a different
hypothesis family (auction structure rather than indicator stretch). Pre-registered: in-sample
Nov 2020 → Nov 2023 on US500, falsifier **PF < 1.05 → abandon without a sweep**.

## Run 1: +3,567

The first run finished up 35.7% on a $10k account. The headline looked like the first winner of the
cycle. It also reported a **97.9% maximum equity drawdown**, which doesn't fit a 0.5%-risk ladder.

The event export explained it. The median position was 1 lot. **One position was 596 lots.**
Something in the sizing path let that trade escape the ladder's risk cap. It closed **+4,974**
gross; the other 1,677 trades lost **−783** between them. One oversized position produced both
the near-wipeout intrabar and more than all of the profit.

## Run 2, with the sizing fixed: −1,111

| | Run 1 (bug) | Run 2 (fixed) |
|---|---|---|
| Net | +3,567 | **−1,111** |
| Closed trades | 1,678 | 1,678 |
| Win rate | 71.3% | 71.5% |
| Gross profit factor | 1.55 | **0.88** |
| Largest position | 596 lots | 5 lots |
| Max equity drawdown | 97.9% | 12.1% |

1,146 of the 1,678 trades hit their POC target. The strategy really does revert to value 70% of
the time, and it still loses: the 30% that don't revert cost more than the 70% that do. The
falsifier fired and the line was closed.

## What happened next

The post-mortem's conclusion was that the idea might need things a CFD backtest can't provide:
real exchange volume, a real order book, and futures cost structure. A successor on ES futures
(VWAP-band fade with acceptance/rejection rules) is pre-registered but hasn't been run, so it isn't
published here.
