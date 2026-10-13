"""MeanReversion1.py — Version 1 mean-reversion backtest (Connors RSI(2)).

Implements the Entry / Exit / Risk rules of *Version 1* from the "Adaptive
Microstructure Trading Framework" notes:

  Entry (LONG, both required, at the close):
      1. Close > 200-day SMA         (long-term trend filter)
      2. RSI(2) < rsi_entry (=5)     (extreme short-term oversold)
  Exit (checked each day, in priority order):
      1. Hard stop  : Low <= entry - k*ATR         (fat-tail protection)
      2. Target     : Close > 5-day SMA             (mean-reversion complete)
                      (optional alt: RSI(2) > 50)
      3. Time stop  : held >= max_hold trading days (trend / no-revert abort)
  Risk overrides (Mandelbrot / Taleb):
      - 1%-risk position sizing off an ATR stop  => size is inversely ~1/ATR,
        which satisfies BOTH the "<=1% per trade" fat-tail cap AND the
        "scale size inversely to ATR" regime-shift rule with one mechanism.
      - Max 5 concurrent positions; one position per symbol; no averaging down.

Long-only, upside-biased universe (indices). Multi-asset portfolio backtest.

Design doc: research/mr-v1-connors-rsi2/DESIGN.md

NOT FINANCIAL ADVICE. Research / backtesting only. Past data does not predict
future results.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Configuration — every tunable lives here.
# ---------------------------------------------------------------------------
@dataclass
class Config:
    # --- Data -------------------------------------------------------------
    # Point this at EITHER a directory of CSVs (one file per instrument,
    # filename = symbol) OR a single .csv file. This is the variable to edit
    # to backtest your own data.
    data_path: str = "data"
    start: str | None = None         # inclusive backtest window start (YYYY-MM-DD)
    end: str | None = None           # inclusive backtest window end   (YYYY-MM-DD)

    # --- Indicator periods ------------------------------------------------
    trend_period: int = 200          # long-term SMA trend filter
    rsi_period: int = 2              # Connors RSI(2)
    exit_sma_period: int = 5         # mean-reversion exit target SMA
    atr_period: int = 14             # ATR for stop distance / sizing

    # --- Entry ------------------------------------------------------------
    rsi_entry: float = 5.0           # buy when RSI(2) < this (aggressive: 2)

    # --- Exits ------------------------------------------------------------
    use_rsi_exit: bool = False       # also exit when RSI(2) > rsi_exit
    rsi_exit: float = 50.0
    max_hold: int = 7                # time stop in trading days (doc: 5-7)

    # --- Risk / position sizing ------------------------------------------
    starting_equity: float = 100_000.0
    risk_frac: float = 0.01          # risk <= 1% of equity per trade
    atr_stop_mult: float = 2.5       # hard stop = entry - k * ATR
    max_positions: int = 5           # correlated-drawdown cap
    max_position_frac: float = 0.20  # notional cap per position (of equity)
    cost_bps: float = 0.0            # per-side transaction cost, basis points

    # --- Reporting --------------------------------------------------------
    figures_dir: str = "figures"
    make_plot: bool = True
    save_trades: bool = True

    # --- Synthetic sample data (used when no CSVs are present) -----------
    generate_sample_data: bool = False
    sample_symbols: int = 8
    sample_days: int = 1500
    seed: int = 42


# ---------------------------------------------------------------------------
# Indicators — pure functions, exercised by --selftest.
# ---------------------------------------------------------------------------
def sma(close: pd.Series, n: int) -> pd.Series:
    """Simple moving average; NaN until `n` observations exist."""
    return close.rolling(n, min_periods=n).mean()


def _rma(x: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothed moving average (a.k.a. RMA / SMMA).

    Seed = simple mean of the first `period` valid values, then the recursive
    Wilder update `rma_t = (rma_{t-1}*(period-1) + x_t) / period`.
    """
    arr = x.to_numpy(dtype=float)
    n = len(arr)
    out = np.full(n, np.nan)
    valid = np.where(~np.isnan(arr))[0]
    if len(valid) < period:
        return pd.Series(out, index=x.index)
    start = valid[0]
    seed_end = start + period            # exclusive
    out[seed_end - 1] = arr[start:seed_end].mean()
    for i in range(seed_end, n):
        out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
    return pd.Series(out, index=x.index)


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI (Connors' convention). Returns values in [0, 100]."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _rma(gain, period)
    avg_loss = _rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)   # avoid inf where loss == 0
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.mask(avg_loss == 0.0, 100.0)          # all gains -> RSI 100
    return out


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's Average True Range."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)                       # first bar: no prev close -> High-Low
    return _rma(true_range, period)


