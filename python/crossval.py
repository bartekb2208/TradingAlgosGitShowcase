"""crossval.py — cross-instrument tune/test for MR V1 (see research/mr-v1-connors-rsi2/PREREG_trial1_cross_instrument.md).

Tune one global param set on {EP, YM}; freeze; test ONCE on {NQ, RTY, DAX}.
Leakage is structural: tune/ and test/ are separate dirs; test is never loaded
during tuning.
"""
from __future__ import annotations
import itertools
import sys
from pathlib import Path

from MeanReversion1 import Config, Backtester, load_data, metrics
from mt5_adapter import clean_file

L1 = Path(__file__).resolve().parent.parent / "data" / "L1"
INSTRUMENTS = {
    "tune": {"EP": L1 / "EP_PERIOD_CURRENTDAILY2000-2026.csv",
             "YM": L1 / "Daily" / "YMDaily.csv"},
    "test": {"NQ": L1 / "Daily" / "ENQDailyOHLC.csv",
             "RTY": L1 / "Daily" / "RTYDaily.csv",
             "DAX": L1 / "Daily" / "DDDaily.csv"},
}
GRID = {"rsi_entry": [2.0, 5.0, 10.0], "atr_stop_mult": [2.0, 2.5, 3.0], "max_hold": [5, 7, 10]}
COST_BPS = 1.0
MIN_TRADES = 10


def param_grid() -> list[dict]:
    keys = list(GRID)
    return [dict(zip(keys, vals)) for vals in itertools.product(*GRID.values())]


def prepare_data(root: str) -> tuple[str, str]:
    tune_dir = Path(root) / "tune"
    test_dir = Path(root) / "test"
    for role, mapping in INSTRUMENTS.items():
        dest = tune_dir if role == "tune" else test_dir
        for sym, src in mapping.items():
            clean_file(str(src), str(dest / f"{sym}.csv"))
    return str(tune_dir), str(test_dir)


def run_combo(data: dict, params: dict, cost_bps: float = COST_BPS) -> dict:
    cfg = Config(cost_bps=cost_bps, make_plot=False, save_trades=False,
                 rsi_entry=params["rsi_entry"], atr_stop_mult=params["atr_stop_mult"],
                 max_hold=params["max_hold"])
    bt = Backtester(data, cfg)
    bt.run()
    m = metrics(bt.equity_curve, bt.trades)
    m["params"] = params
    return m


def tune(tune_data: dict, cost_bps: float = COST_BPS, min_trades: int = MIN_TRADES) -> tuple[dict, list[dict]]:
    results = [run_combo(tune_data, p, cost_bps) for p in param_grid()]
    eligible = [r for r in results if r.get("n_trades", 0) >= min_trades]
    if not eligible:
        raise ValueError("no parameter set met the min-trades guard")
    best = max(eligible, key=lambda r: (r["profit_factor"], r["n_trades"], r["total_return"]))
    return best["params"], results


def evaluate(test_data: dict, frozen: dict, cost_bps: float = COST_BPS):
    cfg = Config(cost_bps=cost_bps, make_plot=True, save_trades=True,
                 rsi_entry=frozen["rsi_entry"], atr_stop_mult=frozen["atr_stop_mult"],
                 max_hold=frozen["max_hold"])
    bt = Backtester(test_data, cfg)
    bt.run()
    pooled = metrics(bt.equity_curve, bt.trades)
    per: dict = {}
    for sym in test_data:
        st = [t for t in bt.trades if t.symbol == sym]
        gw = sum(t.pnl for t in st if t.pnl > 0)
        gl = -sum(t.pnl for t in st if t.pnl <= 0)
        per[sym] = {"n": len(st), "pf": (gw / gl if gl > 0 else float("inf"))}
    return pooled, per, bt, cfg


def verdict(pooled: dict, per: dict) -> dict:
    n = pooled.get("n_trades", 0)
    pf = pooled.get("profit_factor", 0.0)
    profitable = [s for s, v in per.items() if v["pf"] > 1.0]
    broken = [s for s, v in per.items() if v["pf"] < 0.8]
    passed = (n >= 100) and (pf > 1.0) and (len(profitable) >= 2) and (len(broken) == 0)
    return {"n_trades": n, "pooled_pf": pf, "profitable": profitable, "broken": broken, "PASS": passed}


