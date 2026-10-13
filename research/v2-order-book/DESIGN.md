# V2 — Order-Book Exhaustion on NautilusTrader: engineering design

**Audience:** a software engineer who has not seen this repo. Assumes no trading
background beyond what is defined here. Companion to `ENGINE_FROZEN.md` (the
frozen parameter contract) — this document explains *how it is built and why*.

> NOT FINANCIAL ADVICE. Research only.

---

## 0. Vocabulary (read this first)

| term | meaning |
|---|---|
| **tick** | smallest price increment. ES = 0.25 index points. |
| **multiplier** | dollars per 1.0 index point. ES = $50, so **1 tick = $12.50**. |
| **bid / ask** | best resting buy price / best resting sell price. |
| **spread** | ask − bid. On ES front month this is **1 tick, 90 % of the time**. |
| **maker / taker** | maker rests a limit order and waits; taker crosses the spread and pays it. |
| **MBO (L3)** | *Market By Order.* Every individual order add/cancel/modify/trade. What we have. |
| **MBP / L2** | *Market By Price.* Aggregated size per price level. Derived from MBO. |
| **micro-price** | size-weighted mid: `(bid_px·ask_sz + ask_px·bid_sz) / (bid_sz + ask_sz)`. Leans toward the side with *less* size. **Always lies inside the spread.** |
| **OBI** | order-book imbalance `(bid_sz − ask_sz)/(bid_sz + ask_sz)` ∈ [−1, 1]. |
| **λ(t)** | aggressive-sell arrival intensity — an exponentially decaying counter. |
| **MFE / MAE** | max favourable / adverse excursion: furthest a trade went for / against you. |
| **RTH** | Regular Trading Hours, 09:30–16:00 US Eastern. |
| **round trip** | one open + close pair. The unit we count. |

**Cost anchor, memorise it:** commission is **$2.50/side = $5.00 round turn =
0.40 ticks**. Any strategy whose maximum win is 1 tick ($12.50) is giving 40 % of
its best case to costs.

---

## 1. What the strategy claims

From my own research notes (not published), *"Mean Reversion system
2: Order book Exhaustion"*:

> A large algorithmic order arrives as a tightly clustered burst of market sells.
> It consumes resting limit orders faster than market makers can replenish them.
> A **liquidity vacuum** opens: the *traded* price gaps down while the *micro-price*
> (which reflects resting book state) does not move. Once the burst's intensity
> decays, traded price snaps back to the micro-price.

So: **trade price dislocates from book price; bet on the gap closing.**

Entry (LONG), all three required:
1. **Divergence** — last trade ≥ 3 basis points below micro-price.
2. **Imbalance** — OBI > 0.30 (a wall of bids ready to absorb).
3. **Exhaustion** — λ(t) has fallen ≥ 50 % from its peak within 100 ms. *This is
   the falling-knife filter and the condition the whole setup turns on.*

Exit: when traded price converges back to micro-price; hard 2-second time stop.

### 1.1 A different document, do not merge them
Cody Bengtson's *V-Reversal Setup* (2023; third-party document, not redistributed here) is a **discretionary** footprint method —
consolidation zones, blowoff volume, trapped traders, a choice between narrow and
wide structural stops, targets set at "the origin of the thin move". It does not
reduce to fixed thresholds and is **not** what V2 implements. It lives in
`python/vr/v_reversal_prototype.py` as a bar-based screen. V2 implements the mechanical
research-notes setup only.

### 1.2 Two entry variants, one execution layer
| variant | entry | file |
|---|---|---|
| `conjunction` | rolling aggressive-sell volume ≥ p99 **AND** OBI ≥ p90 | `v2MeanReversion_conjunction.py` |
| `hawkes` | divergence ≥ N bps **AND** OBI > 0.30 **AND** λ-exhaustion | `v2MeanReversion_hawkes.py` |

They are **A/B alternatives, never merged**. Each is ~15 lines; everything else is
shared in `V2StrategyBase`.

---

## 2. Data: what we have and what it costs

```
data/
├── L1/                     daily & minute CSVs (V1 legacy, unused by V2)
├── L2/   578 MB            derived, per session:
│     ES_YYYY-MM-DD_trades.parquet    ts, price, size, aggressor(+1 buy/-1 sell)
│     ES_YYYY-MM-DD_depth10.parquet   ts, bid_px_00..09, bid_sz_00..09, ask_*
│     _manifest_ES.json               date -> front-month contract code
└── L3/   18 GB             raw Databento GLBX.MDP3 MBO
      GLBX-<batch>/glbx-mdp3-YYYYMMDD.mbo.dbn.zst    ~0.5 GB/session
```