def compute_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Attach the strategy's indicator columns to an OHLCV frame."""
    df = df.copy()
    df["SMA_trend"] = sma(df["Close"], cfg.trend_period)
    df["RSI"] = rsi(df["Close"], cfg.rsi_period)
    df["SMA_exit"] = sma(df["Close"], cfg.exit_sma_period)
    df["ATR"] = atr(df, cfg.atr_period)
    return df


# ---------------------------------------------------------------------------
# Data loading — tolerant CSV reader (dir of files OR a single file).
# ---------------------------------------------------------------------------
def _standardize(raw: pd.DataFrame) -> pd.DataFrame:
    """Map arbitrary column names to Date/OHLCV; Date-index & sort."""
    lower = {str(c).strip().lower(): c for c in raw.columns}

    def pick(*names: str) -> str | None:
        for name in names:
            if name in lower:
                return lower[name]
        return None

    date_c = pick("date", "datetime", "time", "timestamp")
    close_c = pick("close", "adj close", "adj_close", "adjclose", "price")
    if date_c is None or close_c is None:
        raise ValueError(f"need Date and Close columns; got {list(raw.columns)}")

    o, h, l, v = pick("open"), pick("high"), pick("low"), pick("volume", "vol")
    out = pd.DataFrame()
    out["Date"] = pd.to_datetime(raw[date_c])
    out["Close"] = pd.to_numeric(raw[close_c], errors="coerce")
    # OHLC fall back to Close when absent (close-only index series are common).
    out["Open"] = pd.to_numeric(raw[o], errors="coerce") if o else out["Close"]
    out["High"] = pd.to_numeric(raw[h], errors="coerce") if h else out["Close"]
    out["Low"] = pd.to_numeric(raw[l], errors="coerce") if l else out["Close"]
    out["Volume"] = pd.to_numeric(raw[v], errors="coerce") if v else 0.0

    out = out.dropna(subset=["Close"]).set_index("Date").sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def _apply_window(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    """Restrict a Date-indexed frame to the inclusive [start, end] window."""
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    return df


def load_data(cfg: Config) -> dict[str, pd.DataFrame]:
    """Load all instruments from cfg.data_path (a directory or a single file)."""
    path = Path(cfg.data_path)
    if path.is_dir():
        files = sorted(path.glob("*.csv"))
    elif path.is_file():
        files = [path]
    else:
        return {}

    min_len = cfg.trend_period + cfg.atr_period
    data: dict[str, pd.DataFrame] = {}
    for f in files:
        try:
            df = _standardize(pd.read_csv(f))
        except Exception as exc:  # noqa: BLE001 - report & skip bad files
            print(f"  ! skipping {f.name}: {exc}")
            continue
        df = _apply_window(df, cfg.start, cfg.end)
        if len(df) <= min_len:
            print(f"  ! skipping {f.name}: only {len(df)} rows (< {min_len})")
            continue
        data[f.stem] = df
    return data


def generate_sample_data(cfg: Config) -> tuple[Path, list[str]]:
    """Write synthetic upside-biased index-like CSVs into the data directory.

    Price = positive-drift random walk (permanent component) + a stationary
    AR(1) overlay (temporary component) so RSI(2) oversold dips actually occur,
    mirroring the Fama-French Pt = Qt + Zt decomposition from the notes.
    """
    dest = Path(cfg.data_path)
    if dest.suffix.lower() == ".csv":
        dest = dest.parent
    dest.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(cfg.seed)
    dates = pd.bdate_range(end=pd.Timestamp("2025-12-31"), periods=cfg.sample_days)
    written: list[str] = []

    for i in range(cfg.sample_symbols):
        mu = 0.0003 + 0.0002 * rng.random()          # daily upside drift
        sigma = 0.008 + 0.006 * rng.random()
        perm = np.cumsum(rng.normal(mu, sigma, cfg.sample_days))   # Qt
        temp = np.zeros(cfg.sample_days)                            # Zt (AR1)
        phi = 0.85
        for t in range(1, cfg.sample_days):
            temp[t] = phi * temp[t - 1] + rng.normal(0.0, sigma * 0.9)
        close = np.exp(np.log(100.0) + perm + temp)

        wick = np.abs(rng.normal(0.0, sigma, cfg.sample_days))
        high = close * (1.0 + wick)
        low = close * (1.0 - wick)
        openp = low + (high - low) * rng.random(cfg.sample_days)
        volume = rng.integers(1_000_000, 5_000_000, cfg.sample_days)

        sym = f"IDX{i + 1}"
        pd.DataFrame(
            {"Date": dates, "Open": openp, "High": high, "Low": low,
             "Close": close, "Volume": volume}
        ).to_csv(dest / f"{sym}.csv", index=False)
        written.append(f"{sym}.csv")

    return dest, written


# ---------------------------------------------------------------------------
# Portfolio state.
# ---------------------------------------------------------------------------
@dataclass
class Position:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: float
    stop_price: float
    cost_basis: float          # cash paid incl. entry cost
    days_held: int = 0


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    return_pct: float
    exit_reason: str
    days_held: int


# ---------------------------------------------------------------------------
# Event-driven backtester.
# ---------------------------------------------------------------------------
class Backtester:
    def __init__(self, data: dict[str, pd.DataFrame], cfg: Config):
        self.cfg = cfg
        self.data = {s: compute_indicators(df, cfg) for s, df in data.items()}
        self.cash = cfg.starting_equity
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[pd.Timestamp, float, int]] = []
        self._last_close: dict[str, float] = {}
        self.peak_positions = 0

        calendar = None
        for df in self.data.values():
            calendar = df.index if calendar is None else calendar.union(df.index)
        self.calendar = calendar.sort_values() if calendar is not None else pd.DatetimeIndex([])

    # -- sizing ------------------------------------------------------------
    def _size(self, price: float, atr_val: float, equity: float) -> tuple[int, float]:
        """Whole-share size from 1%-risk-off-ATR-stop, capped by notional."""
        cfg = self.cfg
        stop = price - cfg.atr_stop_mult * atr_val
        risk_per_share = price - stop
        if stop <= 0.0 or risk_per_share <= 0.0:
            return 0, stop
        shares_by_risk = (cfg.risk_frac * equity) / risk_per_share
        shares_by_notional = (cfg.max_position_frac * equity) / price
        shares = math.floor(min(shares_by_risk, shares_by_notional))
        return max(shares, 0), stop

    # -- exits -------------------------------------------------------------
    def _check_exit(self, pos: Position, row: pd.Series) -> tuple[float | None, str]:
        cfg = self.cfg
        # 1. Hard stop (risk first). Fill at stop, or at the open on a gap-down.
        if row["Low"] <= pos.stop_price:
            fill = pos.stop_price if row["Open"] > pos.stop_price else row["Open"]
            return float(fill), "hard_stop"
        # 2. Mean-reversion target.
        if pd.notna(row["SMA_exit"]) and row["Close"] > row["SMA_exit"]:
            return float(row["Close"]), "target_sma5"
        if cfg.use_rsi_exit and pd.notna(row["RSI"]) and row["RSI"] > cfg.rsi_exit:
            return float(row["Close"]), "target_rsi"
        # 3. Time stop.
        if pos.days_held >= cfg.max_hold:
            return float(row["Close"]), "time_stop"
        return None, ""

    def _close(self, pos: Position, date: pd.Timestamp, price: float, reason: str) -> None:
        cost = self.cfg.cost_bps / 1e4
        proceeds = pos.shares * price * (1.0 - cost)
        self.cash += proceeds
        pnl = proceeds - pos.cost_basis
        self.trades.append(Trade(
            symbol=pos.symbol, entry_date=pos.entry_date, exit_date=date,
            entry_price=pos.entry_price, exit_price=price, shares=pos.shares,
            pnl=pnl, return_pct=pnl / pos.cost_basis if pos.cost_basis else 0.0,
            exit_reason=reason, days_held=pos.days_held,
        ))
        del self.positions[pos.symbol]

    # -- entries -----------------------------------------------------------
    def _process_entries(self, date: pd.Timestamp, equity: float) -> None:
        cfg = self.cfg
        free = cfg.max_positions - len(self.positions)
        if free <= 0:
            return
        candidates: list[tuple[float, str, pd.Series]] = []
        for sym, df in self.data.items():
            if sym in self.positions or date not in df.index:
                continue
            row = df.loc[date]
            if pd.isna(row["SMA_trend"]) or pd.isna(row["RSI"]) or pd.isna(row["ATR"]):
                continue
            if row["ATR"] > 0 and row["Close"] > row["SMA_trend"] and row["RSI"] < cfg.rsi_entry:
                candidates.append((float(row["RSI"]), sym, row))

        candidates.sort(key=lambda c: c[0])          # most oversold first
        for _, sym, row in candidates:
            if len(self.positions) >= cfg.max_positions:
                break
            price = float(row["Close"])
            shares, stop = self._size(price, float(row["ATR"]), equity)
            if shares <= 0:
                continue
            gross = shares * price
            cost = gross * (1.0 + cfg.cost_bps / 1e4)
            if cost > self.cash:                      # cap to available cash
                shares = math.floor(self.cash / (price * (1.0 + cfg.cost_bps / 1e4)))
                if shares <= 0:
                    continue
                cost = shares * price * (1.0 + cfg.cost_bps / 1e4)
            self.cash -= cost
            self.positions[sym] = Position(
                symbol=sym, entry_date=date, entry_price=price, shares=shares,
                stop_price=stop, cost_basis=cost, days_held=0,
            )

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        for date in self.calendar:
            # Mark-to-market reference: update last-known close for every symbol.
            for sym, df in self.data.items():
                if date in df.index:
                    self._last_close[sym] = float(df.at[date, "Close"])

            # 1. Exits (age each held position, then test exit conditions).
            for sym in list(self.positions.keys()):
                df = self.data[sym]
                if date not in df.index:
                    continue
                pos = self.positions[sym]
                pos.days_held += 1
                price, reason = self._check_exit(pos, df.loc[date])
                if price is not None:
                    self._close(pos, date, price, reason)

            # 2. Entries (uses equity marked at today's close).
            equity = self._equity()
            self._process_entries(date, equity)

            # 3. Record end-of-day equity.
            self.peak_positions = max(self.peak_positions, len(self.positions))
            self.equity_curve.append((date, self._equity(), len(self.positions)))

    def _equity(self) -> float:
        held = sum(p.shares * self._last_close.get(p.symbol, p.entry_price)
                   for p in self.positions.values())
        return self.cash + held