def main() -> int:
    import json
    from MeanReversion1 import save_outputs
    root = Path(__file__).resolve().parent.parent / "data" / "daily"
    run_dir = Path(__file__).resolve().parent.parent / "research" / "mr-v1-connors-rsi2" / "artifacts" / "trial1"
    run_dir.mkdir(parents=True, exist_ok=True)

    tune_dir, test_dir = prepare_data(str(root))
    tune_data = load_data(Config(data_path=tune_dir))
    print(f"TUNE instruments: {sorted(tune_data)}")

    frozen, results = tune(tune_data)
    import pandas as pd
    pd.DataFrame([{**r["params"], "n_trades": r["n_trades"],
                   "profit_factor": r["profit_factor"], "total_return": r["total_return"],
                   "sharpe": r["sharpe"]} for r in results]).to_csv(run_dir / "tune_grid.csv", index=False)
    (run_dir / "frozen_params.json").write_text(json.dumps(frozen, indent=2))
    print(f"FROZEN params: {frozen}")

    # --- single test-set run (touch once) ---
    test_data = load_data(Config(data_path=test_dir))
    print(f"TEST instruments: {sorted(test_data)}")
    pooled, per, bt, cfg = evaluate(test_data, frozen)
    cfg.figures_dir = str(run_dir)
    save_outputs(bt, cfg)

    v = verdict(pooled, per)
    lines = [
        "# MR Index-Swing -- cross-instrument test result",
        f"Frozen params (tuned on EP+YM): {frozen}",
        f"Test set: {sorted(test_data)}  cost_bps={COST_BPS}/side",
        "",
        f"Aggregate trades : {pooled['n_trades']}   (criterion: >= 100)",
        f"Pooled PF        : {pooled['profit_factor']:.2f}   (criterion: > 1.0)",
        "Per-instrument   : " + ", ".join(f"{s}(n={v2['n']}, PF={v2['pf']:.2f})" for s, v2 in per.items()),
        f"CAGR {pooled['cagr']:+.2%} | MaxDD {pooled['max_drawdown']:.2%} | Sharpe {pooled['sharpe']:.2f} (context only)",
        "",
        f"VERDICT: {'PASS -- supported' if v['PASS'] else 'FAIL -- not supported'}",
    ]
    report = "\n".join(lines)
    (run_dir / "test_report.txt").write_text(report)
    (run_dir / "README.md").write_text(report + "\n")
    print("\n" + report)
    return 0


def run_selftest() -> int:
    import pandas as pd  # noqa: F401  (import kept parallel to engine selftest style)
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    check("grid has 27 combos", len(param_grid()) == 27)
    check("grid combos unique", len({tuple(sorted(p.items())) for p in param_grid()}) == 27)

    # Leakage guard: prepare into a temp root, then load_data(tune_dir) must
    # yield ONLY tune symbols -- never a test symbol.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tune_dir, test_dir = prepare_data(tmp)
        tune_syms = set(load_data(Config(data_path=tune_dir)).keys())
        check("tune load has only tune symbols", tune_syms == {"EP", "YM"})
        check("no test symbol leaked into tune", tune_syms.isdisjoint({"NQ", "RTY", "DAX"}))

    # Selection: synthetic results where a low-trade combo has the highest PF
    # must be rejected by the min-trades guard.
    fake = [
        {"n_trades": 5, "profit_factor": 9.0, "total_return": 1.0, "params": {"x": "few"}},
        {"n_trades": 40, "profit_factor": 1.5, "total_return": 1.0, "params": {"x": "many"}},
    ]
    eligible = [r for r in fake if r["n_trades"] >= MIN_TRADES]
    best = max(eligible, key=lambda r: (r["profit_factor"], r["n_trades"], r["total_return"]))
    check("min-trades guard rejects thin high-PF combo", best["params"]["x"] == "many")

    passing = verdict({"n_trades": 120, "profit_factor": 1.4},
                      {"NQ": {"n": 50, "pf": 1.3}, "RTY": {"n": 40, "pf": 1.1}, "DAX": {"n": 30, "pf": 0.9}})
    check("verdict PASS when all criteria met", passing["PASS"] is True)
    fail_few = verdict({"n_trades": 80, "profit_factor": 1.4},
                       {"NQ": {"n": 40, "pf": 1.3}, "RTY": {"n": 25, "pf": 1.1}, "DAX": {"n": 15, "pf": 0.9}})
    check("verdict FAIL when <100 trades", fail_few["PASS"] is False)
    fail_broken = verdict({"n_trades": 120, "profit_factor": 1.4},
                          {"NQ": {"n": 50, "pf": 1.3}, "RTY": {"n": 40, "pf": 1.1}, "DAX": {"n": 30, "pf": 0.5}})
    check("verdict FAIL when an instrument is broken (PF<0.8)", fail_broken["PASS"] is False)

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else main())
