"""fvt_stats.py — FVT V1 splits, metrics and falsifier verdicts.

Writes research/fvt-v1/artifacts/{stats.json,report.txt,trades.csv}. The falsifier stop
conditions are computed into the artifact so they live in the file rather than in
someone's memory.

TEST split runs by default. The VALIDATION split runs only under --validation and
is meant to be run exactly ONCE, after the Test config is frozen.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime, timezone

import numpy as np

import fvt_data as fd
from fvt_core import FVTConfig
from fvt_engine import TRADE_COLUMNS, arbitrate_trades, run_backtest

FVT_ENGINE_VERSION = "0.1.0"
OUT = fd.ROOT / "research" / "fvt-v1" / "artifacts"
BREAKEVEN_WIN_RATE = 100.0 / (1.0 + 1.5)      # 40.0% for a fixed 1.5R


def metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"round_trips": 0,
                "note": "NO TRADES. PF, win rate and drawdown are UNDEFINED, "
                        "not zero — there is no return series to compute them from."}
    pnl = np.array([t["net_usd"] for t in trades], float)
    comm = np.array([t["commission_usd"] for t in trades], float)
    gross = pnl + comm
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    sd = float(pnl.std(ddof=1)) if len(pnl) > 1 else 0.0
    return {
        "round_trips": len(trades),
        "win_rate_pct": float(100 * (pnl > 0).mean()),
        "gross_usd": float(gross.sum()),
        "commission_usd": float(comm.sum()),
        "net_usd": float(pnl.sum()),
        "expectancy_usd": float(pnl.mean()),
        "gross_expectancy_usd": float(gross.mean()),
        "avg_win_usd": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_usd": float(losses.mean()) if len(losses) else 0.0,
        "payoff_ratio": (float(wins.mean() / abs(losses.mean()))
                         if len(wins) and len(losses) else None),
        "profit_factor": (float(wins.sum() / -losses.sum())
                          if len(losses) else float("inf")),
        "max_drawdown_usd": float(-(eq - peak).min()),
        "per_trade_sharpe": float(pnl.mean() / sd) if sd > 0 else None,
        "ambiguous_pct": float(100 * np.mean([t["ambiguous_bar"] for t in trades])),
    }


def tag_breakdown(trades: list[dict], key: str) -> dict:
    out: dict = {}
    for t in trades:
        out.setdefault(t[key], []).append(t)
    return {k: metrics(v) for k, v in sorted(out.items(), key=lambda kv: str(kv[0]))}


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def falsifiers(es_test, es_val, nq_test, nq_val, phase_break, amb_rate, arb,
               nq_val_sessions: int, cfg: FVTConfig) -> dict:
    n = es_test.get("round_trips", 0)
    pf = es_test.get("profit_factor", 0.0)
    wr = es_test.get("win_rate_pct")
    ge = es_test.get("gross_expectancy_usd", 0.0)
    rt_comm = 2 * cfg.commission_per_side
    p1 = phase_break.get(1, {}).get("expectancy_usd")
    p2 = phase_break.get(2, {}).get("expectancy_usd")
    F5_THRESHOLD_PCT = 25.0
    gap_pct = (100.0 * abs(p1 - p2) / max(abs(p1), abs(p2), 1e-9)
              if p1 is not None and p2 is not None else None)
    distinguishable = gap_pct is not None and gap_pct > F5_THRESHOLD_PCT
    # F5 tests distinguishability alone (a reviewer-identified spec/code drift:
    # the spec's intent is "both ~breakeven AND indistinguishable" -- see "Known
    # defects (V1)" in the design spec). The human ruling on the analogous F2
    # defect applies here too: do NOT retune a pre-registered threshold or
    # comparison after the data has been seen. Instead, add a note so a PASS on
    # two losing phases cannot be misread as support for the fair-value premise,
    # plus a margin_pp field so a near-threshold verdict is visibly near-threshold.
    f5_margin_pp = gap_pct - F5_THRESHOLD_PCT if gap_pct is not None else None
    if p1 is None or p2 is None:
        f5_note = None
    elif p1 <= 0 and p2 <= 0:
        f5_note = (
            "BOTH PHASES LOSE MONEY (phase 1: ${:.2f}, phase 2: ${:.2f}) -- "
            "distinguishability does NOT imply the fair-value premise works. "
            "The relative gap only means phase 2 loses LESS than phase 1, not "
            "that either phase is profitable. This verdict must NOT be read as "
            "support for the strategy.".format(p1, p2))
    elif p1 > 0 and p2 > 0:
        f5_note = (
            "Both phases are profitable (phase 1: ${:.2f}, phase 2: ${:.2f}); "
            "the distinguishability verdict can be read in the ordinary sense "
            "-- as evidence about whether the fair-value premise adds "
            "discriminating power.".format(p1, p2))
    else:
        profitable_phase = 1 if p1 > 0 else 2
        f5_note = (
            "MIXED: only phase {} is profitable (phase 1: ${:.2f}, phase 2: "
            "${:.2f}) -- the gap reflects one profitable phase and one losing "
            "phase, not two comparably-performing ones.".format(
                profitable_phase, p1, p2))
    # F2's floor is a per-trade dollar number ($10 = 2 x 2 x commission_per_side)
    # that was pre-registered without accounting for `contracts` -- see "Known
    # defects (V1)" in the design spec. The human ruling: do NOT retune the
    # threshold after seeing the data. Instead distinguish the two ways a
    # positive-floor test can fail: gross expectancy <= 0 is an unambiguous
    # ADVERSE edge (negative before costs even reaches the floor question, and
    # corroborates F3 rather than being a validity problem); gross expectancy
    # > 0 but still under the floor is genuinely unmeasurable at this
    # threshold, not evidence either way.
    ge_note = ("ADVERSE: gross expectancy is negative before any costs are "
              "applied -- this is not a borderline cost-floor miss, it "
              "corroborates F3's PF<1.10 finding rather than being merely "
              "uninterpretable"
              if ge <= 0 else
              "gross expectancy is positive but below the cost floor -- "
              "genuinely unmeasurable at this threshold, not evidence of an "
              "adverse edge (see 'Known defects (V1)' in the design spec: "
              "the floor is a 1-contract number and this run's average "
              "contract count was not accounted for when it was "
              "pre-registered)")
    es_empty = n == 0
    nq_empty = nq_test.get("round_trips", 0) == 0

    def gated(kind: str, split: str, rule: str, empty: bool, computed: dict) -> dict:
        """F1-F6 read a Test split directly (no one-shot gate protects them). A
        split that was measured with zero trades must report NOT EVALUABLE —
        never a silent PASS (the .get(..., 0) defaults can make an empty NQ Test
        read as a false F6 PASS) and never a silent FAIL either. Mirrors the
        one_shot() pattern below, which already does this for F7/F8."""
        base = {"kind": kind, "split": split, "rule": rule}
        if empty:
            return {**base, "verdict": "NOT EVALUABLE (no trades in the split)"}
        return {**base, **computed}

    def one_shot(label, m, evaluable, why, kind):
        if not evaluable:
            return {"kind": kind, "rule": f"{label}: PF >= 1.05 and expectancy > 0",
                    "verdict": f"NOT EVALUABLE ({why})"}
        if not m.get("round_trips"):
            # Split was run but produced zero trades: it was never measured, so
            # this is NOT EVALUABLE, not a FAIL — FAIL means measured-and-missed.
            return {"kind": kind, "rule": f"{label}: PF >= 1.05 and expectancy > 0",
                    "verdict": "NOT EVALUABLE (no trades in the split)"}
        return {"kind": kind, "rule": f"{label}: PF >= 1.05 and expectancy > 0",
                "profit_factor": m.get("profit_factor"),
                "expectancy_usd": m.get("expectancy_usd"),
                "verdict": _verdict(m.get("profit_factor", 0) >= 1.05
                                    and m.get("expectancy_usd", 0) > 0)}

    return {
        "F1_sample_adequacy": gated(
            "VALIDITY GATE", "ES Test", ">= 300 trades", es_empty,
            {"value": n, "verdict": _verdict(n >= 300)}),
        "F2_cost_floor": gated(
            "VALIDITY GATE", "ES Test",
            f"gross expectancy > 2 x round-turn commission (${2 * rt_comm:.2f})",
            es_empty, {"value": ge, "verdict": _verdict(ge > 2 * rt_comm),
                       "note": ge_note}),
        "F3_core_claim": gated(
            "CLAIM TEST", "ES Test", "net profit factor >= 1.10", es_empty,
            {"value": pf, "verdict": _verdict(pf >= 1.10),
             "note": "HARD falsifier. Failing this ends the track — no sweep."}),
        "F4_win_rate": gated(
            "CLAIM TEST", "ES Test",
            "win rate reported against the 1.5R breakeven line", es_empty,
            {"value": wr, "breakeven_pct": BREAKEVEN_WIN_RATE,
             "verdict": _verdict(wr is not None and wr > BREAKEVEN_WIN_RATE)}),
        "F5_phase_discrimination": gated(
            "CLAIM TEST", "ES Test",
            "phase 1 and phase 2 expectancies must be distinguishable "
            "(>25% relative gap), else the fair-value premise does no work",
            es_empty,
            {"phase1_expectancy": p1, "phase2_expectancy": p2,
             "relative_gap_pct": gap_pct, "threshold_pct": F5_THRESHOLD_PCT,
             "margin_pp": f5_margin_pp, "note": f5_note,
             "verdict": _verdict(distinguishable)}),
        "F6_fill_fidelity": gated(
            "VALIDITY GATE", "NQ Test",
            "ambiguous-bar rate < 20% AND tick arbitration agrees in sign",
            nq_empty,
            {"ambiguous_pct": 100 * amb_rate,
             "sign_agrees": arb.get("sign_agrees"),
             "flipped_to_target": arb.get("flipped_to_target"),
             "verdict": _verdict(amb_rate < 0.20 and bool(arb.get("sign_agrees")))}),
        "F7_es_validation": one_shot(
            "ES Validation", es_val or {}, es_val is not None,
            "validation split not run", kind="CLAIM TEST"),
        "F8_nq_validation": one_shot(
            "NQ Validation", nq_val or {},
            nq_val is not None and nq_val_sessions >= cfg.min_validation_sessions,
            # Only report a session count when validation actually ran and
            # measured one. When nq_val is None (validation not run at all),
            # nq_val_sessions was never measured -- interpolating its
            # placeholder 0 here would state a fabricated fact ("0 sessions
            # cached") in the permanent artifact even though the cache may
            # hold many. Mirror F7's wording in that case instead.
            (f"{nq_val_sessions} sessions cached, need "
             f"{cfg.min_validation_sessions}" if nq_val is not None
             else "validation split not run"),
            kind="CLAIM TEST"),
    }


def _write(payload: dict, trades: list[dict], out_dir=None) -> None:
    """out_dir defaults to OUT (the real committed artifact dir); overridable
    so the selftest can exercise this against a throwaway directory instead
    of clobbering the committed report with test fixtures."""
    out_dir = out_dir or OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stats.json").write_text(json.dumps(payload, indent=2, default=str))
    (out_dir / "report.txt").write_text(_format(payload))
    # Always write trades.csv, even for a zero-trade run -- writing it only
    # `if trades` left a PREVIOUS run's trades.csv sitting next to a fresh
    # zero-trade stats.json, silently misrepresenting the new run.
    with (out_dir / "trades.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
        w.writeheader()
        if trades:
            w.writerows({k: t[k] for k in TRADE_COLUMNS} for t in trades)


# Splits gated by a one-shot validation falsifier: if that falsifier reads
# NOT EVALUABLE, the split's raw numbers must not be printed as if they were a
# result (design doc: "the report must never present a 6-session number as a
# result"). ES Test / NQ Test need no entry here — their own metrics() dict
# already goes through the "not m.get('round_trips')" branch below when empty.
_VALIDATION_GATE = {"ES Validation": "F7_es_validation", "NQ Validation": "F8_nq_validation"}


def _format(p: dict) -> str:
    L = ["=" * 74, f"  FVT V1 — engine v{p['engine_version']}",
         f"  generated {p['generated_utc']}", "=" * 74, ""]
    for name, m in p["splits"].items():
        L.append(f"{name}")
        gate_key = _VALIDATION_GATE.get(name)
        gate_verdict = (p["falsifiers"].get(gate_key, {}).get("verdict", "")
                        if gate_key else "")
        if not m or not m.get("round_trips"):
            L.append(f"  {(m or {}).get('note', 'not run')}")
        elif gate_verdict.startswith("NOT EVALUABLE"):
            # Split ran and has trades, but is undersized / otherwise not
            # evaluable — suppress the raw numbers so nobody mistakes a
            # 6-session profit factor for a finding. The verdict string already
            # carries the session count (see one_shot()'s `why`).
            L.append(f"  {gate_verdict}")
            L.append("  These numbers are NOT a result — do not read them as one.")
        else:
            for k, v in m.items():
                L.append(f"  {k:24s}: {v:,.4f}" if isinstance(v, float)
                         else f"  {k:24s}: {v}")
        L.append("")
    for tag, br in p.get("breakdowns", {}).items():
        L.append(f"BREAKDOWN BY {tag.upper()}")
        for k, m in br.items():
            L.append(f"  {tag}={k}: n={m.get('round_trips')} "
                     f"PF={m.get('profit_factor')} "
                     f"exp=${m.get('expectancy_usd')}")
        L.append("")
    L.append("FALSIFIERS")
    for k, v in p["falsifiers"].items():
        kind = v.get("kind", "?")
        L.append(f"  [{v['verdict']:>46s}]  [{kind:^14s}]  {k}")
        L.append(f"                                                          {v['rule']}")
        for kk, vv in v.items():
            if kk in ("verdict", "rule", "kind") or vv is None:
                continue
            L.append(f"                                                          {kk}: {vv}")
    if "counters" in p:
        # Design doc: "skipped for that window and counted in the report" --
        # these previously reached stats.json only. Surfacing them here is
        # what makes 3,253 signals -> 2,949 trades (etc.) readable without
        # cross-referencing the JSON.
        L += ["", "COUNTERS"]
        for split, c in p["counters"].items():
            L.append(f"  {split}: " + ", ".join(f"{kk}={vv}" for kk, vv in c.items()))
        L.append("")
    L += ["", "=" * 74, "  CAVEATS",
          "  * ES is an ADJACENT-INSTRUMENT PROXY. JJ's claim is NQ-specific.",
          "    A pass on ES is evidence about the entry model, not proof about NQ.",
          "  * Ambiguous bars resolve to the STOP. The ambiguous_pct figure bounds",
          "    how much of the result rests on that assumption.",
          "  * [VALIDITY GATE] (F1, F2, F6): failing one means the result is",
          "    UNINTERPRETABLE in general — a different thing from the strategy",
          "    being bad. EXCEPTION: F2 failing ADVERSELY (gross expectancy <= 0,",
          "    see its `note`) is not an interpretability problem — a negative",
          "    edge before any costs are even applied is unambiguous, and it",
          "    CORROBORATES a failing F3 rather than retracting it.",
          "    [CLAIM TEST] (F3, F4, F5, F7, F8): these judge the strategy itself.",
          "  * The NQ validation split grows with each yfinance harvest. Until it",
          "    reaches the minimum session count its verdict is NOT EVALUABLE,",
          "    which is not the same as a weak pass.",
          "=" * 74]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FVT V1 statistics")
    ap.add_argument("--validation", action="store_true",
                    help="ALSO run the held-out validation splits. Run ONCE.")
    ap.add_argument("--harvest", action="store_true",
                    help="refresh the yfinance NQ cache before running")
    a = ap.parse_args(argv)

    if a.harvest:
        c = fd.harvest_nq_yfinance()
        print(f"harvested NQ cache: {len(c)} bars, "
              f"{fd.session_count(c)} RTH sessions", flush=True)

    cfg_es = FVTConfig(stop_mode="atr_multiple", usd_per_point=50.0, tick_size=0.25)
    cfg_nq = FVTConfig(stop_mode="literal", usd_per_point=20.0, tick_size=0.25)

    print("ES Test ...", flush=True)
    es = fd.load_es_1m()
    es_test_trades, es_test_counters = run_backtest(
        fd.slice_dates(es, *fd.ES_TEST), cfg_es, "ES")
    print(f"  {len(es_test_trades)} trades", flush=True)

    print("NQ Test ...", flush=True)
    nq_test_trades, nq_test_counters = run_backtest(fd.load_nq_test(), cfg_nq, "NQ")
    print(f"  {len(nq_test_trades)} trades", flush=True)
    arb = arbitrate_trades(nq_test_trades, cfg_nq.usd_per_point, "NQ")

    all_trades = es_test_trades + nq_test_trades
    es_test_m = metrics(es_test_trades)
    nq_test_m = metrics(nq_test_trades)
    es_val_m = nq_val_m = None
    nq_val_sessions = 0

    if a.validation:
        print("VALIDATION SPLITS — this is the one-shot run.", flush=True)
        es_val_trades, _ = run_backtest(
            fd.slice_dates(es, *fd.ES_VALIDATION), cfg_es, "ES")
        es_val_m = metrics(es_val_trades)
        nq_val_bars = fd.load_nq_validation()
        nq_val_sessions = fd.session_count(nq_val_bars)
        nq_val_trades, _ = run_backtest(nq_val_bars, cfg_nq, "NQ") if len(nq_val_bars) else ([], {})
        nq_val_m = metrics(nq_val_trades)
        all_trades += es_val_trades + nq_val_trades

    amb_rate = (nq_test_m.get("ambiguous_pct", 0.0) / 100.0
                if nq_test_m.get("round_trips") else 0.0)
    payload = {
        "engine_version": FVT_ENGINE_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "config_es": dict(cfg_es.__dict__), "config_nq": dict(cfg_nq.__dict__),
        "counters": {"ES_Test": es_test_counters, "NQ_Test": nq_test_counters,
                    "Data": {"es_dropped_misaligned_days": fd.LAST_ES_DROPPED_DAYS}},
        "arbitration": arb,
        "splits": {"ES Test": es_test_m, "NQ Test": nq_test_m,
                   "ES Validation": es_val_m, "NQ Validation": nq_val_m},
        "breakdowns": {
            "phase": tag_breakdown(es_test_trades, "phase"),
            "structure": tag_breakdown(es_test_trades, "structure"),
            "attempt_n": tag_breakdown(es_test_trades, "attempt_n"),
        },
    }
    payload["falsifiers"] = falsifiers(
        es_test=es_test_m, es_val=es_val_m, nq_test=nq_test_m, nq_val=nq_val_m,
        phase_break=payload["breakdowns"]["phase"], amb_rate=amb_rate, arb=arb,
        nq_val_sessions=nq_val_sessions, cfg=cfg_es)
    _write(payload, all_trades)
    print(_format(payload))
    print(f"artifacts -> {OUT}")
    return 0


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    check("a fixed 1.5R needs a 40% win rate to break even",
          abs(BREAKEVEN_WIN_RATE - 40.0) < 1e-9)

    def tr(net, phase=1, structure="BOS", attempt=1):
        return {"net_usd": net, "gross_usd": net + 5.0, "commission_usd": 5.0,
                "phase": phase, "structure": structure, "attempt_n": attempt,
                "ambiguous_bar": False, "contracts": 1, "exit_reason": "target"}

    m = metrics([tr(100.0), tr(100.0), tr(-100.0)])
    check("profit factor is gross win over gross loss",
          abs(m["profit_factor"] - 2.0) < 1e-9)
    check("win rate is a percentage", abs(m["win_rate_pct"] - 66.6667) < 1e-3)
    check("expectancy is mean net", abs(m["expectancy_usd"] - 33.3333) < 1e-3)
    check("max drawdown is measured off the running peak",
          abs(m["max_drawdown_usd"] - 100.0) < 1e-9)
    check("no trades yields an explicit undefined, not a zero",
          metrics([])["round_trips"] == 0 and "note" in metrics([]))

    br = tag_breakdown([tr(10.0, phase=1), tr(-5.0, phase=2), tr(20.0, phase=2)],
                       "phase")
    check("tag breakdown splits by the tag", set(br) == {1, 2})
    check("tag breakdown counts correctly", br[2]["round_trips"] == 2)

    # --- Falsifier wiring -------------------------------------------------
    good = {"round_trips": 400, "profit_factor": 1.30, "win_rate_pct": 45.0,
            "expectancy_usd": 60.0, "gross_expectancy_usd": 65.0,
            "commission_usd": 2000.0, "gross_usd": 26000.0, "net_usd": 24000.0}
    f = falsifiers(es_test=good, es_val=None, nq_test=good, nq_val=None,
                   phase_break={1: {"expectancy_usd": 80.0, "round_trips": 200},
                                2: {"expectancy_usd": 10.0, "round_trips": 200}},
                   amb_rate=0.05, arb={"sign_agrees": True},
                   nq_val_sessions=0, cfg=FVTConfig())
    check("F1 passes on 400 trades", f["F1_sample_adequacy"]["verdict"] == "PASS")
    check("F2 reads gross expectancy against the round-turn cost floor",
          f["F2_cost_floor"]["verdict"] == "PASS")
    check("F3 passes at PF 1.30", f["F3_core_claim"]["verdict"] == "PASS")
    check("F4 reports the 40% breakeven line next to the actual",
          f["F4_win_rate"]["breakeven_pct"] == BREAKEVEN_WIN_RATE)
    check("F7 is NOT EVALUABLE before the validation split is run",
          "NOT EVALUABLE" in f["F7_es_validation"]["verdict"])
    check("F8 is NOT EVALUABLE while the NQ cache is undersized",
          "NOT EVALUABLE" in f["F8_nq_validation"]["verdict"])
    check("F8 with nq_val=None (validation not run) reports 'validation split "
          "not run', matching F7 -- NOT a fabricated session count computed from "
          "the un-measured nq_val_sessions=0 default",
          f["F8_nq_validation"]["verdict"] == "NOT EVALUABLE (validation split not run)"
          and "sessions cached" not in f["F8_nq_validation"]["verdict"])

    thin = dict(good, round_trips=100)
    f2 = falsifiers(es_test=thin, es_val=None, nq_test=thin, nq_val=None,
                    phase_break={}, amb_rate=0.05, arb={"sign_agrees": True},
                    nq_val_sessions=0, cfg=FVTConfig())
    check("F1 fails under 300 trades", f2["F1_sample_adequacy"]["verdict"] == "FAIL")

    bad_pf = dict(good, profit_factor=1.02)
    f3 = falsifiers(es_test=bad_pf, es_val=None, nq_test=good, nq_val=None,
                    phase_break={}, amb_rate=0.05, arb={"sign_agrees": True},
                    nq_val_sessions=0, cfg=FVTConfig())
    check("F3 fails below PF 1.10", f3["F3_core_claim"]["verdict"] == "FAIL")

    f4 = falsifiers(es_test=good, es_val=None, nq_test=good, nq_val=None,
                    phase_break={}, amb_rate=0.35, arb={"sign_agrees": True},
                    nq_val_sessions=0, cfg=FVTConfig())
    check("F6 fails when more than 20% of trades are ambiguous",
          f4["F6_fill_fidelity"]["verdict"] == "FAIL")

    f5 = falsifiers(es_test=good, es_val=None, nq_test=good, nq_val=good,
                    phase_break={}, amb_rate=0.05, arb={"sign_agrees": True},
                    nq_val_sessions=25, cfg=FVTConfig())
    check("F8 becomes evaluable once the cache reaches 20 sessions",
          "NOT EVALUABLE" not in f5["F8_nq_validation"]["verdict"])

    f_empty = falsifiers(es_test=good, es_val=metrics([]), nq_test=good, nq_val=None,
                         phase_break={}, amb_rate=0.05, arb={"sign_agrees": True},
                         nq_val_sessions=0, cfg=FVTConfig())
    check("F7 is NOT EVALUABLE when the validation split ran but produced no trades",
          "NOT EVALUABLE" in f_empty["F7_es_validation"]["verdict"])
    check("F7 does not report FAIL for an empty split",
          f_empty["F7_es_validation"]["verdict"] != "FAIL")

    f_empty8 = falsifiers(es_test=good, es_val=None, nq_test=good, nq_val=metrics([]),
                          phase_break={}, amb_rate=0.05, arb={"sign_agrees": True},
                          nq_val_sessions=25, cfg=FVTConfig())
    check("F8 is NOT EVALUABLE when the cache is big enough but the split ran no trades",
          "NOT EVALUABLE" in f_empty8["F8_nq_validation"]["verdict"])
    check("F8 does not report FAIL for an empty split",
          f_empty8["F8_nq_validation"]["verdict"] != "FAIL")

    # --- Zero-trade Test splits must not publish a verdict --------------------
    f_es_empty = falsifiers(es_test=metrics([]), es_val=None, nq_test=good, nq_val=None,
                            phase_break={}, amb_rate=0.05, arb={"sign_agrees": True},
                            nq_val_sessions=0, cfg=FVTConfig())
    v1 = f_es_empty["F1_sample_adequacy"]["verdict"]
    check("F1 is NOT EVALUABLE (never PASS/FAIL) when ES Test has zero trades",
          "NOT EVALUABLE" in v1 and v1 not in ("PASS", "FAIL"))
    v2 = f_es_empty["F2_cost_floor"]["verdict"]
    check("F2 is NOT EVALUABLE (never PASS/FAIL) when ES Test has zero trades",
          "NOT EVALUABLE" in v2 and v2 not in ("PASS", "FAIL"))
    v3 = f_es_empty["F3_core_claim"]["verdict"]
    check("F3 is NOT EVALUABLE (never PASS/FAIL) when ES Test has zero trades",
          "NOT EVALUABLE" in v3 and v3 not in ("PASS", "FAIL"))
    v4 = f_es_empty["F4_win_rate"]["verdict"]
    check("F4 is NOT EVALUABLE (never PASS/FAIL) when ES Test has zero trades",
          "NOT EVALUABLE" in v4 and v4 not in ("PASS", "FAIL"))
    v5 = f_es_empty["F5_phase_discrimination"]["verdict"]
    check("F5 is NOT EVALUABLE (never PASS/FAIL) when ES Test has zero trades",
          "NOT EVALUABLE" in v5 and v5 not in ("PASS", "FAIL"))

    # the exact combination that previously produced a false F6 PASS from no evidence
    f_nq_empty = falsifiers(es_test=good, es_val=None, nq_test=metrics([]), nq_val=None,
                            phase_break={}, amb_rate=0.0, arb={"sign_agrees": True},
                            nq_val_sessions=0, cfg=FVTConfig())
    v6 = f_nq_empty["F6_fill_fidelity"]["verdict"]
    check("F6 is NOT EVALUABLE, not a false PASS, when NQ Test has zero trades "
          "(amb_rate=0.0, sign_agrees=True)",
          "NOT EVALUABLE" in v6 and v6 not in ("PASS", "FAIL"))

    # --- F4's verdict, not just its constant ------------------------------
    f4_pass = falsifiers(es_test=dict(good, win_rate_pct=45.0), es_val=None,
                         nq_test=good, nq_val=None, phase_break={}, amb_rate=0.05,
                         arb={"sign_agrees": True}, nq_val_sessions=0, cfg=FVTConfig())
    check("F4 PASSes when win rate (45%) clears the 40% breakeven line",
          f4_pass["F4_win_rate"]["verdict"] == "PASS")

    f4_fail = falsifiers(es_test=dict(good, win_rate_pct=35.0), es_val=None,
                         nq_test=good, nq_val=None, phase_break={}, amb_rate=0.05,
                         arb={"sign_agrees": True}, nq_val_sessions=0, cfg=FVTConfig())
    check("F4 FAILs when win rate (35%) is below the 40% breakeven line",
          f4_fail["F4_win_rate"]["verdict"] == "FAIL")

    f4_none = falsifiers(es_test=dict(good, win_rate_pct=None), es_val=None,
                         nq_test=good, nq_val=None, phase_break={}, amb_rate=0.05,
                         arb={"sign_agrees": True}, nq_val_sessions=0, cfg=FVTConfig())
    check("F4 with win_rate_pct=None does not crash and does not PASS",
          f4_none["F4_win_rate"]["verdict"] != "PASS")

    # --- F5 coverage (previously zero: F5 only appeared in its own definition) -
    check("F5 PASSes with clearly distinguishable phase expectancies (80 vs 10)",
          f["F5_phase_discrimination"]["verdict"] == "PASS")
    check("F5 reports the actual relative gap (|80-10|/80 = 87.5%), not just "
          "the two raw expectancies",
          abs(f["F5_phase_discrimination"]["relative_gap_pct"] - 87.5) < 1e-6)
    check("F5 reports the 25% threshold it compared the gap against",
          f["F5_phase_discrimination"]["threshold_pct"] == 25.0)

    f5_fail = falsifiers(es_test=good, es_val=None, nq_test=good, nq_val=None,
                         phase_break={1: {"expectancy_usd": 50.0, "round_trips": 200},
                                      2: {"expectancy_usd": 49.0, "round_trips": 200}},
                         amb_rate=0.05, arb={"sign_agrees": True},
                         nq_val_sessions=0, cfg=FVTConfig())
    check("F5 FAILs with near-identical phase expectancies (50 vs 49)",
          f5_fail["F5_phase_discrimination"]["verdict"] == "FAIL")
    check("F5's reported gap for the near-identical case (|50-49|/50 = 2%) is "
          "well under the threshold, showing HOW CLOSE the FAIL was, not just that",
          abs(f5_fail["F5_phase_discrimination"]["relative_gap_pct"] - 2.0) < 1e-6)

    f5_one_missing = falsifiers(es_test=good, es_val=None, nq_test=good, nq_val=None,
                                phase_break={1: {"expectancy_usd": 80.0, "round_trips": 200}},
                                amb_rate=0.05, arb={"sign_agrees": True},
                                nq_val_sessions=0, cfg=FVTConfig())
    check("F5 does not crash or PASS when phase 2 is absent from the breakdown",
          f5_one_missing["F5_phase_discrimination"]["verdict"] != "PASS"
          and f5_one_missing["F5_phase_discrimination"]["phase2_expectancy"] is None)
    check("F5's relative_gap_pct is None (not a crash, not a fabricated 0) when "
          "a phase is missing from the breakdown",
          f5_one_missing["F5_phase_discrimination"]["relative_gap_pct"] is None)

    f5_both_missing = falsifiers(es_test=good, es_val=None, nq_test=good, nq_val=None,
                                 phase_break={}, amb_rate=0.05, arb={"sign_agrees": True},
                                 nq_val_sessions=0, cfg=FVTConfig())
    check("F5 does not crash or PASS when both phases are absent from the breakdown",
          f5_both_missing["F5_phase_discrimination"]["verdict"] != "PASS")

    # --- F5's note must distinguish a PASS that actually supports the
    # fair-value premise from a PASS that merely means "loses less" -- a
    # reviewer-identified spec/code drift (F5 tests distinguishability alone,
    # not "both ~breakeven AND indistinguishable" as the spec intends; see
    # "Known defects (V1)"). The threshold/comparison/verdict logic are
    # UNCHANGED by this fix -- only the note and margin_pp fields are new. The
    # real V1 run passed F5 at a 25.19% gap between two losing phases
    # (-$255.65 and -$191.24), which is exactly the misleading case below.
    f5_both_negative = falsifiers(
        es_test=good, es_val=None, nq_test=good, nq_val=None,
        phase_break={1: {"expectancy_usd": -255.6465795532384, "round_trips": 1550},
                     2: {"expectancy_usd": -191.2387987435485, "round_trips": 1335}},
        amb_rate=0.05, arb={"sign_agrees": True}, nq_val_sessions=0, cfg=FVTConfig())
    f5bn = f5_both_negative["F5_phase_discrimination"]
    check("F5 still PASSes on the real V1 numbers (both phases losing, 25.19% "
          "gap) -- confirms this fix changes NO verdict or threshold",
          f5bn["verdict"] == "PASS")
    check("F5's note says BOTH PHASES LOSE MONEY when phase1<=0 and phase2<=0",
          "BOTH PHASES LOSE MONEY" in f5bn["note"])
    check("F5's both-negative note explicitly says the verdict must NOT be "
          "read as support for the strategy",
          "must NOT be read as support" in f5bn["note"])
    check("F5's margin_pp on the real V1 run is the +0.19pp near-miss above "
          "the 25% threshold (25.194... - 25.0), not a comfortable pass",
          abs(f5bn["margin_pp"] - 0.194071018766355) < 1e-6)

    f5bp_note = f["F5_phase_discrimination"]["note"]  # reuses the 80-vs-10 fixture
    check("F5's note does NOT carry the both-losing caveat when both phases "
          "are profitable",
          "BOTH PHASES LOSE MONEY" not in f5bp_note and "MIXED" not in f5bp_note)
    check("F5's both-positive note reads in the ordinary sense (no caveat "
          "needed) and names both expectancies",
          "profitable" in f5bp_note and "ordinary sense" in f5bp_note)

    f5_mixed = falsifiers(es_test=good, es_val=None, nq_test=good, nq_val=None,
                          phase_break={1: {"expectancy_usd": 50.0, "round_trips": 200},
                                       2: {"expectancy_usd": -10.0, "round_trips": 200}},
                          amb_rate=0.05, arb={"sign_agrees": True},
                          nq_val_sessions=0, cfg=FVTConfig())
    f5m_note = f5_mixed["F5_phase_discrimination"]["note"]
    check("F5's mixed-sign note says MIXED and names phase 1 as the "
          "profitable one when phase 1 > 0 > phase 2",
          "MIXED" in f5m_note and "phase 1 is profitable" in f5m_note)
    check("F5's mixed-sign note does not also carry the both-losing caveat",
          "BOTH PHASES LOSE MONEY" not in f5m_note)

    f5_mixed2 = falsifiers(es_test=good, es_val=None, nq_test=good, nq_val=None,
                           phase_break={1: {"expectancy_usd": -10.0, "round_trips": 200},
                                        2: {"expectancy_usd": 50.0, "round_trips": 200}},
                           amb_rate=0.05, arb={"sign_agrees": True},
                           nq_val_sessions=0, cfg=FVTConfig())
    check("F5's mixed-sign note correctly names phase 2 (not hardcoded to "
          "phase 1) as the profitable one when the signs are reversed",
          "phase 2 is profitable" in f5_mixed2["F5_phase_discrimination"]["note"])

    check("F5's margin_pp is negative (below-threshold) for the near-identical "
          "FAIL case, consistent with the FAIL verdict",
          f5_fail["F5_phase_discrimination"]["margin_pp"] < 0)
    check("F5's note is None (not fabricated) rather than crashing when a "
          "phase is missing from the breakdown",
          f5_one_missing["F5_phase_discrimination"]["note"] is None
          and f5_both_missing["F5_phase_discrimination"]["note"] is None)

    # --- F2's note distinguishes an ADVERSE (negative) gross expectancy from
    # one that is merely positive-but-below-the-floor -- the pre-registered
    # floor is a per-trade dollar number that does not account for contracts
    # (see "Known defects (V1)" in the design spec), so a below-floor FAIL is
    # genuinely unmeasurable, while a negative-before-costs FAIL is not.
    f2_adverse = falsifiers(es_test=dict(good, gross_expectancy_usd=-5.0), es_val=None,
                            nq_test=good, nq_val=None, phase_break={}, amb_rate=0.05,
                            arb={"sign_agrees": True}, nq_val_sessions=0, cfg=FVTConfig())
    check("F2's note calls a negative (<=0) gross expectancy ADVERSE",
          "ADVERSE" in f2_adverse["F2_cost_floor"]["note"])

    f2_below_floor = falsifiers(es_test=dict(good, gross_expectancy_usd=3.0), es_val=None,
                                nq_test=good, nq_val=None, phase_break={}, amb_rate=0.05,
                                arb={"sign_agrees": True}, nq_val_sessions=0, cfg=FVTConfig())
    check("F2's note does NOT call a positive-but-below-floor gross expectancy "
          "ADVERSE -- it is unmeasurable, not evidence of a bad edge",
          f2_below_floor["F2_cost_floor"]["verdict"] == "FAIL"
          and "ADVERSE" not in f2_below_floor["F2_cost_floor"]["note"])

    # --- F7/F8 "measured and missed" must FAIL, not read NOT EVALUABLE ----
    f7_fail = falsifiers(es_test=good,
                         es_val={"round_trips": 50, "profit_factor": 0.90,
                                 "expectancy_usd": -5.0},
                         nq_test=good, nq_val=None, phase_break={}, amb_rate=0.05,
                         arb={"sign_agrees": True}, nq_val_sessions=0, cfg=FVTConfig())
    check("F7 FAILs (not NOT EVALUABLE) when the validation split ran and missed the bar",
          f7_fail["F7_es_validation"]["verdict"] == "FAIL")

    f8_fail = falsifiers(es_test=good, es_val=None, nq_test=good,
                         nq_val={"round_trips": 50, "profit_factor": 0.80,
                                 "expectancy_usd": -8.0},
                         phase_break={}, amb_rate=0.05, arb={"sign_agrees": True},
                         nq_val_sessions=25, cfg=FVTConfig())
    check("F8 FAILs (not NOT EVALUABLE) when the validation split ran and missed the bar",
          f8_fail["F8_nq_validation"]["verdict"] == "FAIL")

    # --- kind labels --------------------------------------------------------
    check("F1/F2/F6 are tagged VALIDITY GATE",
          f["F1_sample_adequacy"]["kind"] == "VALIDITY GATE"
          and f["F2_cost_floor"]["kind"] == "VALIDITY GATE"
          and f["F6_fill_fidelity"]["kind"] == "VALIDITY GATE")
    check("F3/F4/F5/F7/F8 are tagged CLAIM TEST",
          f["F3_core_claim"]["kind"] == "CLAIM TEST"
          and f["F4_win_rate"]["kind"] == "CLAIM TEST"
          and f["F5_phase_discrimination"]["kind"] == "CLAIM TEST"
          and f["F7_es_validation"]["kind"] == "CLAIM TEST"
          and f["F8_nq_validation"]["kind"] == "CLAIM TEST")

    # --- _format() must not print an undersized split's numbers as a result ---
    nqv_metrics = metrics([tr(100.0), tr(100.0), tr(-40.0)])
    fmt_payload = {
        "engine_version": "test", "generated_utc": "2026-01-01T00:00:00+00:00",
        "splits": {"ES Test": metrics([tr(10.0)]), "NQ Test": metrics([tr(10.0)]),
                   "ES Validation": None, "NQ Validation": nqv_metrics},
        "breakdowns": {},
        "falsifiers": {
            "F8_nq_validation": {"kind": "CLAIM TEST",
                                  "rule": "NQ Validation: PF >= 1.05 and expectancy > 0",
                                  "verdict": "NOT EVALUABLE (6 sessions cached, need 20)"},
        },
    }
    rendered = _format(fmt_payload)
    nqv_block = rendered[rendered.index("NQ Validation"):].split("FALSIFIERS")[0]
    pf_str = f"{nqv_metrics['profit_factor']:,.4f}"
    check("_format suppresses an undersized split's raw profit-factor value",
          pf_str not in nqv_block)
    check("_format still surfaces a NOT EVALUABLE marker for the suppressed split",
          "NOT EVALUABLE" in nqv_block)
    check("_format does not leak the raw metrics keys for the suppressed split",
          "profit_factor" not in nqv_block)

    # --- _format() must widen the verdict column so a long NOT EVALUABLE
    # string does not push its own row's [kind] bracket out of alignment
    # with a short PASS/FAIL row's -----------------------------------------
    fmt_payload2 = {
        "engine_version": "test", "generated_utc": "2026-01-01T00:00:00+00:00",
        "splits": {}, "breakdowns": {},
        "falsifiers": {
            "F1_short": {"kind": "VALIDITY GATE", "rule": "r", "verdict": "PASS"},
            "F8_long": {"kind": "CLAIM TEST", "rule": "r",
                        "verdict": "NOT EVALUABLE (123 sessions cached, need 20)"},
        },
    }
    rendered2 = _format(fmt_payload2)
    verdict_lines = [ln for ln in rendered2.splitlines() if ln.startswith("  [")]
    check("the falsifier table has one bracketed line per falsifier",
          len(verdict_lines) == 2)
    check("the verdict column is wide enough that a 44-char NOT EVALUABLE "
          "string keeps its [kind] bracket in the same column as a short "
          "PASS/FAIL row's -- not visibly breaking the table's alignment",
          verdict_lines[0].index("]  [") == verdict_lines[1].index("]  ["))

    # --- _format() must surface the skip/gate counters -- the design doc
    # promises skipped windows are "counted in the report", but they
    # previously reached only stats.json, not report.txt --------------------
    fmt_payload3 = {
        "engine_version": "test", "generated_utc": "2026-01-01T00:00:00+00:00",
        "splits": {}, "breakdowns": {}, "falsifiers": {},
        "counters": {"ES_Test": {"skipped_phase_gate": 4190, "windows_no_anchor": 90,
                                 "skipped_in_position": 304, "skipped_no_size": 13},
                    "Data": {"es_dropped_misaligned_days": 220}},
    }
    rendered3 = _format(fmt_payload3)
    check("_format surfaces skipped_phase_gate/windows_no_anchor/"
          "skipped_in_position/skipped_no_size in the report, not just stats.json",
          "skipped_phase_gate=4190" in rendered3 and "windows_no_anchor=90" in rendered3
          and "skipped_in_position=304" in rendered3
          and "skipped_no_size=13" in rendered3)
    check("_format surfaces the dropped-misaligned-day count in the report",
          "es_dropped_misaligned_days=220" in rendered3)

    # --- _write must emit a header-only trades.csv for a zero-trade run, not
    # silently leave a previous run's file sitting beside a fresh stats.json.
    # Written to a throwaway subdirectory of OUT (still inside the one
    # writable artifact directory) so this never touches the real committed
    # report. --------------------------------------------------------------
    tmp_dir = OUT / "_selftest_tmp"
    try:
        minimal_payload = {"engine_version": "test",
                           "generated_utc": "2026-01-01T00:00:00+00:00",
                           "splits": {}, "breakdowns": {}, "falsifiers": {}}
        _write(minimal_payload, [], out_dir=tmp_dir)
        with (tmp_dir / "trades.csv").open(newline="") as fh:
            rows = list(csv.reader(fh))
        check("_write emits a header-only trades.csv for a zero-trade run "
              "instead of skipping the write entirely",
              len(rows) == 1 and rows[0] == TRADE_COLUMNS)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    check("trade columns match the spec's trade record",
          "ambiguous_bar" in TRADE_COLUMNS and "attempt_n" in TRADE_COLUMNS)

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else main(sys.argv[1:]))
