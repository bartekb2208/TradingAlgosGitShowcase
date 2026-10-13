"""vr_stage6_measure.py — does the DOC'S trade exist after our ARMED events?

THE QUESTION. VR V1 enters at ARMED, which by construction sits at/below cons_lo
(measured 0 to -6 ticks on every exploratory structure). The source doc does NOT
trade there. Its sequence continues (paraphrased): price returns to the zone and probes
its far side, breaks out of the consolidation with only a shallow pullback,
and a high-volume candle then confirms the break.

Design spec research/v-reversal/DESIGN.md lists this as STAGE 6, a separate
gate after the V retrace:  `ARMED --entry rule--> IN_TRADE`. It was never built:
vr_strategy._on_armed() submits at the arming instant and calls sm.reset().

So we tested the V-retrace entry (breakeven at best, n=17) and never tested the
doc's actual trade. This script measures — it does NOT build the strategy.

MEASUREMENT, NOT BACKTEST. Offline over the L2 tape, no engine, no OOS.
For each ARMED event we walk the tape forward and ask:
    1. does price RECLAIM cons_lo?
    2. does it TEST cons_hi?
    3. does it BREAK cons_hi?
    4. or does it INVALIDATE first (new low below v_low)?
and, at the break, what the doc's own risk geometry would have been — the NARROW
stop at cons_lo, which is illegal for us today because our entry sits below it.

DECISION RULE, declared before running:
  * few breaks (<40%) OR forward move from break <= 0  -> the doc's trade is not
    in this data either. Close VR.
  * many breaks with a tight stop and positive expectancy -> park it with a
    documented reason to return when more data is affordable. Do NOT build on 17.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

import numpy as np

import vr_data as vd
from vr_core import NS, TICK, VRConfig
from vr_detect import detect_session, forward_ticks

TICK_USD, COMM_RT_USD = 12.50, 5.00
COMM_RT_TK = COMM_RT_USD / TICK_USD           # 0.40 ticks round trip

SESSIONS = vd.TRAINABLE                        # 23: exploratory (minus 07-10) + DEV
STAGE6_TTL_S = 1800.0                          # abandon a structure that never breaks
BREAK_TICKS = 1                                # "break" = strictly above cons_hi
TARGET_BUFFER_TK = 2                           # SPEC (paraphrased): a few ticks short of the target
HORIZONS = (30.0, 120.0, 300.0)
OUT = vd.ROOT / "research" / "v-reversal" / "artifacts" / "runs" / "D_stage6_measurement"


def _first(mask: np.ndarray) -> int | None:
    """Index of the first True, or None. argmax alone can't tell 'all False'."""
    return int(np.argmax(mask)) if mask.any() else None


def walk_stage6(px: np.ndarray, ts: np.ndarray, i0: int, zone: dict) -> dict:
    """Trace the doc's post-V sequence forward from the ARMED index."""
    ttl = int(np.searchsorted(ts, ts[i0] + int(STAGE6_TTL_S * NS), "right"))
    seg, segt = px[i0:ttl], ts[i0:ttl]
    if len(seg) < 2:
        return {"outcome": "no_data"}

    cons_lo, cons_hi, v_low = zone["cons_lo"], zone["cons_hi"], zone["v_low"]
    i_dead = _first(seg < v_low)                       # new low -> structure failed
    i_recl = _first(seg >= cons_lo)
    i_test = _first(seg >= cons_hi)
    i_brk = _first(seg >= cons_hi + BREAK_TICKS * TICK)

    def before_death(i):
        return i is not None and (i_dead is None or i < i_dead)

    out = {
        "reclaimed": before_death(i_recl),
        "tested_top": before_death(i_test),
        "broke": before_death(i_brk),
        "invalidated": i_dead is not None,
        "secs_to_reclaim": (segt[i_recl] - segt[0]) / NS if before_death(i_recl) else None,
        "secs_to_break": (segt[i_brk] - segt[0]) / NS if before_death(i_brk) else None,
    }
    out["outcome"] = ("broke" if out["broke"] else
                      "tested_no_break" if out["tested_top"] else
                      "reclaimed_only" if out["reclaimed"] else
                      "invalidated" if out["invalidated"] else "stalled")
    if out["broke"]:
        out["break_idx"] = i0 + i_brk
        out["break_px"] = float(seg[i_brk])
    return out


