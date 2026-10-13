# V2 order-book exhaustion — STRATEGY pre-registration

**Written 2026-08-03, BEFORE any out-of-sample data has been read.** Trial #1 for
the V2 strategy family. Nothing below may change after seeing OOS results; a
changed rule, grid or partition is a NEW pre-registration with the trial count
incremented.

> NOT FINANCIAL ADVICE. Research only.

## What this pre-registers, and why a new one is needed

`research/v-reversal/PREREG.md` covers the **signal** and says so explicitly:
*"This measures whether a signal exists, not whether a strategy is tradeable. No
entry rule, stop, target, or cost model is involved."* It therefore sets **no P&L
criteria** and cannot govern a strategy test. This document does.

**Engine:** `v2_core.V2_ENGINE_VERSION` **1.1.0**, frozen per
`research/v2-order-book/ENGINE_FROZEN.md`. Results from any other engine version are not
comparable and must not be pooled with these.

## Prior exploratory findings (already burned — 2026-07-06 … 07-13, 6 sessions)

These sessions are spent. They are reported here so the OOS test cannot be
mistaken for a first look, and so the hypothesis below is visibly *not* one the
exploratory data already rejected.

| finding | evidence |
|---|---|
| Baseline conjunction has **no edge** | 135 round trips, 64.4 % win, gross **−0.067 ticks/trade**, net −$787.50, PF 0.453 |
| Commission dominates the net loss | $675 of $787.50 = 85.7 %; but gross is negative, so zero commission still loses |
| **Target widening is closed** | gross ticks/trade: +1 tick −0.067 · +2 −0.163 · +3 −0.289 — monotonically worse |
| **Timeout lengthening is closed** | width 2: 2 s −0.163 · 5 s −0.319 · 10 s −0.452 — monotonically worse |
| `hawkes` variant is **untestable on ES** | 3 bps = 9.1 ticks; 0.012 % of trades reach it; 0 signals per session |
| Measured failure mode is **adverse selection** | OBI at fill median **0.000** vs entry requirement ≥ p90; **96.3 %** of fills at spread > 1 tick vs session-typical 1 tick |

## Hypothesis under test

The conjunction entry has no edge **as executed** because the conditions that
justify it have decayed by the time the passive order fills. If the strategy
refuses those degraded fills, the surviving trades should carry the edge the
signal study measured.

Two controls, chosen from the measured mechanism above rather than by sweeping a
menu (`docs/RISK_MANAGEMENT_OPTIONS.md` §2.2, §2.4):

- **2.2 `cancel_on_obi_decay`** — cancel a live, unfilled entry when current OBI
  falls below `obi_cancel_floor`.
- **2.4 `max_entry_spread_ticks`** — refuse entry when the prevailing spread
  exceeds an absolute cap in ticks.

## Frozen configuration

Everything is `V2Config` defaults at engine 1.1.0 **except** the two controls.
Their values are frozen from the exploratory A/B and recorded here before the OOS
read:

| parameter | value | status |
|---|---|---|
| `cancel_on_obi_decay` | *to be frozen from the exploratory A/B* | **must be filled in before the OOS run** |
| `obi_cancel_floor` | *to be frozen from the exploratory A/B* | **must be filled in before the OOS run** |
| `max_entry_spread_ticks` | *to be frozen from the exploratory A/B* | **must be filled in before the OOS run** |
| everything else | `V2Config()` defaults | frozen |

> **This document is NOT valid for an OOS read until the three cells above hold
> literal values.** They are set once, from exploratory data only, and never
> revisited. If the exploratory A/B does not clear the pre-condition below, no
> values are frozen and no OOS read happens.

### Pre-condition on the exploratory A/B
At least one control configuration must reach **gross > +0.40 ticks/trade** (the
commission bar) on the 6 exploratory sessions, with **≥ 30 round trips**. If none
does, the hypothesis is abandoned here and the OOS is not spent.

## Dataset and roles

- **ES 2026-06-08 … 07-23**, 34 sessions with an L2 trade tape.
- **EXPLORATORY (spent):** 2026-07-06, 07-07, 07-08, 07-09, 07-10, 07-13.
- **OUT-OF-SAMPLE (sealed, read exactly once):** the remaining **28** sessions.
- **EXCLUDED:** 2026-06-19 — Juneteenth half session, 20,715 trades against a
  260k–775k norm, 13:00 ET close. Excluded on structural grounds declared here,
  before any OOS read.
- NQ (2026-04-06 … 05-06) is **not** part of this test. It does not overlap the ES
  window, so instrument and regime are confounded.

## Success criteria — fixed in advance

All three required, computed on the 28 OOS sessions pooled, **net of commission**:

1. **≥ 100 round trips** (this project's representativeness bar; V1 died on it twice).
2. **Net profit factor > 1.10.**
3. **≥ 4 of the 6 two-week blocks net-positive.** Consistency over magnitude —
   the exploratory sample failed precisely by having its result carried by a
   minority of sessions.

Reported but never optimised: win rate, Sharpe, max drawdown, median hold time.

## Falsification

If any criterion fails, **the V2 conjunction strategy is not supported on ES** and
the family is closed, exactly as MR V1 was closed in `research/mr-v1-connors-rsi2/RESULTS.md`.

Do **not** re-tune the controls on the OOS data and re-test — that converts the
test into a search. A genuinely different hypothesis is a new pre-registration
with the trial count incremented.

## Explicitly not claimed

- Single instrument (ES), single contract, one calendar window of ~2 months. No
  claim about NQ, other regimes, or scaled size.
- No position-sizing framework. Size is fixed at 1 contract by design: this
  measures whether a per-contract edge exists net of real costs. Sizing multiplies
  an edge and cannot create one.
- Commission is modelled at $2.50/side; exchange fee tiers, FCM specifics and
  financing are not modelled.
- Fills rely on Nautilus L3 queue-position modelling. It is a model of queue
  priority, not a guarantee of real fills.
- **VPIN is not implemented.** The source spec names it as the primary
  adverse-selection defence, and adverse selection is the measured failure mode,
  so this test evaluates a deliberately weaker defence than the spec describes.
