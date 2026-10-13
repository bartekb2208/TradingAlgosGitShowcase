# V2 Nautilus engine — FROZEN SPECIFICATION

**Engine version `1.0.1`** (`v2_core.V2_ENGINE_VERSION`). Frozen 2026-08-03.

> **1.0.1** — reporting correction only, no behaviour change. Round trips are now
> enumerated from captured `PositionClosed` events instead of
> `engine.cache.positions_closed()`. Under `OmsType.NETTING` Nautilus reuses one
> `PositionId` per instrument, so the cache reported **1** closed position while
> **28** round trips had actually occurred — every PnL aggregate was silently the
> last trade only. Any artifact stamped `1.0.0` is wrong and was deleted.

> NOT FINANCIAL ADVICE. Research only.

This document is the contract for the V2 execution layer. Every backtest artifact
stamps `engine_version` and the full `V2Config`; **results produced under different
engine versions are not comparable and must not be pooled.**

Bump the version on ANY change to signal evaluation, execution, exit or risk
semantics. Adding a diagnostic counter or a report field is not a semantic change.

---

## 1. Which hypothesis this implements

Two separate source documents exist and they are **not the same setup**:

| Source | Nature | Where it lives |
|---|---|---|
| my own research notes (not published) — "Mean Reversion system 2: Order book Exhaustion" | **Mechanical.** Explicit numeric entry/exit rules (divergence in bps, OBI, λ(t) exhaustion, convergence exit, 2 s time stop). | **This engine.** |
| Cody Bengtson's *V-Reversal Setup* (2023; third-party document, not redistributed here) | **Discretionary.** Consolidation zones, blowoff volume, trapped traders, passive/aggressive entry choices, structural stops at cons-low / V-low. Judgment at every step. | `python/vr/v_reversal_prototype.py` (bar-based screen) |

V2 implements the **research-notes** setup. The V-Reversal doc is the *thesis
ancestor* of the `conjunction` entry variant but its entry/stop/target framework is
deliberately NOT implemented here — it does not reduce to fixed thresholds, and
pretending it does would be fiction.

## 2. Entry variants (same execution layer, different signals — A/B, never merged)

| Variant | Entry condition | Provenance |
|---|---|---|
| `conjunction` | rolling aggressive-sell volume ≥ p99 **AND** book imbalance ≥ p90 | `research/v-reversal/PREREG.md` (signal validated; strategy not) |
| `hawkes` | divergence ≥ 3 bps **AND** OBI > 0.30 **AND** λ(t) ≤ 50 % of its 100 ms peak | research-notes doc, Entry Rules: LONG |

Each needs its **own** pre-registration before any out-of-sample read. The existing
VREVERSAL prereg covers the *signal only* — it explicitly states "No entry rule,
stop, target, or cost model is involved" — so it sets no P&L criteria for either.

## 3. Frozen engine decisions

### 3.1 Instrument — `data_pipeline.build_instrument`
Real `FuturesContract` built from `CONTRACT_SPECS`, **not** `TestInstrumentProvider`.

| | ES | NQ |
|---|---|---|
| tick | 0.25 | 0.25 |
| multiplier | **50** | **20** |
| tick value | $12.50 | $5.00 |

> `TestInstrumentProvider.es_future()` hardcodes `multiplier=1`. It is a test
> fixture. Using it divides every PnL and margin figure by 50. Guarded by selftest.

### 3.2 Costs
`PerContractFeeModel`, `commission_per_side = $2.50` (CME exchange+clearing ≈ $1.33
plus FCM commission). **$5.00 round turn = 0.40 ES ticks.** Charged on both legs.

Previously the venue had no fee model at all and the instrument carried
`maker_fee = taker_fee = 0`, so all v0.1 numbers were gross.

### 3.3 Exit — `convergence` (default)
The profit target is the **micro-price observed at fill**, snapped up to the tick
grid, floored at entry + 1 tick.

Spec: *"Exit via passive limit order the millisecond last traded price converges
back to the micro price."* The dislocation is the edge, so the target must scale
with it. The v0.1 engine used a flat `entry + 1 tick`, which truncates exactly the
right tail that produces the prereg's measured +1.60-tick mean while leaving the
loss side uncapped (timeout exits go out at market and cross the spread).

`exit_mode = "fixed_ticks"` is retained solely to reproduce v0.1 numbers.

### 3.4 Time stop
2 s (spec). On expiry: cancel resting orders, close at market.

### 3.5 Session gate — RTH only, 09:30–16:00 ET
Resolved through `America/New_York` via tzdb, so US DST is handled (the ES tape
moves between 13:30 and 14:30 UTC across the year). **Only the signal is gated —
the book is never gated**, because it must replay the whole session to warm up or
orders resting from before the window would be invisible.

### 3.6 Event independence — 180 s cooldown
The prereg's +1.60-tick result is measured on events **≥ 180 s apart**; without the
same spacing the strategy trades a population the signal was never validated on.
Anchored at **signal time**, not fill time.

