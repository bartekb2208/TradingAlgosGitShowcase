# VR V1 (V-Reversal) — Results and Closure

**Track:** V-Reversal, own algorithm track parallel to MR V1/V2
**Source hypothesis:** Cody Bengtson's *V-Reversal Setup* (2023; third-party document, not redistributed here)
**Pre-registration:** `research/v-reversal/PREREG.md`
**Design spec:** `research/v-reversal/DESIGN.md`
**Engine:** `python/vr/`, v0.1.0 · **Artifacts:** `research/v-reversal/artifacts/runs/`
**Closed:** 2026-08-05
**Status: CLOSED — UNMEASURABLE, not falsified.**

---

## 1. Verdict in one paragraph

VR V1 does not have a demonstrated edge, and more importantly **the setup as the
source document specifies it is too rare to measure on any data we can realistically
obtain.** Across 23 ES sessions the detector produced 24 ARMED events, of which only
**7 (29.2%)** possess the geometry the doc requires, and only **2** ever completed the
doc's full entry sequence. Two trades. The strategy we *did* test — entering at the V
retrace rather than at the doc's breakout — is statistically indistinguishable from
zero (best t-stat across six configurations: **0.47**). The 9 sealed OOS sessions were
never read and should not be spent.

---

## 2. What was tested

| | |
|---|---|
| Instrument | ES front month (ESM6), L3 MBO + L2 MBP-10 |
| Sessions | 23 trainable (5 exploratory + 18 DEV); 9 OOS **sealed, unread** |
| Excluded | `2026-06-19` Juneteenth half session; `2026-07-10` (82.8% crossed L3 book) |
| Structure rate | **0.74–1.04 / session** |
| Engine | NautilusTrader 1.230.0, L3_MBO book, queue position, $2.50/side |

---

## 3. Headline P&L — 23 sessions, all configurations

| config | n | win% | mean $ | PF | Sharpe\* | **t-stat** | maxDD $ | DD/net |
|---|---|---|---|---|---|---|---|---|
| trail3.0 RR none | 17 | 41.2% | +3.09 | 1.021 | 0.010 | **0.04** | 2,372 | 45.2× |
| trail3.0 RR 0.75 | 6 | 50.0% | −27.92 | 0.785 | −0.121 | −0.30 | 757 | 4.5× |
| trail3.0 RR 1.00 | 5 | 60.0% | −5.00 | 0.961 | −0.020 | −0.04 | 615 | 24.6× |
| trail5.0 RR none | 16 | 37.5% | −35.47 | 0.834 | −0.090 | −0.36 | 3,392 | 6.0× |
| trail5.0 RR 0.75 | 6 | 50.0% | +34.58 | 1.255 | 0.113 | 0.28 | 795 | 3.8× |
| trail5.0 RR 1.00 | 5 | 60.0% | +70.00 | 1.520 | 0.211 | **0.47** | 652 | 1.9× |

\* Per-trade Sharpe reconstructed from a two-point (avgWin/avgLoss) distribution —
the 23-session runs stored aggregates only. Two-point understates dispersion, so
**these are upper bounds**; true Sharpes are lower.

**The largest |t| anywhere is 0.47 against a ~2.0 threshold.** Nothing here is
distinguishable from zero.

### The small-sample reversal that started the closure

| config | 5 sessions | **23 sessions** |
|---|---|---|
| trail 5.0× | n=5, PF **3.281**, +25.40 tk/tr, **+$1,562** | n=16, PF **0.834**, **−2.44** tk/tr, **−$567** |
| trail 3.0× | n=6, PF 1.924, +5.67 tk/tr | n=17, PF **1.021**, +0.65 tk/tr |

Tripling the sample did not refine the estimate — it reversed the sign. The 5
exploratory sessions also ran 1.20 structures/session against a true rate of 0.74,
i.e. they were 62% richer *and* more profitable than the population.

---

## 4. The root cause: we built stage 5 and called it stage 6

The design spec ([lines 30-32](DESIGN.md))
listed six stages. Stage 5 is the V retrace; **stage 6 is a separate entry gate**
("passive back inside the zone, or on a confirmed break of `cons_hi`"), with the
state diagram reading `ARMED --entry rule--> IN_TRADE`.

`vr_strategy._on_armed()` fires synchronously the instant the FSM reaches ARMED,
submits an order, and calls `sm.reset()` — discarding the zone. **Stage 6 was never
built.** All three `entry_mode` values are price selections evaluated at the arming
instant, not conditions to wait for. `VRState.IN_TRADE` is declared and never used.

Because ARMED sits at/below `cons_lo` by construction (measured 0 to −6 ticks), this
forced every downstream compromise:

- the doc's **narrow stop** at `cons_lo` became illegal (it would sit above the entry)
- risk ballooned to a **median 17 / mean 30.8 ticks** ($212–$385 per contract)
- the wide stop forced reliance on a trail, producing 33–66 minute holds against a
  signal measured on a 30-second horizon
- `min_rr`, `stop_cons_mult`, `max_stop_ticks` were all added to bound a risk the
  doc never takes

