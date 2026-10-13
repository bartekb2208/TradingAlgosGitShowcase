"""vr_data.py — VR V1 data access.

VR shares MR V2's Nautilus data pipeline (front-month resolution, instrument
construction) and nothing else. The dependency is funnelled through this one
module so there is a single place to change if the pipeline moves.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent      # repository root
L2 = ROOT / "data" / "L2"
sys.path.insert(0, str(ROOT / "python" / "v2"))

from data_pipeline import (           # noqa: E402
    build_instrument, find_mbo_file, front_month_contract,
)

# Already burned setting MR V2's thresholds; safe to calibrate VR on.
EXPLORATORY = ["2026-07-06", "2026-07-07", "2026-07-08",
               "2026-07-09", "2026-07-10", "2026-07-13"]
# Juneteenth half session: 20,715 trades vs a 260k-775k norm, 13:00 ET close.
EXCLUDED = ["2026-06-19"]
# 2026-07-10: 82.8% of L3 book updates crossed (ask <= bid) -- book unreconstructable.
# Objective data-quality exclusion (>5% crossed), NOT a P&L exclusion.
EXCLUDED_BOOK = ["2026-07-10"]

# --- DATA PLAN, declared 2026-08-04 ------------------------------------------
# The 27 sealed ES sessions are split CHRONOLOGICALLY. Time-ordered so the test
# set is the FUTURE relative to development, not a random interleave.
#   DEV  (18): released for building/tuning Tier 1+2+3.
#   OOS  (10): sealed until the fully completed algorithm is frozen. Read ONCE.
# Loosening the detector is already measured as a dead end for sample size, so
# DEV is the only remaining source of structures before the OOS read.
DEV = ['2026-06-08', '2026-06-09', '2026-06-10', '2026-06-11', '2026-06-12', '2026-06-15', '2026-06-16', '2026-06-17', '2026-06-18', '2026-06-22', '2026-06-23', '2026-06-24', '2026-06-25', '2026-06-26', '2026-06-29', '2026-06-30', '2026-07-01', '2026-07-02']
OOS = ['2026-07-03', '2026-07-14', '2026-07-15', '2026-07-16', '2026-07-17', '2026-07-20', '2026-07-21', '2026-07-22', '2026-07-23']

__all__ = ["build_instrument", "find_mbo_file", "front_month_contract",
           "load_l2", "EXPLORATORY", "EXCLUDED", "EXCLUDED_BOOK", "DEV", "OOS",
           "L2", "ROOT"]

# Sessions usable for development right now: exploratory (already burned) + DEV.
TRAINABLE = [s for s in EXPLORATORY if s not in EXCLUDED_BOOK] + DEV


def load_l2(symbol: str, session: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(trades, depth) for one session, both sorted by ts.

    trades: ts, price, size, aggressor(+1 buy / -1 sell)
    depth : ts, bid_px_00..09, bid_sz_00..09, ask_px_00..09, ask_sz_00..09
    """
    t = pd.read_parquet(L2 / f"{symbol}_{session}_trades.parquet").sort_values("ts")
    d = pd.read_parquet(L2 / f"{symbol}_{session}_depth10.parquet").sort_values("ts")
    return t.reset_index(drop=True), d.reset_index(drop=True)


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    inst = build_instrument("ESM6")
    check("ES multiplier is 50 (not the test-kit's 1)", int(inst.multiplier) == 50)
    check("ES tick value is $12.50",
          abs(float(inst.price_increment) * int(inst.multiplier) - 12.50) < 1e-9)
    check("6 exploratory sessions declared", len(EXPLORATORY) == 6)
    check("Juneteenth excluded", "2026-06-19" in EXCLUDED)
    check("exploratory and excluded do not overlap",
          not set(EXPLORATORY) & set(EXCLUDED))

    t, d = load_l2("ES", EXPLORATORY[0])
    check("trades tape non-empty", len(t) > 100_000)
    check("depth has 10 bid levels",
          all(f"bid_sz_{i:02d}" in d.columns for i in range(10)))
    check("trades sorted by ts", bool(t.ts.is_monotonic_increasing))
    check("depth sorted by ts", bool(d.ts.is_monotonic_increasing))
    check("aggressor is +/-1 only", set(t.aggressor.unique()) <= {-1, 1})

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else 0)