### 3.7 Risk
- 1 contract, 1 position (spec: inventory limit). Position *sizing* is deliberately
  absent — this measures whether a per-contract edge exists net of real costs.
- Spread-blowout abort at 3× the rolling EWMA spread (spec).
- **VPIN override: NOT IMPLEMENTED.** The spec names it as the primary
  adverse-selection defence. This is the largest known gap in the engine.

### 3.8 Fills
`BookType.L3_MBO`, `queue_position=True`, `trade_execution=True`. Entries are
`post_only` limits at the best bid — they join the back of the queue and only fill
if the queue ahead is consumed. Measured fill rate: **46.8 %**.

### 3.9 Round-trip accounting — read the event stream, never the cache
`V2StrategyBase.on_position_closed` appends every round trip to
`self.closed_positions`, and the runner aggregates **that** list.

> **Do not use `engine.cache.positions_closed()`.** Under `OmsType.NETTING`
> Nautilus reuses a single `PositionId` per instrument, so the cache holds one
> closed-position record no matter how many open→flat cycles occurred. Measured:
> 28 `PositionClosed` events, cache reported 1. Every aggregate built from the
> cache silently collapses to the last trade — it does not error, it under-reports.

---

## 4. Spec conformance

| Research-notes rule | Engine | Status |
|---|---|---|
| Divergence ≥ 3 **basis points** below micro-price | `divergence_bps = 3.0` | ✅ |
| OBI > 0.30 | `obi_threshold = 0.30` | ✅ |
| λ(t) ≤ 50 % of peak, 100 ms window | `exhaustion_frac = 0.5`, `peak_window_ns = 100 ms` | ✅ |
| Exit on convergence to micro-price | `exit_mode = "convergence"` | ✅ |
| 2-second time stop | `exit_timeout_ns = 2 s` | ✅ |
| Spread blowout abort 3× | `spread_abort_mult = 3.0` | ✅ |
| Inventory limit | `max_positions = 1` | ✅ |
| Passive limit at best bid | `post_only` limit | ✅ |
| Marketable entry when divergence covers costs | — | ❌ **not implemented** |
| VPIN override | — | ❌ **deferred** |

**Unit correction:** the doc says *3 basis points*, i.e. 0.03 % **relative** to
price — on ES @ ~7561 that is 2.27 points ≈ **9.1 ticks**. An earlier reading of
"3 points" (= 12 ticks) was wrong, and the v0.1 default of 2.0 *absolute ticks* was
~4.5× more permissive than the spec. bps is also the correct unit: it stays
comparable across price levels and between ES and NQ.

### Judgment calls (not in the spec — attackable, and the ones to calibrate)
`hist = 4000` · `lam_tau_ns = 50 ms` · `min_peak = 3.0` · `entry_ttl_ns = 300 ms` ·
`commission_per_side = $2.50` · `min_target_ticks = 1`

---

## 5. Known-open items — the engine is NOT ready for the OOS read

Measured on exploratory session **2026-07-06** (in the already-burned 07-06..07-13
window — machinery calibration, not an OOS read). n is small; none of this is a
verdict.

1. **The `hawkes` variant does not fire on ES at the spec threshold (BLOCKER).**
   3 bps = 9.1 ticks on ES. Measured over 266,166 trades that session:

   | divergence | occurrences | share of trades |
   |---|---|---|
   | ≥ 1 bps (3.0 ticks) | 1,228 | 0.46 % |
   | ≥ 2 bps (6.0 ticks) | 256 | 0.096 % |
   | **≥ 3 bps (9.1 ticks)** | **32** | **0.012 %** |

   Session maximum divergence was 3.62 bps. The ES spread is **1 tick 90 % of the
   time** (max 3 ticks), and micro-price is bounded inside the spread by
   construction — so a 9-tick gap between last trade and micro-price is a tail
   event, and requiring it *simultaneously* with OBI > 0.30 and λ-exhaustion gave
   **0 signals in a full session**.

   The spec's 3 bps is almost certainly calibrated for a **crypto** venue: the
   reference implementation it came with subscribes to an exchange WebSocket
   `level2` feed with Coinbase/Binance semantics, where relative spreads are
   orders of magnitude wider and 3 bps vacuums are routine. On a 1-tick-spread
   index future it is not a reachable threshold.

   **1a. Recalibration on ES contradicts the direction of the hypothesis.**
   Replaying all three entry conditions offline across the 6 exploratory sessions
   (1.9 M trades), forward micro-price move over the 2 s time-stop horizon:

   | divergence | indep. events (180 s) | mean fwd 2 s | % up |
   |---|---|---|---|
   | 0.25 bps | 404 | −0.77 ticks | 35.1 % |
   | 0.50 bps | 140 | −2.29 | 17.1 % |
   | 1.00 bps | 21 | −3.08 | 23.8 % |
   | 2.00 bps | 2 | −10.60 | 0 % |

   Monotone, and **negative everywhere** — the dislocation predicts *continuation
   down*, not reversion up. The setup's premise is inverted on ES.

   **1b. A lag control removes ~70 % of that effect but not all of it.** Because
   divergence is a gap between the trade tape and a book sampled at-or-before the
   trade, much of the apparent prediction is the book mechanically catching up to
   a sweep that had already printed. Re-sampling the book 200 ms after the trade
   (outcome anchored post-sweep, baseline = all 1.9 M trades at −0.05 ticks /
   49.4 % up):

   | book lag | 2 bps: mean fwd | % up |
   |---|---|---|
   | at trade | −6.12 | 43.4 % |
   | +50 ms | −4.18 | 46.4 % |
   | **+200 ms** | **−1.87** | **38.4 %** |

   The magnitude shrinks 3× but the **hit rate stays well below the 49.4 %
   baseline and declines monotonically with threshold** — so a real directional
   signal survives, pointing down. Always report the lag-controlled number.

   **Status per the decision taken:** recalibrate for ES (this section) **and**
   retain 3 bps as the spec value for a future crypto test — the venue the
   threshold was written for. Crypto L2 data is not in this repo; that path is
   parked pending data. Any ES threshold chosen from the table above is
   exploratory-derived and **must be pre-registered before the OOS read**.