The empirical work then cemented the error: when passive entry produced 1 fill and
then 0, we concluded "passive entry is geometrically impossible" and defaulted to
crossing the offer. Correct *given the arming moment* — and the wrong lesson.

---

## 5. The decisive measurement (`runs/D_stage6_measurement`)

Question: after our 24 ARMED events, does price go on to do the doc's sequence, and
is that entry better?

**What happens after ARMED**

| outcome | n | % |
|---|---|---|
| broke `cons_hi` | 12 | 50.0% |
| reclaimed `cons_lo` only | 10 | 41.7% |
| tested top, no break | 1 | 4.2% |
| invalidated (new low) | 1 | 4.2% |

**Structure geometry — the finding that closes the track**

The doc's setup requires the consolidation to sit **below** the origin of the thin
move; that gap *is* the room the trade travels into, and `thin_origin` *is* the target.

| | n | % |
|---|---|---|
| ARMED events with `thin_origin > cons_hi` (doc geometry intact) | **7 / 24** | **29.2%** |
| of the 12 breaks — geometry intact | **2** | |
| of the 12 breaks — violated (target below entry) | 10 | |

**71% of what our FSM calls a V-Reversal is not the doc's structure.** `cons_hi`
ratchets upward in `_seek_blowoff` (`vr_core.py:401`) and the rolling window drifts
the zone up, while `thin_origin` stays latched at detection time — so the zone
migrates above the origin it was supposed to have fallen from. This retroactively
explains the "risk 92 ticks to make 14" trades that motivated the whole Tier 1 risk
stack: the target was structurally meaningless.

**Entering at the break is worse than entering at ARMED**

| horizon | ARMED mean | BREAK mean | ARMED %up | BREAK %up |
|---|---|---|---|---|
| 30s | **+3.188** | +1.750 | 62.5% | 58.3% |
| 120s | **+3.021** | −0.167 | 66.7% | 58.3% |
| 300s | **+1.125** | −1.333 | 50.0% | 58.3% |

The narrow stop *is* genuinely tighter (median 11.5 vs 17.0 ticks) — that half of the
thesis held. But the subsequent move is smaller and turns negative beyond 30s. Paying
~10 ticks to enter higher does not buy a better trade.

**The doc's trade, properly defined: n = 2 across 23 sessions.**
One win (+8.60 tk), one loss (−9.40 tk), net −0.8 ticks. Roughly **one occurrence per
11.5 sessions**. At that rate 100 sessions yields ~9 trades — still unmeasurable.

---

## 6. What survives as genuine knowledge

**The random-entry control is the one real positive.** Random longs over the same
sessions lost $540 on average and were profitable only 30.8% of the time, in a sample
where ES rose +130 ticks — **p = 0.018, rejected**. Whatever the detector finds, it is
not simply long exposure to a rising market. (Computed on the 5-session sample; treat
accordingly.)

The time-shift null was **not** rejected (p = 0.074): entries shifted ±2–30 minutes
were profitable 79.2% of the time. The signal locates a favourable *regime*, not a
favourable *moment*.

The loosening sweep rejected the regime-harvest hypothesis cleanly: ES quality
degrades monotonically as structure count rises (1.956 → 0.947 → 0.290 → −0.157 bps).

---

## 7. Retained assets

- **Engine + harness** (`python/vr/`): FSM shared by an offline pandas detector and
  the Nautilus strategy, with `vr_agreement.py` asserting they find identical
  structures — this is what caught the `str(AggressorSide.SELLER) == '2'` bug.
- **Randomisation controls** (`vr_random_control.py`, `vr_timeshift_control.py`) —
  directly reusable on any future track.
- **Survivorship fix** (`flatten_open`): open positions at session end are marked and
  counted. Without it, closed-trade counts fell 5→2 as the trail widened and the
  apparent PF optimum sat exactly where the most trades were being dropped.
- **Crossed-book exclusion criterion** (>5% of L3 updates with ask ≤ bid), applied
  objectively before P&L was known.
- **Perf**: removing an unused `book.bids()` materialisation cut 1986s → 58s/session.

---

## 8. Falsifier ledger

| Falsifier | Result |
|---|---|
| F1 rarity: <100 structures over exploratory → too rare | **24 over 23 sessions.** FAIL |
| F2 direction: mean forward move ≤ 0 | +3.188 tk @30s. **Pass** |
| Stage-6 rule (declared pre-run): <40% break OR forward move from break ≤ 0 → close | Break 50% (pass); forward move ≤0 at 120s and 300s. **CLOSE** |

---

## 9. Closure

Three independent reasons, any one sufficient:

1. **The doc's trade occurs twice in 23 sessions.** Not "it loses" — it cannot be
   measured, on this data or on data we could afford.
2. **The break entry is worse than the retrace entry**, which was itself breakeven
   (t = 0.04).
3. **71% of detected structures lack the doc's geometry.** Fixing that would tighten
   detection and cut the population further.

**The 9 OOS sessions (`2026-07-03`, `07-14`…`07-23`) remain sealed and unread.** There
is no frozen candidate worth spending them on.

*Not financial advice. Research only.*
