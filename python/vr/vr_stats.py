"""vr_stats.py — full VR V1 statistics -> research/v-reversal/artifacts/.

Two blocks:
  * DETECTION  — structures, rates and forward moves for ES (exploratory) and NQ
                 (the untouched cross-instrument control). No P&L claim.
  * EXECUTION  — engine round trips with real commission: PF, expectancy, payoff,
                 drawdown, Sharpe, Calmar. Only this block makes a P&L claim.

Falsifier verdicts (F1-F5, see the design spec) are computed and written out, so
the stop conditions live in the artifact rather than in someone's memory.

Writes incrementally: detection lands before the slow engine leg starts.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import vr_data as vd
from vr_core import VRConfig
from vr_detect import detect_session, forward_bps, forward_ticks
from vr_strategy import VR_ENGINE_VERSION, VRStrategy

BASE = vd.ROOT / "research" / "v-reversal" / "artifacts"
OUT = BASE            # overridden by --tag
TICK_USD_ES = 12.50
TICK_USD_NQ = 5.00

ES_EXPLORATORY = vd.EXPLORATORY
NQ_SESSIONS = ["2026-04-06", "2026-04-07", "2026-04-08", "2026-04-09", "2026-04-10",
               "2026-04-13", "2026-04-14", "2026-04-15", "2026-04-16", "2026-04-17",
               "2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23", "2026-04-24",
               "2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-01",
               "2026-05-04", "2026-05-05", "2026-05-06"]
# Tick thresholds are INSTRUMENT-RELATIVE: 8 ticks is 2.64 bps on ES (~7582) but
# 0.82 bps on NQ (~24320). Scale by ~3.2x or the NQ count is a unit artifact.
NQ_OVERRIDES = dict(cons_max_ticks=26, thin_min_ticks=26, trap_levels=16)

SEALED_ES = 28
SEALED_NQ = 23


def detection_block(symbol: str, sessions: list[str], cfg: VRConfig, horizon: float = 30.0) -> dict:
    events, fwd, fwd_bps = [], [], []
    per_session = {}
    for s in sessions:
        ev = detect_session(symbol, s, cfg)
        _, depth = vd.load_l2(symbol, s)
        f = [forward_ticks(depth, e["ts"], horizon) for e in ev]
        fwd_bps.extend(forward_bps(depth, e["ts"], horizon) for e in ev)
        per_session[s] = {"structures": len(ev), "mean_fwd": float(np.mean(f)) if f else None}
        events.extend(ev)
        fwd.extend(f)
        print(f"    {symbol} {s}: {len(ev)}", flush=True)
    n = len(events)
    a = np.array(fwd) if n else np.array([])
    b = np.array(fwd_bps) if n else np.array([])
    return {
        "fwd_mean_bps": float(b.mean()) if n else None,
        "fwd_median_bps": float(np.median(b)) if n else None,
        "fwd_p10_bps": float(np.percentile(b, 10)) if n else None,
        "fwd_p90_bps": float(np.percentile(b, 90)) if n else None,
        "symbol": symbol, "sessions": len(sessions), "structures": n,
        "rate_per_session": n / len(sessions) if sessions else 0.0,
        "fwd_mean_ticks": float(a.mean()) if n else None,
        "fwd_median_ticks": float(np.median(a)) if n else None,
        "fwd_pct_up": float(100 * (a > 0).mean()) if n else None,
        "fwd_p10": float(np.percentile(a, 10)) if n else None,
        "fwd_p90": float(np.percentile(a, 90)) if n else None,
        "per_session": per_session,
    }


def execution_stats(trades: list[dict], n_sessions: int, tick_usd: float) -> dict:
    if not trades:
        return {"round_trips": 0,
                "note": "NO TRADES. PF, Sharpe, Calmar and drawdown are UNDEFINED, "
                        "not zero — there is no return series to compute them from."}
    pnl = np.array([t["realized_pnl_usd"] for t in trades], float)
    comm = np.array([t["commission_usd"] for t in trades], float)
    gross = pnl + comm
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    maxdd = float(-(eq - peak).min())
    sd = float(pnl.std(ddof=1)) if len(pnl) > 1 else 0.0
    pts = float(pnl.mean() / sd) if sd > 0 else float("nan")
    tpy = len(trades) / n_sessions * 252
    hold = [t["hold_ms"] for t in trades if t.get("hold_ms") is not None]
    return {
        "round_trips": len(trades),
        "win_rate_pct": float(100 * (pnl > 0).mean()),
        "gross_usd": float(gross.sum()),
        "commission_usd": float(comm.sum()),
        "net_usd": float(pnl.sum()),
        "gross_ticks_per_trade": float(gross.sum() / tick_usd / len(trades)),
        "expectancy_usd": float(pnl.mean()),
        "avg_win_usd": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_usd": float(losses.mean()) if len(losses) else 0.0,
        "payoff_ratio": float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else None,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) else float("inf"),
        "max_drawdown_usd": maxdd,
        "calmar_like": float(pnl.sum() / maxdd) if maxdd > 0 else None,
        "per_trade_sharpe": pts,
        "annualised_sharpe_EST": float(pts * np.sqrt(tpy)) if sd > 0 else None,
        "trades_per_year_EST": float(tpy),
        "median_hold_ms": float(np.median(hold)) if hold else None,
    }


def run_engine(sessions: list[str], cfg: VRConfig) -> list[dict]:
    """Engine leg. Imported lazily so the detection block needs no nautilus."""
    from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.backtest.models import PerContractFeeModel
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType, BookType, OmsType
    from nautilus_trader.model.identifiers import TraderId, Venue
    from nautilus_trader.model.objects import Money
    sys.path.insert(0, str(vd.ROOT / "python" / "v2"))
    from v2_core import load_trade_ticks

    out, engine = [], None
    for s in sessions:
        t0 = time.time()
        loader = DatabentoDataLoader()
        alld = loader.from_dbn_file(str(vd.find_mbo_file(s)), price_precision=2,
                                    as_legacy_cython=True)
        inst = vd.build_instrument(vd.front_month_contract("ES", s, alld))
        deltas = [d for d in alld if str(d.instrument_id) == str(inst.id)]
        del alld
        trades = load_trade_ticks(str(vd.L2 / f"ES_{s}_trades.parquet"), inst.id)
        if engine is None:
            engine = BacktestEngine(config=BacktestEngineConfig(
                trader_id=TraderId("VR-STATS"), logging=LoggingConfig(log_level="ERROR")))
            engine.add_venue(venue=Venue("GLBX"), oms_type=OmsType.NETTING,
                             account_type=AccountType.MARGIN, base_currency=USD,
                             starting_balances=[Money(1_000_000, USD)],
                             book_type=BookType.L3_MBO, trade_execution=True,
                             queue_position=True,
                             fee_model=PerContractFeeModel(Money(cfg.commission_per_side, USD)))
            engine.add_instrument(inst)
        else:
            engine.reset(); engine.clear_data(); engine.clear_strategies()
            try:
                engine.add_instrument(inst)
            except Exception:
                pass
        engine.add_data(deltas); engine.add_data(trades)
        st = VRStrategy(inst, cfg)
        engine.add_strategy(st); engine.run()
        for t in st.closed_positions:
            out.append(dict(t, session=s))
        print(f"    engine {s}: armed {st.n_armed} entries {st.n_entries} "
              f"fills {st.n_fills} | skipped: cross {st.n_skipped_would_cross} "
              f"widestop {st.n_skipped_wide_stop} badstop {st.n_skipped_bad_stop} "
              f"| trailmoves {st.n_trail_moves} crossed {st.n_crossed_book:,} ({time.time()-t0:.0f}s)", flush=True)
    if engine is not None:
        engine.dispose()
    return out


def falsifiers(es: dict, nq: dict, ex: dict) -> dict:
    r_es, r_nq = es["rate_per_session"], nq["rate_per_session"]
    projected = r_es * SEALED_ES + r_nq * SEALED_NQ
    ratio = (max(r_es, r_nq) / min(r_es, r_nq)) if min(r_es, r_nq) > 0 else float("inf")
    same_sign = (es["fwd_mean_bps"] is not None and nq["fwd_mean_bps"] is not None
                 and (es["fwd_mean_bps"] > 0) == (nq["fwd_mean_bps"] > 0))
    gtt = ex.get("gross_ticks_per_trade")
    return {
        "F1_sample_adequacy": {
            "rule": "rate x sealed sessions >= 100",
            "projected": round(projected, 1),
            "detail": f"ES {SEALED_ES}x{r_es:.2f} + NQ {SEALED_NQ}x{r_nq:.2f}",
            "verdict": "PASS" if projected >= 100 else "FAIL",
        },
        "F2_direction_ES": {
            "rule": "mean forward move > 0 on ES exploratory",
            "value": es["fwd_mean_ticks"],
            "verdict": "PASS" if (es["fwd_mean_ticks"] or 0) > 0 else "FAIL",
        },
        "F3_rate_stability": {
            "rule": "ES/NQ structure-rate ratio within 3x (price-scaled thresholds)",
            "value": round(ratio, 2),
            "verdict": "PASS" if ratio <= 3.0 else "FAIL",
        },
        "F4_direction_cross_instrument": {
            "rule": "ES and NQ mean forward move share a sign (measured in BPS — "
                    "raw ticks are not comparable across price levels)",
            "es_mean_bps": es["fwd_mean_bps"], "nq_mean_bps": nq["fwd_mean_bps"],
            "es_median_bps": es["fwd_median_bps"], "nq_median_bps": nq["fwd_median_bps"],
            "es_mean": es["fwd_mean_ticks"], "nq_mean": nq["fwd_mean_ticks"],
            "es_median": es["fwd_median_ticks"], "nq_median": nq["fwd_median_ticks"],
            "es_pct_up": es["fwd_pct_up"], "nq_pct_up": nq["fwd_pct_up"],
            "verdict": "PASS" if same_sign else "FAIL",
            "note": "Means diverge on tail events at this n; medians and hit-rates "
                    "are the more robust comparison and are reported alongside.",
        },
        "F5_cost_floor": {
            "rule": "gross > +0.40 ticks/trade (commission $5.00 RT / $12.50 per tick)",
            "value": gtt,
            "verdict": ("PASS" if (gtt is not None and gtt > 0.40)
                        else ("FAIL" if gtt is not None else "NOT EVALUABLE (no trades)")),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="VR V1 statistics")
    ap.add_argument("--engine", action="store_true", help="also run the slow engine leg")
    ap.add_argument("--engine-sessions", nargs="+", default=ES_EXPLORATORY)
    ap.add_argument("--tag", default=None,
                    help="write to research/v-reversal/artifacts/runs/<tag>/ instead of "
                         "the root, so earlier runs are never overwritten")
    ap.add_argument("--note", default="", help="one-line description stored with the run")
    a = ap.parse_args(argv)

    global OUT
    OUT = (BASE / "runs" / a.tag) if a.tag else BASE
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = VRConfig()

    print("DETECTION — ES exploratory")
    es = detection_block("ES", ES_EXPLORATORY, cfg)
    print("DETECTION — NQ control (price-scaled thresholds)")
    nq = detection_block("NQ", NQ_SESSIONS, VRConfig(**NQ_OVERRIDES))

    payload = {
        "engine_version": VR_ENGINE_VERSION,
        "run_tag": a.tag or "root",
        "note": a.note,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "config_es": {k: v for k, v in cfg.__dict__.items()},
        "nq_threshold_overrides": NQ_OVERRIDES,
        "detection": {"ES": es, "NQ": nq},
        "execution": {"round_trips": 0, "note": "engine leg not run"},
    }
    payload["falsifiers"] = falsifiers(es, nq, payload["execution"])
    (OUT / "stats.json").write_text(json.dumps(payload, indent=2, default=str))
    (OUT / "report.txt").write_text(_format(payload))
    print(_format(payload))

    if a.engine:
        print("\nEXECUTION — engine leg (slow)")
        trades = run_engine(a.engine_sessions, cfg)
        payload["execution"] = execution_stats(trades, len(a.engine_sessions), TICK_USD_ES)
        payload["falsifiers"] = falsifiers(es, nq, payload["execution"])
        if trades:
            with (OUT / "trades.csv").open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(trades[0]))
                w.writeheader(); w.writerows(trades)
        (OUT / "stats.json").write_text(json.dumps(payload, indent=2, default=str))
        (OUT / "report.txt").write_text(_format(payload))
        print(_format(payload))

    print(f"\nartifacts -> {OUT}")
    return 0


def _format(p: dict) -> str:
    L = ["=" * 74, f"  VR V1 STATISTICS — engine v{p['engine_version']}   "
         f"[run: {p.get('run_tag','root')}]",
         f"  {p.get('note','')}" if p.get("note") else "",
         f"  generated {p['generated_utc']}", "=" * 74, "", "DETECTION"]
    for sym in ("ES", "NQ"):
        d = p["detection"][sym]
        L.append(f"  {sym}: {d['structures']} structures over {d['sessions']} sessions "
                 f"= {d['rate_per_session']:.2f}/session")
        if d["structures"]:
            L.append(f"      fwd30s ticks: mean {d['fwd_mean_ticks']:+.3f}  "
                     f"median {d['fwd_median_ticks']:+.3f}  %up {d['fwd_pct_up']:.1f}  "
                     f"p10 {d['fwd_p10']:+.1f}  p90 {d['fwd_p90']:+.1f}")
            L.append(f"      fwd30s BPS  : mean {d['fwd_mean_bps']:+.3f}  "
                     f"median {d['fwd_median_bps']:+.3f}  "
                     f"p10 {d['fwd_p10_bps']:+.2f}  p90 {d['fwd_p90_bps']:+.2f}   "
                     f"<- the cross-instrument comparison")
    L += ["", "EXECUTION"]
    ex = p["execution"]
    if ex.get("round_trips"):
        for k, v in ex.items():
            L.append(f"  {k:24s}: {v:,.4f}" if isinstance(v, float) else f"  {k:24s}: {v}")
    else:
        L.append(f"  {ex.get('note', 'no trades')}")
    L += ["", "FALSIFIERS"]
    for k, v in p["falsifiers"].items():
        L.append(f"  [{v['verdict']:>18s}]  {k}")
        L.append(f"                        {v['rule']}")
        for kk in ("projected", "value", "detail", "es_mean_bps", "nq_mean_bps",
                   "es_median_bps", "nq_median_bps", "es_mean", "nq_mean",
                   "es_median", "nq_median", "es_pct_up", "nq_pct_up", "note"):
            if kk in v and v[kk] is not None:
                L.append(f"                        {kk}: {v[kk]}")
    L += ["", "=" * 74,
          "  CAVEATS",
          "  * annualised_sharpe_EST scales a per-trade Sharpe by sqrt(trades/yr)",
          "    extrapolated from a handful of sessions. Order of magnitude only.",
          "  * calmar_like = net / max drawdown over the sampled sessions, NOT an",
          "    annual return / max DD over years. Not comparable to published figures.",
          "  * NQ tick thresholds are price-scaled (8 -> 26 ticks). Raw tick counts",
          "    would make NQ look rarer purely as a unit artifact.",
          "  * NQ has now been READ as the F3/F4 detector control. It can no longer",
          "    serve as a clean P&L out-of-sample set.",
          "=" * 74]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
