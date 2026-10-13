# MRB falsifier run — MeanRevBandFadeBot, shipped defaults — FAILED

AUDNZD h1, tick data, FTMO, 100k, risk 0.15%. Run 2026-07-10.
NOTE: window was 2020-04-01 → 2023-04-01 (UJ dates left in picker), not the
registered IS 2018-04 → 2022-12. Verdict unaffected (see below).

| Metric | Result | Envelope |
|---|---|---|
| Net | **−10,353 (−10.35%)** | > 0 |
| PF | **0.56** | ≥1.05 to survive; 1.2–1.6 target |
| Trades | 405 (85/yr) | 30–60/yr |
| Win rate | 50.6% | 48–58% ✓ (the only pass) |
| Avg win / avg loss | **0.55** | 1.1–1.6 |
| Yearly | 2020 −866, 2021 −3,758, 2022 −3,930, 2023Q1 −1,799 | ≥3 of 5 positive |
| balDD | 10.97% | ≤2.5% |

Both directions negative (long PF 0.68, short PF 0.47). Median hold 10h. Largest
loss −351 (~2.3× intended 150 risk — AUDNZD spread/gap through stops on tick data).
Swaps −549.

**Why the wrong window doesn't rescue it:** 2021 and 2022 (fully inside the
registered window) are deeply red; flipping full-window PF from ~0.56 to 1.05 would
require 2018–19 to contribute ~+10k = ~5%/yr at 0.15% risk — 2–3× the envelope's own
ceiling. Not plausible. Optional for-the-record run on the exact registered window
allowed, but NO parameter changes either way.

**Autopsy (anatomy of failure):** wins are systematically ~half the size of losses —
entries at ±2σ occur when the band is WIDE (vol expansion); by mean-touch the band
has contracted, so the win is small, while the 2.5×ATR stop and time-stop losses are
full-sized. Negative expectancy ≈ −0.17R/trade before swaps. The H4 ADX<25 veto did
not keep the bot out of the 2021–22 AUD/NZD policy-divergence trends.

**Per pre-registered protocol: falsifier fired → ABANDONED without a sweep.**
The QuantAnalyzer-line reconstruction lesson: the *family* recovered from public
stats does not itself carry the edge on live-spread tick data — those portfolio
lines' smoothness was selection/modeling artifact, or their edge lives in specifics
that public stats cannot leak. Which is the answer to the original challenge.
