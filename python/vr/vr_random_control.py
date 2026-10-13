"""vr_random_control.py — does the V-reversal SIGNAL beat random entry?

THE ALTERNATIVE HYPOTHESIS. The 5.0x trail config made $1,562.50 over 5 sessions.
A single long held across the same 5 sessions would have made $1,625. If any long
position held ~40 minutes in this sample makes money, the entry signal is
decoration and the result is beta, not edge.

This runs the SAME exit logic (ratcheting trail, initial stop = entry - trail room)
from RANDOM entry times, many times over, and asks where VR's actual result falls
in that distribution. It is a randomisation test: the null is "entry timing carries
no information", and the p-value is the fraction of random runs that match or beat
the real one.

Simulated offline on the L2 bid/ask series rather than in the engine, so hundreds
of iterations are affordable. Exit mechanics mirror vr_strategy.VRStrategy:
  * enter at the ask
  * initial stop at entry - trail_room
  * ratchet the stop up behind the best bid, never down
  * force-close at session end (the survivorship fix)

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import numpy as np

import vr_data as vd

TICK, TICK_USD, COMM_RT = 0.25, 12.50, 5.00
SESSIONS = [s for s in vd.EXPLORATORY if s != "2026-07-10"]   # crossed-book exclusion
N_ITER = 400
OUT = vd.ROOT / "research" / "v-reversal" / "artifacts" / "runs" / "A4_random_control"


def load_series(sess):
    _, d = vd.load_l2("ES", sess)
    return (d.ts.to_numpy("int64"),
            d.bid_px_00.to_numpy(float),
            d.ask_px_00.to_numpy(float))


def simulate(bid, ask, i0, trail_room):
    """One long from index i0 with a ratcheting trail. Returns net USD."""
    entry = ask[i0]
    stop = entry - trail_room
    hw = bid[i0]
    for i in range(i0 + 1, len(bid)):
        b = bid[i]
        if b > hw:                       # ratchet up, never down
            hw = b
            stop = max(stop, hw - trail_room)
        if b <= stop:                    # stopped out
            return (stop - entry) * (TICK_USD / TICK) - COMM_RT
    return (bid[-1] - entry) * (TICK_USD / TICK) - COMM_RT   # forced flat at session end


def main() -> int:
    rng = np.random.default_rng(20260804)
    series = {s: load_series(s) for s in SESSIONS}

    # Trail room matched to the real config: 5.0x the consolidation range.
    # Zone ranges measured on the actual detected structures were 2-5 ticks,
    # so 5.0x ~= 10-25 ticks. Use the midpoint; swept below for robustness.
    results = {}
    for trail_ticks in (10, 15, 20, 25):
        room = trail_ticks * TICK
        totals = []
        for _ in range(N_ITER):
            tot = 0.0
            for s in SESSIONS:                   # 1 random entry per session, matching VR's ~1/session
                ts, bid, ask = series[s]
                i0 = int(rng.integers(0, len(bid) - 10))
                tot += simulate(bid, ask, i0, room)
            totals.append(tot)
        a = np.array(totals)
        results[trail_ticks] = dict(
            mean=float(a.mean()), median=float(np.median(a)),
            p05=float(np.percentile(a, 5)), p95=float(np.percentile(a, 95)),
            pct_profitable=float(100 * (a > 0).mean()),
            beat_1562=float(100 * (a >= 1562.50).mean()))

    L = ["=" * 84,
         "  RANDOM-ENTRY CONTROL — does V-reversal entry timing beat chance?",
         f"  {N_ITER} iterations x {len(SESSIONS)} sessions, same trailing-stop exit logic",
         f"  VR actual (5.0x trail): $1,562.50 on 5 trades",
         f"  Buy-and-hold over the same sessions: $1,625.00",
         "=" * 84,
         f"{'trail':>7} {'mean $':>10} {'median $':>10} {'p05':>10} {'p95':>10} "
         f"{'% profit':>9} {'% >= VR':>9}   <- p-value",
         "-" * 84]
    for k, v in results.items():
        L.append(f"{k:5d}tk {v['mean']:10.2f} {v['median']:10.2f} {v['p05']:10.2f} "
                 f"{v['p95']:10.2f} {v['pct_profitable']:8.1f}% {v['beat_1562']:8.1f}%")
    L += ["=" * 84,
          "  READ: '% >= VR' is the fraction of RANDOM entry sets that matched or beat",
          "  the real strategy. If that number is large, the entry signal adds nothing",
          "  and the result is exposure to a rising market, not an edge.",
          "  With 5 real trades this test has very low power — it can reject the signal",
          "  but it cannot confirm it.",
          "=" * 84]
    txt = "\n".join(L)
    print(txt)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.txt").write_text(txt + "\n")
    (OUT / "stats.json").write_text(json.dumps(
        {"generated_utc": datetime.now(timezone.utc).isoformat(),
         "sessions": SESSIONS, "n_iter": N_ITER,
         "vr_actual_usd": 1562.50, "buy_and_hold_usd": 1625.00,
         "results": results}, indent=2))
    print(f"\nartifacts -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
