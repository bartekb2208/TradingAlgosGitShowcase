"""vr_detect.py — offline VR structure detector over the L2 tape.

Runs the SAME VRStateMachine the Nautilus strategy uses, but driven from pandas
so threshold calibration iterates in seconds instead of minutes. This layer
counts structures and measures forward moves; it makes NO P&L claim -- only the
engine layer does that.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from vr_core import NS, TICK, VRConfig, VRState, VRStateMachine
from vr_data import EXPLORATORY, load_l2


def detect_session(symbol: str, session: str, cfg: VRConfig) -> list[dict]:
    """Every ARMED event in one session, with its structural context."""
    trades, depth = load_l2(symbol, session)
    dts = depth.ts.to_numpy("int64")
    bid_px = depth.bid_px_00.to_numpy(float)
    ask_px = depth.ask_px_00.to_numpy(float)
    bid_sz = depth[[f"bid_sz_{i:02d}" for i in range(10)]].to_numpy(float)

    sm = VRStateMachine(cfg)
    events: list[dict] = []
    di = 0
    n_depth = len(dts)

    for ts, px, sz, agg in zip(trades.ts.to_numpy("int64"), trades.price.to_numpy(float),
                               trades["size"].to_numpy(float), trades.aggressor.to_numpy(int)):
        while di + 1 < n_depth and dts[di + 1] <= ts:
            di += 1
        sm.on_book(int(ts), bid_px[di], list(bid_sz[di]), ask_px[di])
        if sm.on_trade(int(ts), float(px), float(sz), int(agg)) is VRState.ARMED:
            events.append({"session": session, "ts": int(ts), **sm.zone})
            sm.reset()          # one structure per detection; look for the next
    return events


def forward_ticks(depth: pd.DataFrame, ts_ns: int, horizon_s: float) -> float:
    """Mid move `horizon_s` after ts_ns, in ticks. The outcome measure."""
    dts = depth.ts.to_numpy("int64")
    mid = (depth.bid_px_00.to_numpy(float) + depth.ask_px_00.to_numpy(float)) / 2.0
    i0 = min(np.searchsorted(dts, ts_ns, "right") - 1, len(dts) - 1)
    i1 = min(np.searchsorted(dts, ts_ns + int(horizon_s * NS), "right") - 1, len(dts) - 1)
    return float((mid[max(i1, 0)] - mid[max(i0, 0)]) / TICK)


def forward_bps(depth: pd.DataFrame, ts_ns: int, horizon_s: float) -> float:
    """Mid move `horizon_s` after ts_ns, in BASIS POINTS.

    Required for any CROSS-INSTRUMENT comparison. A tick is 2.64 bps on ES
    (~7582) but 0.82 bps on NQ (~24320), so raw tick counts make NQ's outcome
    distribution look ~3x wider than it is and can flip the sign of a mean.
    """
    dts = depth.ts.to_numpy("int64")
    mid = (depth.bid_px_00.to_numpy(float) + depth.ask_px_00.to_numpy(float)) / 2.0
    i0 = min(np.searchsorted(dts, ts_ns, "right") - 1, len(dts) - 1)
    i1 = min(np.searchsorted(dts, ts_ns + int(horizon_s * NS), "right") - 1, len(dts) - 1)
    a, b = mid[max(i0, 0)], mid[max(i1, 0)]
    return float((b - a) / a * 1e4) if a > 0 else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline VR structure detector")
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--sessions", nargs="+", default=EXPLORATORY)
    ap.add_argument("--horizon", type=float, default=30.0, help="forward window, seconds")
    ap.add_argument("--cons-max-ticks", type=int, default=None,
                    help="override; tick thresholds are INSTRUMENT-RELATIVE and must be "
                         "scaled by price level to compare ES against NQ")
    ap.add_argument("--thin-min-ticks", type=int, default=None, help="override")
    ap.add_argument("--trap-levels", type=int, default=None, help="override")
    a = ap.parse_args(argv)

    over = {}
    if a.cons_max_ticks is not None:
        over["cons_max_ticks"] = a.cons_max_ticks
    if a.thin_min_ticks is not None:
        over["thin_min_ticks"] = a.thin_min_ticks
    if a.trap_levels is not None:
        over["trap_levels"] = a.trap_levels
    cfg = VRConfig(**over)
    if over:
        print(f"  threshold overrides: {over}")
    all_events, fwd = [], []
    for s in a.sessions:
        ev = detect_session(a.symbol, s, cfg)
        _, depth = load_l2(a.symbol, s)
        for e in ev:
            fwd.append(forward_ticks(depth, e["ts"], a.horizon))
        all_events.extend(ev)
        print(f"  {s}: {len(ev)} structures", flush=True)

    n = len(all_events)
    print("\n" + "=" * 64)
    print(f"VR STRUCTURES — {a.symbol}, {len(a.sessions)} sessions")
    print("=" * 64)
    print(f"  total structures : {n}")
    f = np.array(fwd) if n else np.array([])
    if n:
        print(f"  fwd {a.horizon:.0f}s (ticks): mean {f.mean():+.3f}  median {np.median(f):+.3f}  "
              f"%up {100*(f>0).mean():.1f}")
    print("-" * 64)
    print("  FALSIFIER 1 (rarity): < 100 structures over the 6 exploratory")
    print("  sessions means the setup is too rare to test on this data. STOP —")
    print("  do NOT tune thresholds down to manufacture events.")
    print(f"  -> {'FAIL (too rare)' if n < 100 else 'pass'}  [{n} structures]")
    print("  FALSIFIER 2 (direction): mean forward move <= 0 means the reversion")
    print("  direction is wrong. Close the track; do not spend the OOS.")
    if n:
        print(f"  -> {'FAIL (wrong direction)' if f.mean() <= 0 else 'pass'}  [{f.mean():+.3f} ticks]")
    else:
        print("  -> not evaluable (no structures)")
    print("=" * 64)
    return 0


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    d = pd.DataFrame({"ts": [0, NS, 2 * NS, 3 * NS],
                      "bid_px_00": [100.0, 100.5, 101.0, 101.5],
                      "ask_px_00": [100.25, 100.75, 101.25, 101.75]})
    check("forward_ticks measures a 2s move",
          abs(forward_ticks(d, 0, 2.0) - 4.0) < 1e-9)
    check("forward_ticks clamps past the end",
          abs(forward_ticks(d, 0, 99.0) - 6.0) < 1e-9)
    check("forward_ticks is 0 over no elapsed time",
          abs(forward_ticks(d, 0, 0.0)) < 1e-9)

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else main())