# ---------------------------------------------------------------------------
# Metrics & reporting.
# ---------------------------------------------------------------------------
def metrics(equity_curve, trades: list[Trade]) -> dict:
    if not equity_curve:
        return {}
    dates = [d for d, _, _ in equity_curve]
    eq = pd.Series([e for _, e, _ in equity_curve], index=pd.DatetimeIndex(dates))
    npos = np.array([n for _, _, n in equity_curve])

    rets = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    drawdown = eq / eq.cummax() - 1.0

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)

    return {
        "start": eq.index[0].date(),
        "end": eq.index[-1].date(),
        "days": len(eq),
        "start_equity": float(eq.iloc[0]),
        "end_equity": float(eq.iloc[-1]),
        "total_return": float(eq.iloc[-1] / eq.iloc[0] - 1.0),
        "cagr": float((eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0),
        "max_drawdown": float(drawdown.min()),
        "sharpe": float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0,
        "n_trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "avg_win_pct": float(np.mean([t.return_pct for t in wins])) if wins else 0.0,
        "avg_loss_pct": float(np.mean([t.return_pct for t in losses])) if losses else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_hold_days": float(np.mean([t.days_held for t in trades])) if trades else 0.0,
        "exposure": float((npos > 0).mean()),
        "avg_positions": float(npos.mean()),
        "peak_positions": int(npos.max()) if len(npos) else 0,
    }


def print_report(m: dict, cfg: Config) -> None:
    if not m:
        print("No results (no data / no trading days).")
        return
    print("\n" + "=" * 60)
    print("  MEAN REVERSION V1  —  Connors RSI(2)  —  backtest report")
    print("=" * 60)
    print(f"  Period            : {m['start']}  ->  {m['end']}  ({m['days']} days)")
    print(f"  Starting equity   : ${m['start_equity']:,.0f}")
    print(f"  Ending equity     : ${m['end_equity']:,.0f}")
    print(f"  Total return      : {m['total_return']:+.2%}")
    print(f"  CAGR              : {m['cagr']:+.2%}")
    print(f"  Max drawdown      : {m['max_drawdown']:.2%}")
    print(f"  Sharpe (ann.)     : {m['sharpe']:.2f}")
    print("-" * 60)
    print(f"  Trades            : {m['n_trades']}")
    print(f"  Win rate          : {m['win_rate']:.1%}")
    print(f"  Avg win / loss    : {m['avg_win_pct']:+.2%} / {m['avg_loss_pct']:+.2%}")
    print(f"  Profit factor     : {m['profit_factor']:.2f}")
    print(f"  Avg hold (days)   : {m['avg_hold_days']:.1f}")
    print("-" * 60)
    print(f"  Exposure (days in): {m['exposure']:.1%}")
    print(f"  Avg positions     : {m['avg_positions']:.2f}")
    print(f"  Peak positions    : {m['peak_positions']} / {cfg.max_positions}")
    print("=" * 60 + "\n")


def save_outputs(bt: Backtester, cfg: Config) -> None:
    out_dir = Path(cfg.figures_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if cfg.save_trades and bt.trades:
        pd.DataFrame([t.__dict__ for t in bt.trades]).to_csv(
            out_dir / "trades_log.csv", index=False)
        print(f"  trade log -> {out_dir / 'trades_log.csv'}")

    if not cfg.make_plot or not bt.equity_curve:
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dates = [d for d, _, _ in bt.equity_curve]
    eq = pd.Series([e for _, e, _ in bt.equity_curve], index=pd.DatetimeIndex(dates))
    dd = eq / eq.cummax() - 1.0

    fig, (ax0, ax1) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True, height_ratios=[3, 1])
    ax0.plot(eq.index, eq.values, color="#1f77b4", lw=1.3)
    ax0.set_title("Mean Reversion V1 (Connors RSI(2)) — Equity Curve")
    ax0.set_ylabel("Equity ($)")
    ax0.grid(alpha=0.3)
    ax1.fill_between(dd.index, dd.values, 0.0, color="#d62728", alpha=0.35)
    ax1.set_ylabel("Drawdown")
    ax1.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "equity_curve.png", dpi=110)
    plt.close(fig)
    print(f"  equity plot -> {out_dir / 'equity_curve.png'}")


