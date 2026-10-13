# V-Reversal — an order-flow footprint setup on ES market-by-order data

**Verdict: closed as unmeasurable, not falsified. The setup is too rare to test: 24 structures
in 23 sessions against a pre-registered need for 100.**

> Research only. Not financial advice.

The source is Cody Bengtson's *V-Reversal Setup* (2023), a discretionary footprint method:
heavy sell aggression into the bid, absorption, then a sharp reversal. The document itself isn't
redistributed here.

| File | What it is |
|---|---|
| [`PREREG.md`](PREREG.md) | Signal pre-registration, written before any out-of-sample data was read |
| [`DESIGN.md`](DESIGN.md) | Detector and strategy design, falsifiers F1–F3 |
| [`RESULTS.md`](RESULTS.md) | Results and the closure decision |
| [`artifacts/`](artifacts/) | Every run's report, including the random-entry and time-shift controls |

**Code:** [`python/vr/`](../../python/vr/), built on the NautilusTrader rig in `python/v2/`.
[`vr_random_control.py`](../../python/vr/vr_random_control.py) and
[`vr_timeshift_control.py`](../../python/vr/vr_timeshift_control.py) are the null models: does the
signal beat random entry, and does the *precise* trigger moment carry information?

The headline lesson is in RESULTS §3: on the 5 exploratory sessions the best configuration made
+$1,562 on 5 trades, PF 3.28. On all 23 sessions the same configuration took 16 trades at PF 0.83
and lost $567. Tripling the sample didn't sharpen the estimate, it changed its sign; the
exploratory sessions had simply been 62% richer in setups than the population. The 9 sealed out-of-sample sessions were never read, because there was no frozen candidate
worth spending them on.
