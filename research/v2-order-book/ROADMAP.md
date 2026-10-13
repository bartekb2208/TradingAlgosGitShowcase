# V2 — Order-Book Exhaustion (V-Reversal) on NautilusTrader — roadmap & design

**Compiled 2026-08-02.** V2 is the **tradeable-strategy layer** on top of the V-reversal signal.
`research/v-reversal/PREREG.md` validates whether the *signal* exists (no execution/costs); V2 adds
execution, exits, risk overrides, and realistic fills via NautilusTrader. Two entry variants live
in separate files sharing one core.

> NOT FINANCIAL ADVICE. Research only.

## Environment — Phase 0 ✅ DONE
- venv at `.venv` (a private per-project library folder). **Python 3.14.6.**
- Installed: **nautilus_trader 1.230.0**, **databento 0.82.0** (import verified).
- Caveat: 3.14 is newer than Nautilus's officially-tested 3.11–3.13. It installed native wheels
  and imports fine — but it's the first suspect if a deep backtest misbehaves.

## Data
- Raw MBO: `data/L3/GLBX-*/glbx-mdp3-YYYYMMDD.mbo.dbn.zst` — 18 GB, schema `mbo`, dataset
  `GLBX.MDP3`. ES batch ≈ Jun–Jul 2026 (41 files), NQ batch ≈ Apr–May. **~0.5 GB/day.**
- **Parent symbology:** every contract + calendar spread shares one tape. The front-month outright
  per session is already recorded in `data/L2/_manifest_{ES,NQ}.json`
  (date → `instrument_id` → contract, e.g. ES 2026-06-08 → `42140864` / `ESM6`). Validated: the
  front-month is ~78% of records; full MBO action set (R/A/C/M/T/F) present.
- **No DEFINITION file** in the batch → the ES/NQ `FuturesContract` instrument must be constructed
  in code (ES tick 0.25 = $12.50, multiplier $50; NQ tick 0.25 = $5, multiplier $20).

## Nautilus API (verified, 1.230)
```python
from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.model.enums import BookType

loader = DatabentoDataLoader()
deltas = loader.from_dbn_file(path, instrument_id=InstrumentId.from_str("ESM6.GLBX"),
                              as_legacy_cython=False)   # -> list[OrderBookDelta] (+ TradeTick)
catalog = ParquetDataCatalog("./v2_catalog")
catalog.write_data([instrument]); catalog.write_data(deltas)
# backtest: engine.add_instrument(instrument); engine.add_data(deltas)
# strategy: self.subscribe_order_book_deltas(instrument_id, book_type=BookType.L3_MBO)
#           on_order_book_delta(self, delta) / on_trade_tick(self, tick)
```

## Architecture / files (DRY: shared core + two entry variants)
| File | Responsibility |
|---|---|
| `python/v2/data_pipeline.py` | one session's MBO → filter to front-month (via L2 manifest) → build instrument → load to a `ParquetDataCatalog` |
| `python/v2/v2_core.py` | shared metrics + `V2StrategyBase(Strategy)`: book/trade wiring, execution, exits, risk. Only `entry_signal()` abstract |
| `python/v2/v2MeanReversion_conjunction.py` | entry = vol_sell ≥ p99 AND imbalance ≥ p90 (VREVERSAL) |
| `python/v2/v2MeanReversion_hawkes.py` | entry = divergence + imbalance + Hawkes λ(t) exhaustion (design doc) |
| `python/v2/run_backtest.py` | CLI: strategy + session → BacktestEngine → report/artifacts |

**Shared metrics (per event):** micro-price (size-weighted top-of-book mid), OBI/imbalance,
rolling aggressive-sell volume + online p99 tracker, sell-intensity λ(t) (EWMA/Hawkes), spread +
rolling avg. **Execution/exits:** passive-limit entry at bid (or marketable cross when divergence
covers cost); exit on revert to micro-price, else 2-second time-stop; realistic fills + commission.
**Risk (MVP):** 1 position at a time + spread-blowout abort (>3× rolling avg). **Deferred:** VPIN.

## Phased plan (status)
| Phase | Deliverable | Status |
|---|---|---|
| 0 · Environment | nautilus + databento in venv | ✅ done |
| 1 · Data → Nautilus | `data_pipeline.py`; one ES session in a catalog | ✅ done — filters front-month, writes book deltas to catalog (verified end-to-end on 2026-06-07) |
| 2 · Core + conjunction | `v2_core.py` + conjunction strategy; first one-session backtest + report | ✅ done — runs end-to-end (2026-06-19: 2.4M deltas → passive fills → +1-tick reversion round-trip, 13s). **v0.1 / preliminary**: fill-rate, order lifecycle, `multiplier=1`, and calibration all still to review (Phase 4) |
| 3 · Hawkes strategy | `v2MeanReversion_hawkes.py`, shared core; compare | ✅ done — divergence + OBI>0.30 + λ(t)-exhaustion, on the shared core; runs (2026-06-19: 1 signal vs conjunction's 11 — far more selective, as designed). v0.1/preliminary |
| 4 · Calibrate + test | freeze thresholds on VREVERSAL exploratory (ES 07-06→07-13), run OOS ~27 ES once (pre-registered), scale 1→55 sessions; NQ robustness | |
| 5 · Later | VPIN, live path | deferred |

## Phase 1 findings (resolved) & remaining risks
- **Loading (resolved):** `from_dbn_file(path, price_precision=2, as_legacy_cython=False)` with NO
  `instrument_id` — Nautilus maps every record via symbology; then filter the returned **pyo3**
  deltas to the front-month `InstrumentId` (e.g. `ESM6.GLBX`). `price_precision` is required (no
  DEFINITION file). Do NOT `isinstance`-check against the Cython `OrderBookDelta` (pyo3 objects
  won't match). Verified: 681k front-month deltas → catalog.
- **Trades (resolved):** the MBO loader is **book-only** (`include_trades=True` errors on MBO).
  The trade tape comes from the verified L2 parquet (`mbo_to_l2` output, `ES_<date>_trades.parquet`:
  ts/price/size/aggressor) → convert to Nautilus `TradeTick` in Phase 2.
- 0.5 GB/day → process **one session at a time**, never the whole 18 GB at once; watch memory.
- ES multiplier from `es_future(...)` is 1, not $50 — set correctly for PnL in Phase 2.
- Python 3.14 vs Nautilus's official 3.11–3.13.

## Phase 2 findings (resolved)
- **Backtest wiring (proven):** `BacktestEngine.add_data` wants **Cython** objects, so reload the
  session with `as_legacy_cython=True` for the manual engine path (the catalog holds pyo3 for the
  BacktestNode/scaling path). `add_venue(..., book_type=BookType.L3_MBO)`; strategy handler is
  **`on_order_book_deltas`** (plural, batched); read the book via `self.cache.order_book(id)` →
  `best_bid_price()/best_ask_price()`. Verified: 200k deltas replay, correct 1-tick ES spread.
- **Trades → TradeTick (done):** `v2_core.load_trade_ticks()` maps the L2 tape
  (aggressor −1→SELLER, +1→BUYER) to Cython `TradeTick`s (add_data-compatible).
- Book cannot be sliced mid-stream (orders resting from before a window would be missed) — a
  correct backtest replays a **whole session** to warm the book; gate only signals/output.

## How you'll run it (once Phase 1–2 are built)
```bash
.venv/bin/python -m v2.data_pipeline  --symbol ES --session 2026-06-08   # -> catalog
.venv/bin/python -m v2.run_backtest   --strategy conjunction --session 2026-06-08
# -> research/v2-order-book/artifacts/<tag>/  (report, trades, fills)
```
