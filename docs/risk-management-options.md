# Risk-management options — menu for the MR/V-Reversal algorithm family

**Compiled 2026-08-03.** A menu, not a recommendation set. Applies across MR V1
(daily), V2 (order-book, seconds) and the forthcoming V-Reversal track.

> NOT FINANCIAL ADVICE. Research only.

---

## 0. The thing to get right first

**Your holding period changes what "fat tail" means, and V1's answers do not port
to V2.**

| | MR V1 | MR V2 |
|---|---|---|
| hold | 5–7 days | 2–10 **seconds** |
| tail event | gap, crash, structural repricing | flash move, liquidity vacuum, halt |
| defence in V1 framework | 2.5×ATR hard stop, 1 % risk, ≤5 positions, ATR-inverse sizing | — none of these transfer cleanly |
| current V2 defence | — | 2 s time stop, 1 contract, 3× spread abort |

Mandelbrot's point is that returns follow power laws, so six-sigma moves happen
often enough to matter. Taleb's point is narrower and sharper: **optimise against
ruin, not against variance.** Both apply at every timescale — but the *mechanism*
of ruin differs, so the control must differ.

**The specific V2 exposure worth naming:** the 2-second time stop exits **at
market, with no price protection**. In precisely the fat-tail scenario Mandelbrot
describes — a liquidity vacuum — a market exit is the worst available action.
Measured MAE reached **−47 ticks** at the 10 s horizon on the exploratory sample.
On one contract that is $587; the exposure is currently small *only* because size
is 1, which means **any sizing framework must price this in before size goes up.**

---

## 1. Per-trade tail risk

