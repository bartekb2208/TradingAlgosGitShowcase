# Mean Reversion V1 — Connors RSI(2) Backtest — Design

**Date:** 2026-07-31
**File:** all code in `MeanReversion1.py` (single-file constraint)
**Source:** "Adaptive Microstructure Trading Framework" PDF, Version 1 (pages 6–7).

## Goal

Backtest **Version 1** of the mean-reversion strategy: the Connors RSI(2)
ruleset. Long-only, on an upside-biased universe (indices), multi-asset with the
Mandelbrot/Taleb portfolio risk overrides active. Version 2 (order-book
exhaustion / L2) and the page-3 "ATR-spike short" idea are explicitly out of
scope.

## Non-goals (YAGNI)

- No live trading, no broker integration, no L2/order-book data.
- No short side (equity/index markets have structural upside bias; the doc warns
  against mirror-image shorting).
- No parameter optimization / walk-forward — a single configurable backtest run.

## Data layer

- `data_path` config variable accepts **either** a directory (load every `*.csv`
  inside) **or** a single `.csv` file path. Default: the `data/` directory.
- Loader is tolerant: filename stem = symbol; case-insensitive
  `Date, Open, High, Low, Close, Volume` columns; Date parsed to a sorted
  DatetimeIndex.
- `data/` and `figures/` are already gitignored (existing repo convention:
  datasets fetched on demand, plots generated).
- `generate_sample_data()` writes N upside-biased index-like series
  (positive-drift GBM + a mean-reverting overlay so RSI(2) dips actually occur)
  into the data dir. Runs only when the data dir is empty or
  `--generate-sample-data` is passed, so the backtest works out-of-the-box and
  steps aside for real CSVs.

## Indicators (pure, self-tested functions)

- `rsi(close, period=2)` — **Wilder's** RSI (Connors' convention).
- `sma(close, n)` — for the 200-day trend filter and 5-day exit target.
- `atr(df, period=14)` — **Wilder's** ATR, for stop distance / sizing.

## Strategy rules (Version 1, long-only)

**Entry** — both required, evaluated at the daily close:
1. `Close > SMA200` (long-term trend filter).
2. `RSI(2) < rsi_entry` (default 5; aggressive 2). → Buy at that day's close.

**Exit** — checked each day in this priority order:
1. **Hard stop** (risk first): if the day's `Low <= stop_price` → fill at
   `stop_price`, or at the open if the bar gapped open below the stop.
2. **Primary target**: `Close > SMA5` → exit at the close.
   - Alternative target `RSI(2) > 50` available via config, default off.
3. **Time stop**: in-trade for `>= max_hold` days → exit at the close.

Execution is close-to-close; every signal uses only information available
through that close (no look-ahead). No same-day exit on the entry bar.

## Risk overrides — concrete interpretations (the judgment calls)

| Doc rule | Implementation | Default |
|---|---|---|
| "no single trade > 1% of portfolio" + "Hard Stops" | **1%-risk sizing**: `shares = risk_frac·equity / (entry − stop)`, `stop = entry − k·ATR`. Max loss if stopped ≈ 1% of equity (canonical "1% rule"). | `risk_frac=0.01`, `k=2.5` |
| "Scale position size inversely to ATR" | Falls out of the same formula: `shares ∝ 1/ATR`. One mechanism covers both fat-tail and regime-shift overrides. | — |
| "Cap max positions to 5" | Hard cap on concurrent open positions. More signals than free slots → take the **most oversold** (lowest RSI2). One position per symbol; **no averaging down**. | `max_positions=5` |
| "Exit if no 5-day-SMA cross within 5–7 days" | Time stop at close of day N. | `max_hold=7` |
| "Buy/Sell at market close" | Close-to-close execution, no look-ahead. | — |

**Guards:** per-position notional cap (default 20% of equity) + available-cash
check prevent absurd sizes when ATR is tiny. Starting equity $100k, long-only,
no leverage. Configurable transaction cost in bps (default 0).

## Architecture (single file)

1. `@dataclass Config` at top — all params above, incl. `data_path`. Light
   `argparse` overrides.
2. Data: `load_data(data_path)`, `generate_sample_data(...)`.
3. Indicators: `rsi`, `sma`, `atr`; `compute_indicators(df, cfg)` adds columns.
4. `@dataclass Position` and a `Backtester` holding portfolio state (cash,
   open positions, equity curve, trade log).
5. `Backtester.run()` — event loop over the union trading calendar: mark to
   market, process exits, then process ranked entries subject to the max-5 cap
   and sizing.
6. `metrics(equity_curve, trades)` — total return, CAGR, max drawdown,
   annualized Sharpe, win rate, avg win/avg loss, # trades, exposure.
7. Reporting: console summary + trade log; equity-curve plot saved to `figures/`.
8. `--selftest`: asserts indicator correctness against known RSI/SMA/ATR values
   and that a constructed oversold scenario forces a trade. Verification hook
   given the single-file constraint.

## Testing / verification

- Indicator unit checks with hand-computable inputs (Wilder RSI/ATR, SMA).
- Engine sanity: constructed series that must produce ≥1 trade; assert no
  look-ahead (signals only use data ≤ current close); assert max-5 cap and
  no-averaging-down invariants hold across the run.
- Runnable via `python MeanReversion1.py --selftest`.

## Open assumptions (flagged for the user)

- "1%" read as **risk-based** sizing (loss-if-stopped ≤ 1%), not literal notional
  ≤ 1% of equity — approved.
- Close-to-close execution — approved.
- Real CSV schema assumed OHLCV with a Date column; loader is tolerant but may
  need a tweak if real files differ.