# How an idea gets tested

Backtests are very good at producing a number and very bad at telling you whether to believe
it. Almost every way a backtest misleads comes down to the researcher looking at the answer before
deciding what the question was. Everything below exists to make that harder to do by accident.

None of it is original. It's standard practice in empirical science (pre-registration, held-out
data, null models) applied to a field where it's routinely skipped. What's worth showing is that it
was actually followed, including the times it produced an inconvenient answer.

---

## 1. Write the kill condition down first

Every serious idea gets a pre-registration before any out-of-sample data is read
([template](hypothesis-template.md)). It's dated, and it states:

- **The hypothesis**, with a mechanism for why the effect might exist and a literature anchor
  where one exists ([DailyDipSwing](../research/survivors/daily-dip-swing.md) borrows Nagel 2012's
  liquidity-provision argument, and the argument becomes a test of its own).
- **A frozen definition.** Every rule, with every judgment call labelled as one. Where the source
  is discretionary prose, the translation is part of the document
  ([FVT](../research/fvt-v1/DESIGN.md) decodes a "displacement candle" from the source's own worked
  examples, to the cent).
- **Datasets and their roles.** Which data tunes, which data tests, which is sealed for one final
  read, and the dates of each.
- **Gates and falsifiers, with numbers.** "Profit factor below 1.05 → abandon without a sweep."
  Not "if it looks weak."
- **What is explicitly not claimed.** A pass on ES is evidence about an entry model, not proof
  about NQ; a long-only strategy that survives bear markets by mostly not trading has not shown
  bear-market skill.

Once results exist, the document doesn't change. A changed rule, grid or partition is a new
pre-registration with the trial count incremented
([V2 PREREG](../research/v2-order-book/PREREG.md)).

## 2. When a falsifier fires, stop

The most tempting move after a failed test is a parameter sweep to find the version that would
have passed. That turns an out-of-sample result back into an in-sample fit. So when the falsifier
fires, the line is **abandoned without a sweep**, and the post-mortem says why it failed rather
than how it could have passed. [Band-fade](../research/band-fade/RESULTS.md),
[TPO fade](../research/tpo-value-fade/README.md) and [FVT](../research/fvt-v1/README.md) all ended
this way.

The same discipline applies in the other direction. [MR V1](../research/mr-v1-connors-rsi2/README.md)
missed its 100-trade bar by six trades with a pooled PF of 1.56. It's recorded as a fail.

## 3. Keep the test data physically out of reach

Leakage is prevented by structure, not by intention. In
[`crossval.py`](../python/crossval.py) the tune and test sets live in different directories and
the test directory is never opened during tuning. Out-of-sample windows are walled off before
optimisation starts, and **read once**. After that they're spent: ORB 1.1's results say so in as
many words.

Every decision for bar *T* reads bars ≤ *T−1*, and a signal fills at the next bar's open, never on
the bar that produced it. FVT's swing detector is lookahead-free by construction: a pivot at bar
*p* can't be reported until bar *p+k* has printed, because the tracker never sees a bar it hasn't
been given. The engine selftests pin the rest down: next-bar entry, a bar that touches both stop
and target resolves to the stop and is counted, a gap through the stop fills at the open, and a
trade still open at the end of the data exits at the last close
([`fvt_engine.py`](../python/fvt/fvt_engine.py)).

## 4. Choose the plateau, not the peak

When there's a parameter grid, the chosen set comes from **per-dimension marginals at the plateau
centre**, not from the single best pass. A peak is the likeliest place to be fitting noise.

- [ORB 1.1](../research/survivors/orb-1.1.md): all 160 passes profitable, set chosen by Calmar
  marginals. The platform's default ranking (net profit) would have chosen the worst-quality corner.
- [UsdJpyCarryTrend](../research/survivors/usdjpy-carry-trend.md): when the optimum landed on a grid
  boundary, the grid was extended to check it was genuinely interior; the plateau centre was then
  kept over a statistically tied neighbour.
- [DailyDipSwing](../research/survivors/daily-dip-swing.md): 99.2% of a 60-cell grid profitable is
  stronger evidence than any single cell, and per-market tuning lost to the frozen config on 7 of
  10 markets.

## 5. Beat a null, not zero

"It made money" isn't the test. The test is whether it made more than something that shouldn't
have an edge:

- **Random entry under the same filters.** For a long-only dip-buyer the null isn't buy-and-hold;
  it's random entries on the days the trend filter allows, with identical exits. That isolates what
  the entry signal adds.
- **Time-shifted entry.** Does the *precise* trigger moment carry information, or would any entry
  nearby do as well? ([`vr_timeshift_control.py`](../python/vr/vr_timeshift_control.py))
- **Other instruments.** Tune on some markets, test on others the config has never seen.
- **Exposure-matched buy-and-hold** for anything that's long most of the time.

## 6. Separate "bad" from "uninterpretable"

A validity gate (enough trades, fills that can be trusted, data that's actually aligned) is not
the same kind of check as a claim test. Failing a validity gate means the result says nothing, which
is different from the strategy being bad. [FVT's report](../research/fvt-v1/artifacts/report.txt)
labels every check as one or the other, and refuses to read a technically passing check as support
when both halves of the comparison lost money.

## 7. Read the raw events, not the summary

Summary statistics hide the failures that matter most. Two of the most important results in this
repository came from opening the event export:

- A −78% run that was mostly an execution bug: stops silently dropped by server-side rounding
  ([ORB First-Move](../research/orb-first-move/README.md)).
- A profitable run that was one 596-lot position produced by a sizing bug
  ([TPO fade](../research/tpo-value-fade/README.md)).

The same rule applies to tooling: under one order-management mode, NautilusTrader's cache reported
1 closed position where the event stream had 28 ([V2](../research/v2-order-book/README.md)).

## 8. Judge live results against the right benchmark

A live or forward-test stretch should be compared with the **out-of-sample** expectation, not the
in-sample one. ORB 1.1's 18-trade live evaluation looked significantly bad against in-sample
(p ≈ 0.03) and ordinary against out-of-sample (p ≈ 0.10). The benchmark choice is the result.

## 9. Make results reproducible and comparable

- **Engines are versioned**, and the version is stamped into every artifact. Results from different
  engine versions aren't pooled ([V2 engine freeze](../research/v2-order-book/ENGINE_FROZEN.md)).
- **The core modules carry a `--selftest`** (14 of them here), and the eight that don't need
  market data run in CI.
- **Dependencies whose behaviour was verified are pinned.** NautilusTrader is pinned to the exact
  version its loader and accounting semantics were checked against.