**Coverage:** 68 sessions of L3 (ES 2026-06-07→07-23, NQ 2026-04-06→05-06); **34**
have the L2 trade tape. L2 tapes cover exactly RTH, 09:30–15:59 ET.

Three constraints that shape the whole pipeline:

1. **~0.5 GB/session compressed, ~9.4 M book deltas.** Process **one session at a
   time**. Never load the batch.
2. **Parent symbology.** Every contract month *and every calendar spread* shares
   one tape. You must filter to the front-month outright (e.g. `ESM6.GLBX`) or you
   are trading a blend of unrelated instruments.
3. **No DEFINITION file in the batch.** Nautilus cannot self-describe the
   instrument; you must construct it in code (§4.2).

**2026-06-19 is a Juneteenth half session** — 20 k trades and a 13:00 ET close vs
260 k–775 k normally. Excluded from calibration.

---

## 3. How to backtest MBO data with NautilusTrader

This is the part with the sharp edges. Every item below was established
empirically against **nautilus_trader 1.230.0**.

### 3.1 Loading DBN → Nautilus

```python
from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader

loader = DatabentoDataLoader()
deltas = loader.from_dbn_file(
    str(path),
    price_precision=2,        # REQUIRED — no DEFINITION file to infer it from
    as_legacy_cython=True,    # see 3.2
)
```

- **Do NOT pass `instrument_id`.** Nautilus maps every record via symbology; pass
  nothing and filter afterwards on `str(d.instrument_id) == "ESM6.GLBX"`.
- **The MBO loader is book-only.** `include_trades=True` raises. You get
  `OrderBookDelta` objects and no trades — see §3.3.
- **Do not `isinstance`-check against the Cython `OrderBookDelta`** when loading
  pyo3 objects; they will not match.

### 3.2 pyo3 vs Cython — the fork that wastes an afternoon

| you want | flag | why |
|---|---|---|
| write to a `ParquetDataCatalog` | `as_legacy_cython=False` (pyo3) | fast serialisation |
| feed `BacktestEngine.add_data()` | `as_legacy_cython=True` (Cython) | the manual engine path requires Cython objects |

Same file, different object type. Pick per destination.

### 3.3 Trades must come from the L2 tape

The strategy needs `TradeTick`s (that is what triggers signal evaluation), and the
MBO loader will not give them. Source them from the derived L2 parquet:

```python
# v2_core.load_trade_ticks
TradeTick(
    instrument_id=instrument.id,
    price=Price(float(price), 2),
    size=Quantity(int(size), 0),
    aggressor_side=AggressorSide.SELLER if aggr < 0 else AggressorSide.BUYER,
    trade_id=TradeId(str(i)),
    ts_event=t, ts_init=t,
)
```
Nautilus merges `add_data()` streams by timestamp, so book deltas and trade ticks
interleave correctly. **A session with no L2 tape produces zero signals** — the
runner raises rather than silently reporting a flat result.

### 3.4 Venue configuration

```python
engine.add_venue(
    venue=Venue("GLBX"),
    oms_type=OmsType.NETTING,
    account_type=AccountType.MARGIN,
    base_currency=USD,
    starting_balances=[Money(1_000_000, USD)],
    book_type=BookType.L3_MBO,   # full order-by-order book
    trade_execution=True,        # trades can fill our resting orders
    queue_position=True,         # model FIFO queue position — essential for a maker
    fee_model=PerContractFeeModel(Money(2.50, USD)),
)
```

`queue_position=True` is what makes a passive strategy honest: your limit joins the
**back** of the queue at its price level and fills only when everything ahead of it
is consumed. Without it, every resting order fills instantly and the backtest is
fiction.

**`fee_model` defaults to `None` and the instrument's `maker_fee`/`taker_fee` are
`0`.** If you do not pass a fee model your results are gross. For a strategy whose
whole edge is ~1 tick, that is the difference between a result and a fantasy.

### 3.5 Strategy interface

```python
def on_start(self):
    self.subscribe_order_book_deltas(self.iid, book_type=BookType.L3_MBO)
    self.subscribe_trade_ticks(self.iid)

def on_order_book_deltas(self, deltas):     # NOTE: PLURAL, batched
    book = self.cache.order_book(self.iid)
    bp, ap = book.best_bid_price(), book.best_ask_price()

def on_trade_tick(self, tick): ...
```

The handler is **`on_order_book_deltas`** (plural, batched), not the singular form.
Read the book through `self.cache.order_book(id)`.

### 3.6 Three traps that silently corrupt results

