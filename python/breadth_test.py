"""breadth_test.py — trial #2 zero-tuning breadth test.

See research/mr-v1-connors-rsi2/PREREG_trial2_breadth.md. Fixed design-doc params (NO
tuning) applied ONCE to a fresh, unseen set of upward-biased equity indices
(Yahoo daily cash), pooled as one portfolio. Verdict via the 3 pre-registered
criteria (reuses crossval.verdict).
"""
from __future__ import annotations
import sys
from pathlib import Path

from MeanReversion1 import Config, Backtester, load_data, metrics, save_outputs
from crossval import verdict, COST_BPS

YAHOO = Path(__file__).resolve().parent.parent / "data" / "yahoo"
FRESH = ["FTSE", "CAC", "N225"]                   # LOCKED test set (trial #2)
TRIAL1_SEEN = {"EP", "YM", "NQ", "RTY", "DAX"}     # excluded: already examined
FIXED = {"rsi_entry": 5.0, "atr_stop_mult": 2.5, "max_hold": 7}   # design-doc defaults, NOT tuned


def load_fresh() -> dict:
    data: dict = {}
    for sym in FRESH:
        data.update(load_data(Config(data_path=str(YAHOO / f"{sym}.csv"))))
    return data


def run():
    data = load_fresh()
    cfg = Config(cost_bps=COST_BPS, make_plot=True, save_trades=True,
                 rsi_entry=FIXED["rsi_entry"], atr_stop_mult=FIXED["atr_stop_mult"],
                 max_hold=FIXED["max_hold"])
    bt = Backtester(data, cfg)
    bt.run()
    pooled = metrics(bt.equity_curve, bt.trades)
    per: dict = {}
    for sym in data:
        st = [t for t in bt.trades if t.symbol == sym]
        gw = sum(t.pnl for t in st if t.pnl > 0)
        gl = -sum(t.pnl for t in st if t.pnl <= 0)
        per[sym] = {"n": len(st), "pf": (gw / gl if gl > 0 else float("inf"))}
    return pooled, per, bt, cfg


def main() -> int:
    run_dir = Path(__file__).resolve().parent.parent / "research" / "mr-v1-connors-rsi2" / "artifacts" / "trial2"
    run_dir.mkdir(parents=True, exist_ok=True)
    pooled, per, bt, cfg = run()
    cfg.figures_dir = str(run_dir)
    save_outputs(bt, cfg)
    v = verdict(pooled, per)
    lines = [
        "# MR Index-Swing V1 -- trial #2 zero-tuning breadth test",
        f"Fixed params (NO tuning): {FIXED}   cost_bps={COST_BPS}/side",
        f"Fresh test set (Yahoo cash indices): {sorted(per)}",
        "",
        f"Aggregate trades : {pooled['n_trades']}   (criterion: >= 100)",
        f"Pooled PF        : {pooled['profit_factor']:.2f}   (criterion: > 1.0)",
        "Per-instrument   : " + ", ".join(f"{s}(n={v2['n']}, PF={v2['pf']:.2f})" for s, v2 in per.items()),
        f"CAGR {pooled['cagr']:+.2%} | MaxDD {pooled['max_drawdown']:.2%} | Sharpe {pooled['sharpe']:.2f} (context)",
        "",
        f"VERDICT: {'PASS -- supported' if v['PASS'] else 'FAIL -- not supported'}",
    ]
    report = "\n".join(lines)
    (run_dir / "breadth_report.txt").write_text(report)
    (run_dir / "README.md").write_text(report + "\n")
    print(report)
    return 0


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    check("fresh set disjoint from trial-1 seen", set(FRESH).isdisjoint(TRIAL1_SEEN))
    check("all fresh data files exist", all((YAHOO / f"{s}.csv").exists() for s in FRESH))
    v = verdict({"n_trades": 150, "profit_factor": 1.4},
                {"FTSE": {"n": 50, "pf": 1.2}, "CAC": {"n": 50, "pf": 1.1}, "N225": {"n": 50, "pf": 0.9}})
    check("verdict wiring PASS on synthetic pass-case", v["PASS"] is True)

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else main())