def simulate_from_break(px: np.ndarray, ts: np.ndarray, i_brk: int,
                        entry: float, stop: float, target: float) -> dict:
    """Doc's trade: enter on the break, NARROW stop at cons_lo, target thin_origin.

    Run to session end and record what happened. Anything still open at the end is
    marked to the last trade and reported -- the survivorship discipline the trail
    sweep needed (closed-trade count fell 5->2 as the trail widened, purely from
    unresolved positions vanishing from the stats).
    """
    seg, segt = px[i_brk + 1:], ts[i_brk + 1:]
    if len(seg) < 2:
        return {"exit": "no_data", "pnl_tk": 0.0}
    i_stop, i_tgt = _first(seg <= stop), _first(seg >= target)
    if i_stop is not None and (i_tgt is None or i_stop <= i_tgt):
        exit_px, i_x, reason = stop, i_stop, "stop"
    elif i_tgt is not None:
        exit_px, i_x, reason = target, i_tgt, "target"
    else:
        exit_px, i_x, reason = float(seg[-1]), len(seg) - 1, "session_end"
    return {"exit": reason,
            "pnl_tk": (exit_px - entry) / TICK - COMM_RT_TK,
            "hold_s": float((segt[i_x] - segt[0]) / NS),
            "mfe_tk": float((seg[:i_x + 1].max() - entry) / TICK),
            "mae_tk": float((seg[:i_x + 1].min() - entry) / TICK)}


