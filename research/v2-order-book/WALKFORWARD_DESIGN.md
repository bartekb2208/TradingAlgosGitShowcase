# V2 Walk-Forward: target-width tuning — design

**Date:** 2026-08-03 · **Status:** approved, not yet implemented
**Engine:** `v2_core.V2_ENGINE_VERSION` 1.0.2 (frozen — this work does not modify it)

> NOT FINANCIAL ADVICE. Research only.

## Problem

The V2 `conjunction` strategy exits at a fixed **+1 tick** target. Measured on the
6 exploratory sessions (135 round trips), that target captures far less than what
is available: favourable excursion on the mid averages **+1.78 ticks** within 2 s
(median +1.50, p90 +3.50), and target-exit trades show MFE **+2.79** while banking
+1.00. Meanwhile commission is **0.40 ticks/trade**, so a 1-tick maximum win gives
40 % of its best case to costs.

**Goal:** choose a target width that captures more of the available reversion,
using a disciplined procedure rather than a single in-sample fit — and do it
*without* spending the sealed out-of-sample set.

## Scope

**In:** target width tuning for `conjunction`, on the 6 already-burned exploratory
sessions, via leave-one-session-out cross-validation.

**Out:** the `hawkes` variant (does not fire on ES — see `ENGINE_FROZEN.md` §5);
risk-management changes (separate deliverable); V-Reversal (separate algorithm
track); anything touching the 27-session OOS.

## Constraints

| constraint | value | source |
|---|---|---|
| Data | 6 exploratory sessions, 2026-07-06 … 07-13 | already burned setting original thresholds |
| Sealed | ~27 OOS sessions, untouched | `research/v-reversal/PREREG.md` |
| Tunable | target width only | user decision |
| Time stop | **fixed at 5 s** | user decision — bounded hold time |
| Grid | width ∈ {1, 2, 3, 4} ticks | spans the measured reachable range |
| Objective | **maximise net profit** | user decision |
| Engine | frozen at 1.0.2, unmodified | `ENGINE_FROZEN.md` |

## What this can and cannot deliver

**Cannot:** a performance estimate. Six sessions / ~135 trades split into folds of
~25 trades is noise, and no fold arithmetic changes that.

**Can:** answer whether the optimal target width is **stable**. If the best width
flips between folds, the parameter is not real and no out-of-sample test will
rescue it. The deliverable is therefore *one frozen parameter set plus evidence
about whether it deserves to be frozen at all*.

This framing is the point of the exercise: it is a gate in front of the OOS, not a
substitute for it.

## Architecture

New module `python/v2/walkforward.py` (designed here, never built). It imports `V2StrategyBase` and drives the
frozen engine; it does not modify it.

**Key efficiency property:** each `(width, session)` pair is run **exactly once**
and its per-trade records cached. Folds are partitions of cached results, not
re-runs. 4 widths × 6 sessions = 24 engine runs (~20 min); every fold structure
afterwards is free arithmetic.

```
run_grid(sessions, widths, timeout_ns, base_cfg)
    -> for each session: load MBO once, reuse engine via reset()+clear_strategies()
    -> writes research/v2-order-book/artifacts/WF_conj_width_expl6/raw/w{W}_s{YYYY-MM-DD}.json

loso_folds(cache, sessions)          -> 6 folds, each (tune = 5 sessions, test = 1)
select(trades_by_width, min_trades)  -> width maximising net profit
report(folds)                        -> stability table, frozen_params.json
```

Engine reuse follows `DESIGN.md` §3.7: `reset()` + `clear_strategies()` +
`add_strategy()`. `V2StrategyBase.on_reset()` already clears all derived state.

## Protocol

For each held-out session *h* ∈ the 6:

1. **Tune** — pool trades from the other 5 sessions. For each width, compute net
   profit. Select the width with maximum net profit, subject to a **minimum of 10
   trades** in the tune pool (repo convention, `crossval.py`).
2. **Test** — evaluate that selected width on session *h* alone.

Report per fold: selected width, tune net, held-out net, held-out trade count.
Report pooled: LOSO aggregate net, and a **width-stability table** (how many folds
elected each width).

Profit factor and win rate are reported but **never optimised** — same discipline
as `crossval.py`.

### Why LOSO rather than rolling walk-forward

Over 6 consecutive trading sessions there is no regime drift for forward-ordering
to protect against, so strict time-ordering costs half the folds (3 vs 6) and buys
nothing. LOSO uses every session as test exactly once. This is a deliberate
deviation from the literal term "walk-forward" and is recorded here so it is not
mistaken for an oversight.

## Falsifiers — declared before the run

Either condition means **freeze nothing and do not spend the OOS**:

1. **Cost floor.** Commission is 0.40 ticks/trade. If no width achieves gross
   **> +0.40 ticks/trade** on the tune folds, there is no edge to freeze.
   Selecting the least-bad option is how a fake parameter is manufactured.
2. **Instability.** Let the *modal width* be the one elected by the most folds.
   If the modal width wins **fewer than 4 of the 6 folds**, the parameter is not
   real and nothing is frozen. (Equivalently: a stable parameter must be selected
   by a clear majority, not a plurality.) Six folds electing three different
   widths is a null result, not a tie to break.

A null result here is a legitimate and useful outcome: it closes the width question
cheaply and leaves the 27 OOS sessions intact.

## Outputs

```
research/v2-order-book/artifacts/WF_conj_width_expl6/
├── raw/w{1,2,3,4}_s{session}.json   cached per-trade records
├── folds.csv                        per-fold: selected width, tune net, test net, n
├── report.txt                       stability table + falsifier verdict
└── frozen_params.json               engine_version + chosen width (only if falsifiers pass)
```

`frozen_params.json` is the input to the eventual V2 strategy pre-registration.

## Testing

Embedded `--selftest` (house style, no pytest):

- **LOSO partition correctness** — each session held out exactly once; a tune set
  never contains its own held-out session.
- **Objective selection** — on synthetic trade records with a known winner.
- **Min-trades guard** — a width below the threshold is not selectable.
- **Falsifier 1** — fires when all widths are below the cost floor.
- **Falsifier 2** — fires when fold selections disagree at the stated rate.
- **Cache round-trip** — records written and re-read are identical.

## Supporting evidence in flight

A separate sweep (width {1,2,3} @ 2 s, plus width 2 @ {2, 5, 10} s) is running on
the same 6 sessions. It tests whether **5 s is the right timeout to fix**. If it
shows 5 s is clearly wrong, revise this design before running the grid.

## Open items this design does NOT resolve

1. **No V2 strategy pre-registration exists.** `research/v-reversal/PREREG.md` covers the
   signal only and sets no P&L criteria. One must be written before the OOS read.
2. **Adverse selection is untreated.** The measured failure mode is that entry
   conditions have evaporated by fill (OBI median **0.000** at fill against an
   entry requirement of ≥ p90; 96.3 % of fills at spread > 1 tick). Target width
   does not address this. It is the strongest candidate for the next work item and
   is covered in the risk-management options menu.
3. **Gross edge is currently negative** (−0.067 ticks/trade). Width tuning may or
   may not lift it above the cost floor; falsifier 1 exists precisely because it
   may not.
