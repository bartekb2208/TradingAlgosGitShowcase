# Larry Williams, tested: four additions to an opening-range breakout

**From an earlier research repository, June 2026. Verdict: none of the four additions beat
the baseline. The baseline was carried forward and became [ORB 1.1](../survivors/orb-1.1.md).**
**What's published:** the experiment design and results. The five cBots are withheld, because each
variant is "the baseline plus one idea", so publishing any of them would publish the baseline,
which is still a [survivor](../survivors/orb-1.1.md).

> Research only. Not financial advice.

## The question

Larry Williams' *Long-Term Secrets to Short-Term Trading* is one of the most-cited practitioner
books on short-term index trading. If its ideas are as good as their reputation, bolting any one
of them onto a working breakout system should improve it. So: one baseline, four variants, each
changing exactly one thing, all run over the same window on three instruments.

| Variant | The single change | Source in the book |
|---|---|---|
| **Bailout exit** | Drop the fixed target; exit on the first bar that closes in profit after N bars | Ch. 11, 14: fixed targets dampen a trend system |
| **Extension filter** | Refuse breakouts that have already travelled a large fraction of a typical day's range, or follow a long same-direction run | Ch. 2: a blast-off needs room to run |
| **Greatest Swing Value** | Replace the expansion trigger with Williams' average "failure swing" measure | Ch. 8 |
| **Trade-day-of-week** | Longs only Mon–Wed, shorts only Thu–Fri (his index findings) | Ch. 4, 6 |

## Results

cTrader backtests, five-minute bars, $10,000 starting balance, Jan 2022 → Jun 2026, commissions
and swaps included.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../../docs/img/lw-ablation-dark.png">
  <img src="../../docs/img/lw-ablation-light.png" alt="Net P&L by variant for NAS100, US500 and XAUUSD. The baseline is best or tied on every instrument; every variant loses on gold.">
</picture>

| Variant | NAS100 | US500 | XAUUSD |
|---|---|---|---|
| **Baseline** | +14,945 · PF 1.36 · 741 tr · DD 8.3% | +10,288 · PF 1.23 · 756 tr · DD 11.2% | −984 · PF 0.96 · 682 tr · DD 22.1% |
| + Bailout exit | *no clean record* | −2,010 · PF 0.77 · 759 tr · DD 23.9% | −2,034 · PF 0.75 · 682 tr · DD 23.9% |
| + Extension filter | +13,691 · PF 1.33 · 744 tr · DD 8.6% | +9,618 · PF 1.22 · 759 tr · DD 11.2% | −1,713 · PF 0.92 · 679 tr · DD 28.9% |
| + Greatest Swing Value | −299 · PF 0.90 · 113 tr · DD 8.0% | +109 · PF 1.03 · 110 tr · DD 5.9% | −154 · PF 0.92 · 96 tr · DD 5.3% |
| + Trade-day-of-week | +8,015\* · PF 1.52 · 390 tr · DD 15.1% | +4,533 · PF 1.27 · 396 tr · DD 11.5% | −1,223 · PF 0.89 · 343 tr · DD 25.2% |

## What each one did

- **Bailout exit turned a working system into a textbook trap.** On US500 it won **654 of 759
  trades (86%)** and still lost money, PF 0.77. Taking the first profitable close cuts every
  winner short while losers run to the full stop. A high win rate is not an edge.
- **The extension filter was close to neutral**, slightly worse everywhere. The late,
  "over-extended" breakouts it refused were apparently carrying their share of the profit.
- **Greatest Swing Value starved the system.** Trade count fell from ~750 to ~110 and the edge
  went with it.
- **Trade-day-of-week halved the trades.** On NAS100 it raised PF (1.52 vs 1.36) but cut net
  profit and nearly doubled drawdown.
- **Gold lost for every variant, baseline included.** The breakout logic doesn't transfer to
  XAUUSD. Worth remembering: a later, unrelated strategy ([ORB First-Move](../orb-first-move/README.md))
  was run on gold anyway and produced the worst result in this repository.

## A data-integrity problem, and how it's handled

Each result was recorded as a screenshot of cTrader's "Trade statistics" tab. Three of the NAS100
screenshots, saved in the *Bailout*, *Greatest Swing* and *Trade-day-of-week* folders, are the
**same image**: identical to the cent, +8,015.46 on 390 trades. Only one of those runs actually
produced it.

The figure is attributed to **trade-day-of-week** by trade count. Removing two weekdays per
direction should roughly halve the baseline's ~740 trades, and the TDW run on US500 did exactly
that (396). The bailout exit changes only the exit, so it should trade about as often as the
baseline, which rules it out. Greatest Swing Value has its own, different NAS100 screenshot. The
Bailout NAS100 result is therefore **missing**, and the table says so instead of reusing a number
that belongs to another run.

## Why this study changed how everything after it was done

Everything above is a single in-sample run per cell. No window was held out, no gate was written
before the run, and nothing stops a reader (or me) from picking the cell that looks best after
the fact. It was a fair way to reject four ideas, because none of them got close, but it couldn't
have validated one. From here on, anything that was a candidate for real money got a
[pre-registration](../../docs/methodology.md) and a sealed out-of-sample window, starting with
the move from ORB 1.0 to 1.1.
