"""V-Reversal: trapped-trader full-retracement reversal (M1 preferred, RTH).

Drop into QuantLab as  quantlab/strategies/v_reversal.py

STRUCTURE (from Cody Bengtson's *V-Reversal Setup* (2023; third-party document, not redistributed here)), long case:
  1. THIN impulse down into a zone   -> thin_origin = the high it fell from.
     "Thin" = low volume-per-tick vs the zone. This is the precondition:
     thin areas break back through cleanly.
  2. CONSOLIDATION (6-8 ticks)       -> cons_hi / cons_lo / zone volume.
  3. BLOWOFF sell out of the zone    -> the V low. Volume on the extreme bar
     >> the zone average, closing near its low = END of move, not continuation.
  4. V RETRACE: >= retrace_pct of the blowoff leg recovered at similar SPEED.
  5. ENTRY  passive (back inside the zone) | breakout (confirmed break of
     cons_hi on volume).
  6. STOP   cons_lo (narrow) or the V low (wide, justified by the blowoff).
     TARGET thin_origin - buffer.  ABSOLUTE price -- no R multiple anywhere
     in the profit path; R is only ever a sizing input.

Shorts are the mirrored inverted-V (Λ top): the same code path runs on
sign-flipped arrays, so there is one implementation to get right, not two.

DELIBERATELY NOT IMPLEMENTED: the "falling knife" entry. The setup doc itself
says its risk is not definable and it is DOM-only. Backtesting it on bars
would be fiction -- it belongs to the footprint phase.

WHAT BARS CANNOT SEE (why this is a SCREEN, not a verdict): the bottom-5-tick
bid/ask imbalance, the 1000-contracts-in-one-second burst, and trapped-trader
inference. `blowoff_rvol` in the trade context is the hook -- the footprint
phase replaces it with real aggressor imbalance from tick data.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from quantlab.backtest import sizing as sz
from quantlab.backtest.engine import ExitConfig, Signal, run_backtest
from quantlab.data.sessions import RTH_END
from quantlab.indicators import atr as atr_fn
from quantlab.strategies.base import StrategyResult, SymbolData


@dataclass
class VRevParams:
    # --- structure (tick-denominated so it ports across symbols) ---
    thin_max_bars: int = 5           # impulse into the zone must be fast
    thin_min_ticks: float = 8.0      # ...and travel at least this far
    thin_vpt_ratio: float = 0.85     # impulse vol/tick <= ratio * zone vol/tick
    consol_min_bars: int = 8
    consol_max_ticks: float = 8.0    # the doc's 6-8 tick range
    vol_lookback: int = 30           # causal average volume window
    blowoff_mult: float = 3.0        # extreme-bar volume vs the rolling average
    blowoff_close_frac: float = 0.35 # must close in the bottom frac of its range
    blowoff_min_ticks: float = 4.0   # leg must clear the zone low by this much
    retrace_pct: float = 0.90        # "retraced in full"
    speed_ratio: float = 1.5         # retrace bars <= ratio * blowoff bars
    retrace_grace: int = 3
    # --- execution ---
    entry_mode: str = "breakout"     # "passive" | "breakout"
    break_vol_mult: float = 2.0      # confirming volume on the break candle
    break_max_bars: int = 10
    stop_mode: str = "consol"        # "consol" (narrow) | "v_low" (wide)
    stop_buffer_ticks: float = 1.0
    target_mode: str = "thin_origin" # "thin_origin" | "trail" | "origin_then_trail"
    target_buffer_ticks: float = 2.0 # exit a few ticks early to ensure the fill
    scale_frac: float = 0.5
    chandelier_mult: float = 2.0
    atr_period: int = 14
    allow_shorts: bool = True
    flat_before_min: int = 5
    max_trades_per_day: int = 2
    # --- sizing ---
    sizing_mode: str = "risk"        # "risk" | "fixed1"
    risk_pct: float = 0.5
    fractional: bool = True
    starting_equity: float = 100_000.0
    use_costs: bool = True


def _find_long_setups(hi, lo, cl, vol, p: VRevParams, tick: float) -> list[dict]:
    """Anchor on the blowoff bar (the rarest event), then verify structure around it.

    All arrays are day-local and already sign-oriented for the LONG case.
    Returns dicts with day-local indices; caller maps back to global positions.
    """
    n = len(hi)
    need = p.consol_min_bars + p.thin_max_bars + 3
    if n < need + 5:
        return []

    avgv = (
        pd.Series(vol).rolling(p.vol_lookback, min_periods=max(5, p.vol_lookback // 3)).mean().to_numpy()
    )
    out: list[dict] = []

    for j in range(need, n - 3):
        # ---- 3. blowoff bar: volume spike, closing near its low ----
        if not np.isfinite(avgv[j]) or avgv[j] <= 0 or vol[j] < p.blowoff_mult * avgv[j]:
            continue
        rng = hi[j] - lo[j]
        if rng <= 0 or (cl[j] - lo[j]) > p.blowoff_close_frac * rng:
            continue

        # ---- 2. consolidation immediately before the leg ----
        cz_end = j - 1
        cz_start = cz_end - p.consol_min_bars + 1
        if cz_start - p.thin_max_bars < 0:
            continue
        chi = hi[cz_start:cz_end + 1].max()
        clo = lo[cz_start:cz_end + 1].min()
        zone_rng = chi - clo
        if zone_rng <= 0 or zone_rng > p.consol_max_ticks * tick:
            continue
        zvol = vol[cz_start:cz_end + 1].mean()
        if zvol <= 0:
            continue
        if lo[j] > clo - p.blowoff_min_ticks * tick:      # must actually break the zone
            continue

        # ---- 1. thin move INTO the consolidation ----
        ts = cz_start - p.thin_max_bars
        thin_origin = hi[ts:cz_start].max()
        thin_drop = thin_origin - clo
        if thin_drop < p.thin_min_ticks * tick:
            continue
        thin_vpt = vol[ts:cz_start].sum() / max(thin_drop / tick, 1.0)
        zone_vpt = zvol / max(zone_rng / tick, 1.0)
        if thin_vpt > p.thin_vpt_ratio * zone_vpt:        # impulse wasn't thin -> skip
            continue

        # ---- 4. V retrace at similar speed ----
        leg = clo - lo[j]
        if leg <= 0:
            continue
        horizon = j + int(p.speed_ratio * max(1, j - cz_end)) + p.retrace_grace
        r = None
        for k in range(j + 1, min(horizon + 1, n)):
            if hi[k] >= lo[j] + p.retrace_pct * leg:
                r = k
                break
        if r is None:
            continue

        # ---- 5. entry ----
        if p.entry_mode == "passive":
            sig = r
        else:
            sig = None
            for k in range(r, min(r + p.break_max_bars + 1, n)):
                if cl[k] > chi and np.isfinite(avgv[k]) and vol[k] >= p.break_vol_mult * avgv[k]:
                    sig = k
                    break
            if sig is None:
                continue

        out.append(
            {
                "sig": sig,
                "stop": (clo if p.stop_mode == "consol" else lo[j]) - p.stop_buffer_ticks * tick,
                "target": thin_origin - p.target_buffer_ticks * tick,
                "ctx": {
                    "cons_hi": float(chi),
                    "cons_lo": float(clo),
                    "v_low": float(lo[j]),
                    "thin_origin": float(thin_origin),
                    "blowoff_rvol": float(vol[j] / avgv[j]),
                    "leg_ticks": float(leg / tick),
                },
            }
        )
    return out


def _volume(m5: pd.DataFrame) -> np.ndarray:
    col = "realvol" if "realvol" in m5.columns else "tickvol"
    return m5[col].to_numpy(float)


def run_v_reversal(data: SymbolData, p: VRevParams) -> StrategyResult:
    bars = data.m5                                   # M1 if that's what was loaded
    o = bars["open"].to_numpy(float)
    h = bars["high"].to_numpy(float)
    l = bars["low"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    v = _volume(bars)
    atr = atr_fn(bars["high"], bars["low"], bars["close"], p.atr_period, "sma").to_numpy(float)
    tick = data.spec.tick_size

    flat_t = (
        dt.datetime.combine(dt.date(2000, 1, 1), RTH_END) - dt.timedelta(minutes=p.flat_before_min)
    ).time()
    times = bars.index.time
    tdates = bars["trading_date"].to_numpy()
    is_rth = bars["is_rth"].to_numpy(bool)

    signals: list[Signal] = []

    for td, day_pos in pd.Series(np.arange(len(bars)), index=tdates).groupby(level=0):
        dp = day_pos.to_numpy()
        rth = dp[is_rth[dp]]
        flat_c = dp[(times[dp] >= flat_t) & is_rth[dp]]
        if len(rth) < 40 or len(flat_c) == 0:
            continue
        flat_pos = int(flat_c[0])

        found: list[tuple[int, int, dict]] = []
        # long side
        for s in _find_long_setups(h[rth], l[rth], c[rth], v[rth], p, tick):
            found.append((int(rth[s["sig"]]), +1, s))
        # short side: mirror prices so the identical code path finds Λ tops
        if p.allow_shorts:
            for s in _find_long_setups(-l[rth], -h[rth], -c[rth], v[rth], p, tick):
                s = dict(s)
                s["stop"], s["target"] = -s["stop"], -s["target"]
                s["ctx"] = {k: (-val if k in {"cons_hi", "cons_lo", "v_low", "thin_origin"} else val)
                            for k, val in s["ctx"].items()}
                found.append((int(rth[s["sig"]]), -1, s))

        taken = 0
        for sig_pos, direction, s in sorted(found, key=lambda x: x[0]):
            if taken >= p.max_trades_per_day:
                break
            entry_pos = sig_pos + 1
            if entry_pos >= flat_pos or entry_pos >= len(bars) or tdates[entry_pos] != td:
                continue

            cfg = ExitConfig(stop=float(s["stop"]), forced_exit_pos=flat_pos)
            if p.target_mode == "thin_origin":
                cfg.target = float(s["target"])                  # ABSOLUTE, structural
            elif p.target_mode == "trail":
                cfg.chandelier_mult = p.chandelier_mult
            elif p.target_mode == "origin_then_trail":
                cfg.scale_target = float(s["target"])
                cfg.scale_frac = p.scale_frac
                cfg.chandelier_mult = p.chandelier_mult
            else:
                raise ValueError(f"unknown target_mode {p.target_mode!r}")

            signals.append(
                Signal(
                    entry_pos=entry_pos,
                    direction=direction,
                    exit=cfg,
                    context={"trading_date": str(pd.Timestamp(td).date()), **s["ctx"]},
                )
            )
            taken += 1

    spec = data.spec
    size_fn = {
        "risk": sz.risk_pct(p.risk_pct, spec.point_value, p.fractional),
        "fixed1": sz.fixed(1),
    }[p.sizing_mode]

    trades = run_backtest(
        bars,
        signals,
        point_value=spec.point_value,
        tick_size=spec.tick_size,
        atr=atr,
        starting_equity=p.starting_equity,
        size_fn=size_fn,
        commission_per_side=spec.costs.commission_per_side if p.use_costs else 0.0,
        slippage_ticks=spec.costs.slippage_ticks if p.use_costs else 0.0,
    )
    return StrategyResult.build("v_reversal", data, asdict(p), trades, bars, p.starting_equity)