2. **The `conjunction` variant is destroyed by adverse selection at fill.**
   6 exploratory sessions, 1.9 M trades: 274 signals → 135 round trips (49.3 %
   fill), **64.4 % win rate, PF 0.453, net −$787.50**.

   Per-trade diagnosis — this is not a marginal edge, it is a cost pump:

   | | value |
   |---|---|
   | avg win | +$7.50 (gross +$12.50 = exactly 1 tick) |
   | avg loss | −$30.00 (gross −$25.00) |
   | payoff ratio | **0.25** |
   | breakeven win rate needed | **80.0 %** (actual 64.4 %) |
   | **MFE** (max favourable excursion) | mean **+0.01 ticks**, median 0.00, p90 0.00 |
   | MAE | mean −1.93 ticks (timeout losers −2.79) |
   | **gross PnL over 135 trades** | **−$112.50** (≈ −0.07 ticks/trade) |
   | commission paid | −$675.00 |

   The setup conditions are true at *signal* time and gone by *fill* time:
   **OBI at fill averages +0.02** (entry requires ≥ p90) and **spread at fill
   averages 2.06 ticks** against a session-typical 1 tick. The passive bid fills
   precisely when it is being consumed — textbook adverse selection.

   **There is no inverted edge to harvest.** Gross is ≈ 0, not strongly negative,
   so flipping direction gives ≈ 0 gross and pays the same $675 commission
   (−$562.50). A low PF is only informative when gross is large and signed; here
   the signal has no directional content *at the point of fill* in either
   direction. MFE ≈ 0 is the tell: these trades never go in favour at all.

   Also confirms the convergence exit is **degenerate for this variant** —
   `target_ticks` is 1.00 at mean, median and p90, because entry requires no
   divergence (`entry_div_ticks` mean +0.028, median 0.000).

3. **No V2 strategy pre-registration exists** (see §2).
4. **VPIN not implemented** (§3.7).
5. **Conjunction percentile population differs from the prereg's.** The prereg
   computed p99/p90 over *1-second buckets*; the engine computes them over a
   per-trade-tick rolling window. Related but not identical populations.
6. **Python 3.14.6 vs Nautilus's tested 3.11–3.13.** First suspect on odd behaviour.

**Not a blocker (corrected):** entry fill rate is **46.8 %** (22 fills / 47
signals), giving ~22 round trips per session ≈ 590 over 27 OOS sessions — comfortably
past the 100-trade bar. An earlier "2 % fill rate" reading was an artifact of the
`positions_closed()` undercount fixed in 1.0.1.

## 6. Reproducing a result

```bash
V=.venv/bin/python
cd python/v2
$V data_pipeline.py --selftest && $V v2_core.py --selftest
$V run_backtest.py --strategy conjunction --sessions 2026-07-06 2026-07-07 --tag MY_RUN
# -> research/v2-order-book/artifacts/MY_RUN/{config.json,report.txt,trades.csv,per_session.csv}
```

`config.json` carries the engine version and every parameter. If it does not match
this document's version, this document does not describe that run.

## 7. Session data notes

- ES trade tapes cover **09:30–15:59 ET** (RTH) — 34 sessions, 2026-06-08 → 07-23.
- **2026-06-19 is a Juneteenth half-session**: 20,715 trades and a 13:00 ET close,
  versus 260k–775k on a normal day. The Phase 2/3 "engine works" results were run
  on this session — the least representative day in the sample. Do not use it for
  calibration.
- L3 MBO covers 68 sessions (2026-04-06 → 05-06 NQ, 2026-06-07 → 07-23 ES); the L2
  trade tape exists for 34. **A session without an L2 tape cannot signal at all**
  (the MBO loader is book-only), so the runner now fails loudly instead of
  silently running with zero trades.