**(a) `cache.positions_closed()` under NETTING undercounts.**
Nautilus reuses one `PositionId` per instrument, so the cache holds a *single*
closed-position record however many open→flat cycles occurred. Measured: **28
`PositionClosed` events, cache reported 1.** Every aggregate built from the cache
collapses to the last trade. It does not error.

> **Rule: accumulate round trips in `on_position_closed`.** The event stream is the
> population; the cache is current state.

**(b) The instrument multiplier.**
`TestInstrumentProvider.es_future()` hardcodes `multiplier=1`. It is a test
fixture. Using it divides every PnL and margin figure by 50. Build a real
`FuturesContract` (§4.2); a selftest asserts `multiplier == 50`.

**(c) You cannot slice the book mid-session.**
Orders resting from before your window would be invisible, so the book must replay
the **whole session** to warm up. Gate the **signal**, never the data. `in_rth()`
does exactly this.

### 3.7 Repeated runs in one process

Nautilus initialises a Rust logger once per process — constructing a second
`BacktestEngine` panics. For parameter sweeps:

```python
engine.reset()              # clears state, RETAINS data/instruments/venues
engine.clear_strategies()
engine.add_strategy(NewStrategy(instrument, new_cfg))
engine.run()
```

Because strategies survive `reset()`, **`V2StrategyBase.on_reset()` must clear every
derived field** — percentile buffers, λ state, counters, captured positions. A field
left behind leaks one config's history into the next and looks like a real effect.

---

## 4. Code layout

```
python/v2/
├── data_pipeline.py    MBO file -> front-month filter -> instrument -> catalog
├── v2_core.py          metrics, config, V2StrategyBase (execution/exit/risk)
├── v2MeanReversion_conjunction.py    entry variant A (~15 lines)
├── v2MeanReversion_hawkes.py         entry variant B (~15 lines)
└── run_backtest.py     CLI: sessions -> engine -> artifacts
```

The engine freeze and this design document live in `research/v2-order-book/`
(`ENGINE_FROZEN.md`, `DESIGN.md`).

### 4.1 `data_pipeline.py`
- `find_mbo_file(session)` — glob `glbx-mdp3-YYYYMMDD.mbo.dbn.zst`.
- `front_month_contract(symbol, session, deltas)` — reads `_manifest_ES.json`;
  falls back to the most common outright ID (spreads contain `-`).
- `build_instrument(contract)` — see below.
- `build_catalog(...)` — writes pyo3 deltas to a `ParquetDataCatalog`.

### 4.2 Instrument construction
```python
CONTRACT_SPECS = {
    "ES": {"multiplier": 50, "tick": "0.25", "precision": 2, ...},
    "NQ": {"multiplier": 20, "tick": "0.25", "precision": 2, ...},
}
```
Contract code `ESM6` → `M` = June (CME month codes), `6` → 2026. Expiry = third
Friday of the contract month. Activation is set a year earlier so the session under
test is comfortably inside `[activation, expiration]`.

### 4.3 `v2_core.py`

Pure functions (unit-testable with no engine): `micro_price`, `book_imbalance`,
`rth_bounds_ns`, `RollingPercentile`.

`V2Config` is a frozen-by-convention dataclass. Every field is annotated `SPEC:`
(verbatim from the doc) or `JUDGMENT:` (our choice — the attackable ones). It
serialises into every artifact via `to_dict()`.

**`V2StrategyBase` lifecycle:**

```
on_order_book_deltas ──► refresh bid/ask/sizes
                        update spread EWMA, OBI percentile
                        update MFE/MAE if a position is open
                        _manage_orders()  ── entry TTL expiry
                                           └─ 2 s time stop -> close at market

on_trade_tick ────────► decay λ, bump on SELLER aggressor, track 100 ms peak
                        roll 1 s aggressive-sell volume, update p99
                        if _can_enter(ts) and entry_signal(): _enter(ts)

_can_enter  = warm (>1000 samples) AND flat AND no live entry/exit
              AND in_rth AND cooldown elapsed AND spread not blown out

on_order_filled(BUY) ─► arm 2 s deadline, snapshot entry context,
                        submit reduce_only SELL limit at exit_target()

on_position_closed ───► APPEND the round trip to self.closed_positions
```

**Exit target:**
```python
convergence : target = micro_price_at_fill, snapped up to the tick grid,
                       floored at entry + 1 tick
fixed_ticks : target = entry + fixed_target_ticks     # legacy v0.1 behaviour
```

**Event independence — the 180 s cooldown.** The signal's supporting evidence
(`research/v-reversal/PREREG.md`) is measured on events **≥ 180 s apart**; without
matching spacing the strategy trades a population the signal was never validated
on. Anchored at *signal* time.