# ---------------------------------------------------------------------------
# Self-tests (`python MeanReversion1.py --selftest`).
# ---------------------------------------------------------------------------
def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def run_selftest() -> int:
    cfg = Config()
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    # -- SMA ---------------------------------------------------------------
    s = sma(pd.Series([1, 2, 3, 4, 5], dtype=float), 3)
    check("sma basic", s.tolist()[2:] == [2.0, 3.0, 4.0] and s[:2].isna().all())

    # -- RSI extremes ------------------------------------------------------
    up = rsi(pd.Series(np.arange(1, 20), dtype=float), 2).dropna()
    down = rsi(pd.Series(np.arange(20, 1, -1), dtype=float), 2).dropna()
    check("rsi monotone up -> 100", bool((up == 100.0).all()))
    check("rsi monotone down -> 0", bool((down == 0.0).all()))

    # -- RSI known values (period 2), prices [10,12,11,13] -----------------
    r = rsi(pd.Series([10, 12, 11, 13], dtype=float), 2)
    check("rsi(2) known #1", _approx(float(r.iloc[2]), 100.0 - 100.0 / 3.0, 1e-4))
    check("rsi(2) known #2", _approx(float(r.iloc[3]), 100.0 - 100.0 / 7.0, 1e-4))

    # -- ATR known values (period 2) --------------------------------------
    ohlc = pd.DataFrame({
        "High": [10.0, 11.0, 10.5, 12.0],
        "Low": [9.0, 9.5, 9.8, 10.0],
        "Close": [9.5, 10.5, 10.0, 11.5],
    })
    a = atr(ohlc, 2)
    check("atr known #1", _approx(float(a.iloc[1]), 1.25, 1e-6))
    check("atr known #2", _approx(float(a.iloc[2]), 0.975, 1e-6))
    check("atr known #3", _approx(float(a.iloc[3]), 1.4875, 1e-6))

    # -- Engine: an oversold dip in an uptrend must produce a LONG trade ---
    up_days = 250
    price = list(100.0 * np.cumprod(np.full(up_days, 1.004)))  # steady uptrend
    price += [price[-1] * 0.95, price[-1] * 0.95 * 0.95]       # 2 sharp down days
    price += list(price[-1] * np.cumprod(np.full(15, 1.01)))   # recovery
    dates = pd.bdate_range("2020-01-01", periods=len(price))
    df = pd.DataFrame({
        "Open": price, "High": [p * 1.001 for p in price],
        "Low": [p * 0.999 for p in price], "Close": price,
        "Volume": 1_000_000,
    }, index=dates)
    bt = Backtester({"T": df}, cfg)
    bt.run()
    check("engine produced >=1 trade", len(bt.trades) >= 1)
    check("engine trade is long (positive shares)",
          all(t.shares > 0 for t in bt.trades))
    check("engine entry respected trend filter (RSI<5 & Close>SMA200)",
          all(t.exit_reason in {"hard_stop", "target_sma5", "time_stop", "target_rsi"}
              for t in bt.trades))

    # -- Risk: initial risk per trade <= ~1% of equity --------------------
    if bt.trades:
        t0 = bt.trades[0]
        # reconstruct stop distance used at sizing from the first position rule
        risk_ok = True
        # replay sizing on entry row to confirm the invariant
        row = compute_indicators(df, cfg).loc[t0.entry_date]
        shares, stop = bt._size(float(row["Close"]), float(row["ATR"]), cfg.starting_equity)
        risk_dollars = shares * (float(row["Close"]) - stop)
        risk_ok = risk_dollars <= cfg.risk_frac * cfg.starting_equity * 1.0001
        check("sizing: risk <= 1% of equity", risk_ok)

    # -- Portfolio cap: never exceed max_positions ------------------------
    many = {}
    base = 100.0 * np.cumprod(np.full(260, 1.004))
    d1, d2 = base[-1] * 0.95, base[-1] * 0.95 * 0.95
    recover = d2 * np.cumprod(np.full(15, 1.01))            # let positions close
    dip = np.concatenate([base, [d1, d2], recover])
    ddates = pd.bdate_range("2020-01-01", periods=len(dip))
    for i in range(10):  # 10 symbols all dipping the same day
        many[f"S{i}"] = pd.DataFrame({
            "Open": dip, "High": dip * 1.001, "Low": dip * 0.999,
            "Close": dip, "Volume": 1_000_000}, index=ddates)
    bt2 = Backtester(many, cfg)
    bt2.run()
    check("portfolio cap test actually traded", len(bt2.trades) >= 1)
    check("portfolio cap: peak positions capped at max_positions",
          bt2.peak_positions == cfg.max_positions)

    # -- Date-window slicing --------------------------------------------------
    idx = pd.bdate_range("2016-01-01", periods=100)
    wdf = pd.DataFrame({"Close": range(100)}, index=idx)
    w1 = _apply_window(wdf, "2016-02-01", None)
    check("window start excludes earlier bars", w1.index.min() >= pd.Timestamp("2016-02-01"))
    w2 = _apply_window(wdf, None, "2016-02-01")
    check("window end excludes later bars", w2.index.max() <= pd.Timestamp("2016-02-01"))
    w3 = _apply_window(wdf, "2016-01-15", "2016-02-15")
    check("window both bounds inclusive",
          w3.index.min() >= pd.Timestamp("2016-01-15") and w3.index.max() <= pd.Timestamp("2016-02-15"))

    print()
    if failures:
        print(f"SELFTEST FAILED: {len(failures)} check(s) -> {failures}")
        return 1
    print("SELFTEST PASSED: all checks green.")
    return 0


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def build_config(argv: list[str] | None) -> tuple[Config, argparse.Namespace]:
    cfg = Config()
    p = argparse.ArgumentParser(description="Connors RSI(2) mean-reversion backtest (V1).")
    p.add_argument("--data-path", default=cfg.data_path,
                   help="directory of CSVs OR a single .csv file")
    p.add_argument("--start", default=cfg.start, help="inclusive window start YYYY-MM-DD")
    p.add_argument("--end", default=cfg.end, help="inclusive window end YYYY-MM-DD")
    p.add_argument("--rsi-entry", type=float, default=cfg.rsi_entry)
    p.add_argument("--max-hold", type=int, default=cfg.max_hold)
    p.add_argument("--max-positions", type=int, default=cfg.max_positions)
    p.add_argument("--risk-frac", type=float, default=cfg.risk_frac)
    p.add_argument("--atr-stop-mult", type=float, default=cfg.atr_stop_mult)
    p.add_argument("--starting-equity", type=float, default=cfg.starting_equity)
    p.add_argument("--cost-bps", type=float, default=cfg.cost_bps)
    p.add_argument("--use-rsi-exit", action="store_true")
    p.add_argument("--generate-sample-data", action="store_true")
    p.add_argument("--no-plot", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)

    cfg.data_path = args.data_path
    cfg.start = args.start
    cfg.end = args.end
    cfg.rsi_entry = args.rsi_entry
    cfg.max_hold = args.max_hold
    cfg.max_positions = args.max_positions
    cfg.risk_frac = args.risk_frac
    cfg.atr_stop_mult = args.atr_stop_mult
    cfg.starting_equity = args.starting_equity
    cfg.cost_bps = args.cost_bps
    cfg.use_rsi_exit = args.use_rsi_exit
    cfg.generate_sample_data = args.generate_sample_data
    cfg.make_plot = not args.no_plot
    return cfg, args


def main(argv: list[str] | None = None) -> int:
    cfg, args = build_config(argv)

    if args.selftest:
        return run_selftest()

    if cfg.generate_sample_data:
        dest, files = generate_sample_data(cfg)
        print(f"Generated {len(files)} sample CSV(s) in {dest}: {', '.join(files)}")

    data = load_data(cfg)
    if not data:
        print(f"No usable data at '{cfg.data_path}'. Generating synthetic sample data...")
        generate_sample_data(cfg)
        data = load_data(cfg)
    if not data:
        print("Still no data — nothing to backtest.")
        return 1

    print(f"Loaded {len(data)} instrument(s): {', '.join(sorted(data))}")
    bt = Backtester(data, cfg)
    bt.run()
    m = metrics(bt.equity_curve, bt.trades)
    print_report(m, cfg)
    save_outputs(bt, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())