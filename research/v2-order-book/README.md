# V2 — order-book exhaustion on NautilusTrader with CME market-by-order data

**Verdict: no edge in exploratory sessions (135 trades, PF 0.45, gross negative before costs).
A refined trial was pre-registered and the sealed sessions were never read; the line was parked.**

> Research only. Not financial advice.

The most engineering-heavy work in the repository: an event-driven backtest that replays ES
futures **market-by-order** data (Databento GLBX.MDP3, L3) through NautilusTrader with a
**queue-position-aware** venue model and real CME commission, so a passive order only fills once
the queue ahead of it has traded.

| File | What it is |
|---|---|
| [`DESIGN.md`](DESIGN.md) | Architecture, data pipeline, and the NautilusTrader behaviours verified against a pinned version |
| [`ENGINE_FROZEN.md`](ENGINE_FROZEN.md) | Engine freeze: what's locked, versioning, known pitfalls |
| [`ROADMAP.md`](ROADMAP.md) | How the track was sequenced |
| [`PREREG.md`](PREREG.md) | The pre-registered strategy trial, with the exploratory findings it was written against |
| [`WALKFORWARD_DESIGN.md`](WALKFORWARD_DESIGN.md) | A walk-forward design for choosing one parameter without spending the sealed set |
| [`artifacts/`](artifacts/) | Exploratory and smoke-test reports |

**Code:** [`python/v2/`](../../python/v2/): `data_pipeline.py` (MBO → catalog, front-month
isolation), `v2_core.py` (strategy base and signals), `run_backtest.py` (engine wiring),
plus two thin strategy variants.

Two findings from building it:

- **A NautilusTrader accounting trap.** Under `OmsType.NETTING`, Nautilus reuses one position ID
  per instrument, so `cache.positions_closed()` reported **1** closed position for a session with
  **28** `PositionClosed` events. Every aggregate built from the cache would have been wrong. Round
  trips are now accumulated from the event stream, and the rule is written into the engine freeze.
- **The failure mode was measured, not guessed: adverse selection.** Order-book imbalance at the
  moment of fill had a median of 0.000 against an entry requirement at the 90th percentile, and
  96% of fills happened when the spread had widened beyond its usual single tick. By the time a
  passive order filled, the condition that justified it was gone.

Market data is licensed and not included, so the data-dependent modules can't run from this
repository; `v2_core.py --selftest` runs without data.
