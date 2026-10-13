"""vr_timeshift_control.py — does the PRECISE trigger moment carry information?

Two nulls, deliberately different in strength:

  RANDOM   entries at uniformly random times in the session. Tests "is this just
           long exposure to a market that rose?" Already rejected: 0/400 beat the
           real result, and random entries LOSE despite +130 ticks of drift.

  SHIFTED  the real entries, moved by a random +/-2..30 minutes. Preserves session,
           time-of-day and regime; destroys ONLY the signal-specific timing. This
           is the hard null, and the one that decides whether the FSM's precision
           is earning its keep.

Supersedes the first attempt, which shifted by scaling ROW INDICES on the
assumption that depth rows are uniformly spaced in time. They are not — rows
cluster during active periods, so an index shift is not a time shift. Here the
shift is applied to the TIMESTAMP and resolved with searchsorted.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import numpy as np

import vr_data as vd
from vr_core import TICK, VRConfig
from vr_detect import detect_session

TICK_USD, COMM_RT = 12.50, 5.00
NS = 1_000_000_000
SESSIONS = [s for s in vd.EXPLORATORY if s != "2026-07-10"]   # crossed-book exclusion
N_ITER = 2000
SHIFT_MIN_S, SHIFT_MAX_S = 120, 1800
OUT = vd.ROOT / "research" / "v-reversal" / "artifacts" / "runs" / "A4_controls"


def simulate(bid, ask, i0, room):
    """One long with a ratcheting trail; force-flat at session end."""
    entry = ask[i0]
    stop = entry - room
    hw = bid[i0]
    for i in range(i0 + 1, len(bid)):
        b = bid[i]
        if b > hw:
            hw = b
            stop = max(stop, hw - room)
        if b <= stop:
            return (stop - entry) * (TICK_USD / TICK) - COMM_RT
    return (bid[-1] - entry) * (TICK_USD / TICK) - COMM_RT


def main() -> int:
    rng = np.random.default_rng(20260804)
    cfg = VRConfig()
    series, real = {}, []
    for s in SESSIONS:
        _, d = vd.load_l2("ES", s)
        ts = d.ts.to_numpy("int64")
        series[s] = (ts, d.bid_px_00.to_numpy(float), d.ask_px_00.to_numpy(float))
        for e in detect_session("ES", s, cfg):
            room = 5.0 * max(e["cons_hi"] - e["cons_lo"], 4 * TICK)
            real.append((s, int(e["ts"]), room))

    def pnl_at(s, ts_ns, room):
        ts, bid, ask = series[s]
        i = int(np.clip(np.searchsorted(ts, ts_ns, "right") - 1, 0, len(ts) - 10))
        return simulate(bid, ask, i, room)

    actual = sum(pnl_at(s, t, r) for s, t, r in real)

    # --- SHIFTED null: real entries moved in TIME (searchsorted, not index scaling)
    shifted = []
    for _ in range(N_ITER):
        v = 0.0
        for s, t, room in real:
            ts, _, _ = series[s]
            off = int(rng.choice([-1, 1]) * rng.integers(SHIFT_MIN_S, SHIFT_MAX_S)) * NS
            tt = int(np.clip(t + off, ts[0], ts[-1]))
            v += pnl_at(s, tt, room)
        shifted.append(v)
    sh = np.array(shifted)

    # --- RANDOM null: uniformly random times, same count per session
    randoms = []
    for _ in range(N_ITER):
        v = 0.0
        for s, _, room in real:
            ts, _, _ = series[s]
            v += pnl_at(s, int(rng.integers(ts[0], ts[-1])), room)
        randoms.append(v)
    rd = np.array(randoms)

    def block(name, a):
        return dict(name=name, mean=float(a.mean()), median=float(np.median(a)),
                    p05=float(np.percentile(a, 5)), p95=float(np.percentile(a, 95)),
                    pct_profitable=float(100 * (a > 0).mean()),
                    p_value=float((a >= actual).mean()))

    res = [block("SHIFTED (+/-2-30 min)", sh), block("RANDOM (uniform)", rd)]
    L = ["=" * 88,
         "  VR CONTROLS — is the edge beta, a regime, or a precise moment?",
         f"  real entries n={len(real)}   actual P&L=${actual:,.2f}   "
         f"(trail 5.0x each trade's own zone width)",
         f"  {N_ITER} iterations per null.  Buy-and-hold same sessions: $1,625.00",
         "=" * 88,
         f"{'null':>24} {'mean $':>10} {'median $':>10} {'p05':>10} {'p95':>10} "
         f"{'%profit':>8} {'p-value':>8}",
         "-" * 88]
    for r in res:
        L.append(f"{r['name']:>24} {r['mean']:10.2f} {r['median']:10.2f} {r['p05']:10.2f} "
                 f"{r['p95']:10.2f} {r['pct_profitable']:7.1f}% {r['p_value']:8.3f}")
    L += ["=" * 88,
          "  RANDOM rejected  => the result is NOT simply long exposure to a rising market.",
          "  SHIFTED is the hard null: if nearby times are just as profitable, the FSM's",
          "  tick-level precision is not earning its keep and the signal is finding a",
          "  favourable REGIME rather than a favourable MOMENT.",
          f"  Power is very low at n={len(real)} real trades: this can reject, not confirm.",
          "=" * 88]
    txt = "\n".join(L)
    print(txt)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.txt").write_text(txt + "\n")
    (OUT / "stats.json").write_text(json.dumps(
        {"generated_utc": datetime.now(timezone.utc).isoformat(),
         "sessions": SESSIONS, "n_real_trades": len(real), "n_iter": N_ITER,
         "actual_usd": actual, "buy_and_hold_usd": 1625.00,
         "shift_range_s": [SHIFT_MIN_S, SHIFT_MAX_S], "nulls": res}, indent=2))
    print(f"\nartifacts -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