def main() -> int:
    cfg = VRConfig()
    rows: list[dict] = []
    for s in SESSIONS:
        t0 = time.time()
        ev = detect_session("ES", s, cfg)
        if not ev:
            print(f"  {s}: 0 structures ({time.time()-t0:.0f}s)", flush=True)
            continue
        trades, depth = vd.load_l2("ES", s)
        px = trades.price.to_numpy(float)
        ts = trades.ts.to_numpy("int64")
        for e in ev:
            i0 = int(np.searchsorted(ts, e["ts"], "right")) - 1
            r = {"session": s, "ts": e["ts"],
                 "cons_lo": e["cons_lo"], "cons_hi": e["cons_hi"],
                 "v_low": e["v_low"], "thin_origin": e["thin_origin"],
                 "armed_px": e["armed_px"],
                 "zone_tk": (e["cons_hi"] - e["cons_lo"]) / TICK,
                 "wide_stop_tk": (e["armed_px"] - e["v_low"]) / TICK}
            for h in HORIZONS:
                r[f"armed_fwd_{int(h)}s_tk"] = forward_ticks(depth, e["ts"], h)
            r.update(walk_stage6(px, ts, max(i0, 0), e))
            if r.get("broke"):
                entry = r["break_px"]
                stop, target = e["cons_lo"], e["thin_origin"] - TARGET_BUFFER_TK * TICK
                r["narrow_stop_tk"] = (entry - stop) / TICK
                r["reward_tk"] = (target - entry) / TICK
                r["rr"] = (r["reward_tk"] / r["narrow_stop_tk"]
                           if r["narrow_stop_tk"] > 0 else 0.0)
                for h in HORIZONS:
                    r[f"break_fwd_{int(h)}s_tk"] = forward_ticks(
                        depth, int(ts[r["break_idx"]]), h)
                r.update(simulate_from_break(px, ts, r["break_idx"], entry, stop, target))
            rows.append(r)
        print(f"  {s}: {len(ev)} structures ({time.time()-t0:.0f}s)", flush=True)

    n = len(rows)
    brk = [r for r in rows if r.get("broke")]
    # GEOMETRY CHECK — added after the first run produced a nonsense P&L block.
    #
    # The doc's structure REQUIRES the consolidation to sit BELOW the origin of the
    # thin move (one fast candle carried price down into the zone), and the
    # target is that origin. If cons_hi >= thin_origin the target sits at or under
    # the breakout price, the trade is short-circuited at entry, and the doc's
    # target #2 simply does not exist for that structure.
    #
    # Measured on run 1: 10 of 12 breaks (83%) had target < entry, every one exiting
    # "target" at hold_s=0 for a loss. That is not a market result, it is
    # `_first(seg >= target)` firing on the first tick. Those rows are excluded here
    # and reported separately, because the violation is itself the finding.
    geom_ok = [r for r in brk if r.get("reward_tk", 0) > 0]
    geom_bad = [r for r in brk if r.get("reward_tk", 0) <= 0]
    n_geom_all = sum(1 for r in rows if r["thin_origin"] > r["cons_hi"])
    tradeable = [r for r in geom_ok
                 if r.get("narrow_stop_tk", 0) > 0 and r.get("exit") != "no_data"]

    def agg(vals):
        a = np.array([v for v in vals if v is not None], float)
        return (dict(n=len(a), mean=float(a.mean()), median=float(np.median(a)),
                     p25=float(np.percentile(a, 25)), p75=float(np.percentile(a, 75)))
                if len(a) else dict(n=0))

    counts = {k: sum(1 for r in rows if r.get("outcome") == k) for k in
              ("broke", "tested_no_break", "reclaimed_only", "invalidated", "stalled", "no_data")}

    L = ["=" * 94,
         "  VR STAGE-6 MEASUREMENT — does the DOC'S trade exist after our ARMED events?",
         f"  {len(SESSIONS)} sessions (exploratory minus 07-10 + DEV). OOS UNTOUCHED.",
         f"  TTL {STAGE6_TTL_S:.0f}s   break = cons_hi + {BREAK_TICKS} tick   "
         f"invalidation = new low < v_low",
         "=" * 94,
         f"  ARMED events: {n}    ({n/len(SESSIONS):.2f}/session)", "",
         "  WHAT HAPPENS AFTER ARMED:"]
    for k, v in counts.items():
        if v:
            L.append(f"    {k:>16} : {v:3d}  ({100*v/max(n,1):5.1f}%)")

    L += ["", "-" * 94,
          "  STRUCTURE GEOMETRY — does the zone sit BELOW the origin of the thin move?",
          "  The doc's setup requires it: the gap IS the room the trade travels into,",
          "  and thin_origin IS the target. cons_hi >= thin_origin => no trade exists.",
          "-" * 94,
          f"  ARMED events with thin_origin > cons_hi (doc geometry intact) : "
          f"{n_geom_all:3d} / {n} ({100*n_geom_all/max(n,1):5.1f}%)",
          f"  of the {len(brk)} that BROKE — geometry intact : {len(geom_ok):3d}   "
          f"VIOLATED (target under entry) : {len(geom_bad):3d}"]

    L += ["", "-" * 94, "  RISK GEOMETRY — the doc's NARROW stop vs our forced WIDE stop", "-" * 94]
    w, nar = agg([r["wide_stop_tk"] for r in rows]), agg([r.get("narrow_stop_tk") for r in brk])
    L.append(f"  wide stop  (armed -> v_low)   n={w['n']:3d}  "
             f"median {w.get('median',0):6.1f} tk  mean {w.get('mean',0):6.1f} tk "
             f"= ${w.get('median',0)*TICK_USD:8.2f}")
    if nar["n"]:
        L.append(f"  NARROW stop (break -> cons_lo) n={nar['n']:3d}  "
                 f"median {nar['median']:6.1f} tk  mean {nar['mean']:6.1f} tk "
                 f"= ${nar['median']*TICK_USD:8.2f}")
        rr = agg([r.get("rr") for r in geom_ok])
        if rr["n"]:
            L.append(f"  reward:risk (geometry-intact)  n={rr['n']:3d}  "
                     f"median {rr['median']:6.2f}     mean {rr['mean']:6.2f}")

    L += ["", "-" * 94, "  FORWARD MOVE: entering at ARMED (what we tested) vs at the BREAK", "-" * 94,
          f"{'horizon':>10} {'ARMED mean':>12} {'ARMED med':>11} {'ARMED %up':>10} "
          f"{'BREAK mean':>12} {'BREAK med':>11} {'BREAK %up':>10}"]
    for h in HORIZONS:
        a = np.array([r[f"armed_fwd_{int(h)}s_tk"] for r in rows], float)
        b = np.array([r[f"break_fwd_{int(h)}s_tk"] for r in brk], float) if brk else np.array([])
        L.append(f"{int(h):9d}s {a.mean():12.3f} {np.median(a):11.3f} {100*(a>0).mean():9.1f}% "
                 + (f"{b.mean():12.3f} {np.median(b):11.3f} {100*(b>0).mean():9.1f}%"
                    if len(b) else f"{'-':>12} {'-':>11} {'-':>10}"))

    res_stats = {}
    if tradeable:
        p = np.array([r["pnl_tk"] for r in tradeable], float)
        wins, losses = p[p > 0], p[p < 0]
        eq = np.cumsum(p)
        dd = float(-(eq - np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]).min())
        pf = float(wins.sum() / -losses.sum()) if len(losses) else float("inf")
        sharpe = float(p.mean() / p.std(ddof=1) * np.sqrt(len(p))) if len(p) > 1 and p.std(ddof=1) > 0 else 0.0
        res_stats = dict(n=len(p), win_pct=float(100 * (p > 0).mean()),
                         mean_tk=float(p.mean()), median_tk=float(np.median(p)),
                         total_tk=float(p.sum()), total_usd=float(p.sum() * TICK_USD),
                         avg_win_tk=float(wins.mean()) if len(wins) else 0.0,
                         avg_loss_tk=float(losses.mean()) if len(losses) else 0.0,
                         pf=pf, sharpe_agg=sharpe, maxdd_tk=dd,
                         median_hold_s=float(np.median([r["hold_s"] for r in tradeable])),
                         exits={k: sum(1 for r in tradeable if r["exit"] == k)
                                for k in ("stop", "target", "session_end")})
        L += ["", "-" * 94,
              "  THE DOC'S TRADE, SIMULATED: enter on break, stop at cons_lo, target thin_origin",
              f"  GEOMETRY-INTACT STRUCTURES ONLY (n={len(tradeable)} of {n} armed events).",
              "  (net of $5.00 round-trip commission = 0.40 ticks)", "-" * 94,
              f"  trades {res_stats['n']}   win {res_stats['win_pct']:.1f}%   "
              f"PF {pf:.3f}   net {res_stats['total_tk']:+.1f} tk = "
              f"${res_stats['total_usd']:+,.2f}",
              f"  mean {res_stats['mean_tk']:+.3f} tk/trade   median {res_stats['median_tk']:+.3f}   "
              f"avgWin {res_stats['avg_win_tk']:+.2f}   avgLoss {res_stats['avg_loss_tk']:+.2f}",
              f"  maxDD {dd:.1f} tk   median hold {res_stats['median_hold_s']:.0f}s   "
              f"exits {res_stats['exits']}"]

    L += ["", "=" * 94, "  DECISION RULE (declared before the run):",
          "    <40% of ARMED events break, OR forward move from break <= 0",
          "      -> the doc's trade is not in this data either. CLOSE VR.",
          "    high break rate + tight stop + positive expectancy",
          "      -> PARK with a documented reason to return. Do NOT build a strategy on n<30.",
          "  This is a MEASUREMENT on already-burned sessions. The 9 OOS sessions are untouched.",
          "=" * 94]
    txt = "\n".join(L)
    print("\n" + txt)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.txt").write_text(txt + "\n")
    (OUT / "stats.json").write_text(json.dumps(
        {"generated_utc": datetime.now(timezone.utc).isoformat(),
         "sessions": SESSIONS, "n_armed": n, "outcome_counts": counts,
         "stage6_ttl_s": STAGE6_TTL_S, "break_ticks": BREAK_TICKS,
         "geometry_intact_all": n_geom_all,
         "geometry_intact_breaks": len(geom_ok),
         "geometry_violated_breaks": len(geom_bad),
         "wide_stop_tk": w, "narrow_stop_tk": nar, "doc_trade": res_stats,
         "rows": rows}, indent=2, default=str))
    print(f"\nartifacts -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
