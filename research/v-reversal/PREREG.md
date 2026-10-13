# V-Reversal footprint signal — pre-registration

**Written 2026-07-25, BEFORE looking at any out-of-sample data.** Thresholds are frozen
from the 6-session exploratory sample (ES, 2026-07-06 → 07-13). Nothing below may be
changed after seeing OOS results; if something must change, it becomes a NEW
pre-registration with the trial counted.

## Hypothesis
From Cody Bengtson's *V-Reversal Setup* (2023; third-party document, not redistributed here): heavy sell aggression into the bid
("blowoff") that occurs while the resting book is bid-heavy at the extreme
(imbalance = trapped sellers) is followed by upward reversion. The doc's claim is a
CONJUNCTION — neither component alone is the signal.

## Exploratory result that motivates this (6 sessions, ~140k 1-second buckets)
After collapsing overlapping buckets into independent events (>=180s apart):

| signal | indep n | mean fwd120 (ticks) | % up |
|---|---|---|---|
| baseline (all buckets) | 139,680 | +0.199 | 48.0% |
| blowoff alone (vol_sell >= p99) | 371 | +0.12 | 46.9% |
| blowoff alone (>= p99.9) | 76 | +0.18 | 50.0% |
| imbalance alone (imb_top5 >= p99) | 153 | +0.00 | 49.0% |
| imbalance alone (>= p99.9) | 19 | −0.47 | 47.4% |
| **CONJUNCTION (sell p99 & imb p90)** | **142** | **+1.60** | **55.6%** |
| CONJUNCTION (sell p99 & imb p95) | 90 | +1.23 | 56.7% |

Standalone effects are AUTOCORRELATION ARTIFACTS (raw +3.69 / +1.81 collapse to ~0
once overlapping 1-second buckets are deduplicated). Only the conjunction survives —
which is the pattern the setup doc predicts.

**Why this is not yet a finding:** per-session means are +5.25 / −4.93 / +6.43 / −0.54 /
+1.19 / +1.31. Two sessions carry the whole result, two are negative. n=142 over 6 days
is not distinguishable from noise.

## Frozen event definition (do not tune)
- Data: ES front month (per-session `instrument_id` by volume), RTH only, 1-second buckets.
- Book: `quantlab.lob` via `lob/catalog.py` (tick-keyed; post tick-precision fix).
- Signal: `vol_sell >= p99` AND `imb_top5 >= p90`, percentiles computed **within the
  evaluated sample** (not carried from exploration — they are relative, not absolute).
- Independence: events >= 180s apart, first occurrence kept.
- Outcome: `fwd120` = mid change 120s later, in ticks. Baseline = all buckets in sample.

## Datasets and roles (fixed)
- **ES 2026-06-07 → 07-23 (~33 sessions).** Sessions 07-06..07-13 are EXPLORATORY
  (already used). The remaining **~27 sessions are the OUT-OF-SAMPLE test**.
- **NQ 2026-04-06 → 05-06 (~22 sessions).** Robustness check. NOTE: it does NOT overlap
  the ES window, so instrument and regime are confounded. It can support the result; it
  cannot cleanly attribute a difference.

## Success criteria (fixed in advance)
Primary (ES OOS, ~27 sessions):
1. Conjunction mean `fwd120` **> +1.0 ticks**, AND
2. **>= 4 of 6** two-week blocks positive (consistency matters more than the mean —
   inconsistency is exactly how the exploratory sample fails), AND
3. Conjunction beats BOTH standalone components (which should stay ~0, as they did).

Secondary (NQ): same event definition, mean `fwd120` > 0 and >= 50% of blocks positive.

## Falsification
If the primary criteria fail, the footprint V-Reversal signal is **not supported** on
ES. Do NOT re-tune thresholds on the OOS data and re-test — that converts the test into
a search. A genuinely different hypothesis is a new pre-registration with the trial
count incremented.

## Explicitly NOT claimed here
This measures whether a *signal* exists, not whether a *strategy* is tradeable. No entry
rule, stop, target, or cost model is involved. +1.60 ticks ≈ $20/contract gross on ES
against ~1 tick ($12.50) round-trip cost — so even a confirmed signal is close to the
cost floor and would need a real execution design before any conclusion about profit.