### 4.4 `run_backtest.py`
```bash
V=.venv/bin/python
cd python/v2
$V data_pipeline.py --selftest && $V v2_core.py --selftest
$V run_backtest.py --strategy conjunction \
   --sessions 2026-07-06 2026-07-07 --tag MY_RUN
```
Writes `research/v2-order-book/artifacts/MY_RUN/{config.json, report.txt, trades.csv, per_session.csv}`.
`config.json` carries `engine_version` + every parameter.

### 4.5 Testing convention
No pytest. Each module has an embedded `--selftest` (house style). 23 checks in
`v2_core.py`, 11 in `data_pipeline.py`, covering DST-correct RTH bounds, bps↔tick
conversion, exit-target semantics in both modes, config guards, and the
`multiplier == 50` regression guard.

---

## 5. Versioning and the freeze

`V2_ENGINE_VERSION` is stamped into every artifact. **Results from different engine
versions must never be pooled.** Bump on any change to signal, execution, exit or
risk semantics; diagnostics and report fields do not count.

| version | change |
|---|---|
| 1.0.0 | first frozen engine |
| 1.0.1 | round trips read from the event stream, not the cache (§3.6a). Reporting only — but every 1.0.0 number was wrong. |
| 1.0.2 | MFE/MAE excursion diagnostics + entry-context capture. Diagnostic only. |

---

## 6. Measured behaviour (6 exploratory sessions, 1.9 M trades)

The sessions 2026-07-06 … 07-13 are **already burned** as exploratory — they were
used to set the original thresholds, so calibrating on them is legitimate and the
~27-session OOS remains untouched.

**`hawkes` at the spec threshold does not fire on ES.** 3 bps = 9.1 ticks; only
0.012 % of trades ever reach that divergence, and requiring OBI and exhaustion
simultaneously gives **0 signals in a full session**. ES's spread is 1 tick 90 % of
the time and micro-price is bounded inside the spread, so a 9-tick gap is a tail
event. The threshold is almost certainly calibrated for a **crypto** venue — the
reference implementation subscribes to an exchange `level2` WebSocket with
Coinbase/Binance semantics, where relative spreads are orders of magnitude wider.

**`conjunction`: 274 signals → 135 round trips, 64.4 % win, PF 0.453, net −$787.50.**

```
gross P&L    -$112.50    (-0.067 ticks/trade)
commission   -$675.00    ( 0.400 ticks/trade)
net          -$787.50    commission = 85.7% of the net loss
```

**What it actually is.** Fills happen at a spread of **2.06 ticks on average**
(96.3 % of fills occur when spread > 1 tick, against a session-typical 1 tick), and
the +1-tick target sits *inside* that spread. So the strategy is **capturing half
the spread as a maker** — it is a market-making trade, not a mean-reversion trade.
The "mean reversion" premise contributes nothing: `entry_div_ticks` has median
**0.000**, i.e. there is no dislocation at entry to revert from.

**Adverse selection is measurable.** The entry demands OBI ≥ p90, but OBI *at fill*
has median **0.000** — the bid wall you joined is gone by the time you are filled.
You are filled precisely when your order is being run over.

**Favourable excursion does exist** (measured on the mid, at the 2 s horizon):
MFE mean +1.78 ticks, median +1.50, p90 +3.50; 53 % of trades reach +1 tick,
29 % reach +2, 16 % reach +3. MFE/|MAE| = 1.197. So the +1-tick target is **not**
capturing what is available — which is what makes target width a real lever.

> **Measurement note.** An earlier version of this analysis computed MFE against the
> *bid* and concluded there was no favourable excursion at all. That was wrong: a
> maker fills at the bid and exits via an offer, so bid-referenced MFE reads ≈ 0
> even for winners. **Excursion for a passive strategy must be measured on the mid
> or micro-price.**

---

## 7. Open items before any out-of-sample read

1. **No V2 strategy pre-registration exists.** `research/v-reversal/PREREG.md` covers the
   *signal* and states explicitly that no entry rule, stop, target or cost model is
   involved — so it sets no P&L criteria. Each variant needs its own.
2. **VPIN not implemented.** The spec names it as the primary adverse-selection
   defence, and adverse selection is the measured failure mode. This is the largest
   gap between spec and engine.
3. **No marketable-entry path.** The spec permits crossing the spread when
   divergence covers costs; only the passive path exists.
4. **Percentile population differs from the prereg's** — prereg used 1-second
   buckets, the engine uses a per-trade-tick rolling window.
5. **Python 3.14.6 vs Nautilus's tested 3.11–3.13.** First suspect on odd behaviour.