| # | Option | Fit | Cost / caveat |
|---|---|---|---|
| 1.1 | **Hard stop in ticks** | V2 has none — only a time stop | Bounds the tail directly. On exploratory data a −2-tick stop would have hit 36/48 losers; it reshuffles *when* you lose, it does not create edge. |
| 1.2 | **Time stop** (in place, 2 s) | V2 | Bounds *duration*, not loss. Exits at market — see §0. |
| 1.3 | **Protective limit instead of market exit** | V2 | Converts the timeout from "take any price" to "take this price or keep the position". Removes the flash-move exit, adds the risk of being stuck. |
| 1.4 | **Fixed-fractional sizing off the stop** (V1's method) | V1 ✓, V2 needs 1.1 first | 1 % risk / ATR-stop distance. Simultaneously satisfies "≤1 % per trade" and "scale inversely to volatility" with one mechanism. Requires a defined stop distance, which V2 lacks. |
| 1.5 | **Fractional Kelly** | Neither, yet | Needs a *validated* edge and payoff distribution. V2's gross edge is currently −0.067 ticks/trade; Kelly on an unvalidated edge sizes into ruin. Park until an edge exists. |
| 1.6 | **Hard notional cap** | Both | Crude, effective, already used in the MT5 EA (10×-equity cap). Cheap insurance against a sizing bug. |

## 2. Adverse-selection risk — *the measured V2 failure mode*

This is where V2's actual losses come from, and it is the least-defended category.
Evidence: entry demands OBI ≥ p90, but **OBI at fill has median 0.000**; **96.3 %
of fills occur at spread > 1 tick** against a session-typical 1 tick. You are
filled precisely when the setup has already broken.

| # | Option | Fit | Cost / caveat |
|---|---|---|---|
| 2.1 | **VPIN override** | V2 — *the spec's own answer, unimplemented* | Volume-synchronised probability of informed trading; disable entries above a threshold. This is the single largest spec-to-engine gap and it targets the measured failure mode directly. Non-trivial to implement and calibrate. |
| 2.2 | **Cancel-on-condition-decay** | V2 | Between signal and fill, re-check OBI; cancel if it collapses. Cheapest thing that attacks the OBI-median-0.000 finding. Reduces fill count — which is the point. |
| 2.3 | **Fill-quality gate** | V2 | Refuse entry when the prevailing spread exceeds the session norm. Directly targets the 96.3 % finding. Risk: may eliminate nearly all fills, since wide spread may be *why* you get filled. |
| 2.4 | **Spread-blowout abort** (in place, 3×) | V2 | Already present, but the threshold is far above the observed fill regime (2.06 ticks vs a 3× ≈ 3-tick trigger) — so it almost never binds. Tightening it is nearly free. |
| 2.5 | **Queue-position awareness** | V2 | Only enter when estimated queue position is favourable. Realistic in backtest (`queue_position=True` models it); demanding in live. |

## 3. Inventory and exposure

| # | Option | Fit | Cost / caveat |
|---|---|---|---|
| 3.1 | **Max concurrent positions** | V1 (5) ✓, V2 (1) ✓ | Present in both. |
| 3.2 | **Net delta cap** | V2, when >1 contract | The spec names it. Trivially satisfied at size 1; needed the moment size scales. |
| 3.3 | **Max trades per session** | V2 | Bounds commission bleed — relevant when cost is 85.7 % of the net loss. Blunt: caps good and bad trades alike. |
| 3.4 | **Daily loss limit** | Both | Halt for the session at a fixed drawdown. Standard prop-firm requirement. Beware the OrbFirstMoveBot post-mortem lesson (`research/orb-first-move/`): a 2 % daily halt *preempted* the strategy's own governing rule and had to be widened to a 5 % backstop. Set it as a backstop, not as a strategy rule. |
| 3.5 | **Consecutive-loss halt** | Both | Cheap circuit breaker. Can trigger on noise at small n. |

## 4. Regime and structural

| # | Option | Fit | Cost / caveat |
|---|---|---|---|
| 4.1 | **Volatility-scaled sizing** | Both | Mandelbrot's "scale inversely to volatility". V1 gets it free via the ATR stop; V2 would need a realised-vol estimator. |
| 4.2 | **Session filter** (RTH, in place) | V2 ✓ | Present and correct. |
| 4.3 | **Event blackout** | Both | Scheduled releases. The cTrader bots did this via a calendar feed; CME data carries scheduled events. Meaningful for a seconds-scale strategy — releases are exactly when vacuums open. |
| 4.4 | **Halt / limit-up-down detection** | V2 | Rare, catastrophic, cheap to guard. |
| 4.5 | **Anomalous-session exclusion** | Both | Half-days and holidays. 2026-06-19 (Juneteenth) has 5 % of normal volume and already contaminated the Phase 2/3 "engine works" claim. |

## 5. Portfolio / meta

| # | Option | Fit | Cost / caveat |
|---|---|---|---|
| 5.1 | **Rolling-PF kill switch** | Both | Disable a strategy whose trailing PF drops below a floor. The systematic version of "stop trading a broken edge". Needs enough trades for the rolling window to mean anything. |
| 5.2 | **Correlated-strategy cap** | Family | If conjunction, hawkes and V-Reversal run together they share a failure mode (all long-only reversion into selling). Cap aggregate exposure, not just per-strategy. |
| 5.3 | **Aggregate risk across a scale-in ladder** | V-Reversal | The `TpoValueFadeBot` pattern: size each rung so the *full ladder* at the shared stop equals the intended risk. Relevant if V-Reversal implements the doc's scale-in entries. |

---

## 6. If you want a starting shortlist

Ordered by evidence-per-unit-effort **for V2 specifically**:

1. **2.2 cancel-on-condition-decay** — cheapest attack on the measured failure mode.
2. **2.4 tighten the spread abort** — nearly free; the current threshold never binds.
3. **1.1 hard tick stop** — bounds the tail the time stop leaves open.
4. **1.3 protective limit exit** — removes the market-exit-into-a-vacuum exposure.
5. **2.1 VPIN** — the spec's own answer and the biggest gap, but the most work.

**Deliberately not on the list yet:** any sizing framework (1.4, 1.5, 4.1).
V2's gross edge is currently **−0.067 ticks/trade**. Sizing multiplies an edge; it
cannot create one, and applying it to a negative edge accelerates ruin. Sizing is
the right conversation *after* an edge clears the 0.40 ticks/trade cost floor.

## 7. A caution about testing these

Every option here is a parameter, and each one tested on the exploratory sessions
consumes degrees of freedom. The 6 exploratory sessions carry ~135 trades; a menu
of 20 controls evaluated against them will find something that "works" by chance.

Pick from mechanism and evidence — as items 2.2 and 2.4 are picked from the
OBI-at-fill and spread-at-fill measurements — rather than by sweeping the menu.
Anything selected still needs pre-registration before the OOS read.
