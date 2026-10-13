"""run_backtest.py — run a V2 strategy on one or more sessions.

Reloads each session's MBO as Cython deltas (the manual engine path needs Cython),
filters to the front-month, feeds the verified L2 trade tape as TradeTicks, and runs
a Nautilus BacktestEngine with an L3_MBO queue-aware venue and real CME commission.

Engine is FROZEN at v2_core.V2_ENGINE_VERSION — see research/v2-order-book/ENGINE_FROZEN.md. Every
artifact carries the version and the full config so a result can be reproduced
and so results from different engine versions are never silently compared.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

import data_pipeline as dp
from v2_core import NS, V2_ENGINE_VERSION, V2Config, load_trade_ticks
from v2MeanReversion_conjunction import ConjunctionStrategy
from v2MeanReversion_hawkes import HawkesStrategy

ROOT = Path(__file__).resolve().parent.parent.parent
L2 = ROOT / "data" / "L2"
OUT = ROOT / "research" / "v2-order-book" / "artifacts"
STRATS = {"conjunction": ConjunctionStrategy, "hawkes": HawkesStrategy}


def run_session(symbol: str, session: str, strat_name: str, cfg: V2Config) -> dict:
    """One session end-to-end. Returns a result dict; raises if the tape is missing."""
    tape = L2 / f"{symbol}_{session}_trades.parquet"
    if not tape.exists():
        raise FileNotFoundError(
            f"no trade tape for {session} at {tape}. The MBO loader is book-only, so "
            f"without the L2 tape there are no TradeTicks and the strategy cannot signal.")

    path = dp.find_mbo_file(session)
    loader = DatabentoDataLoader()
    all_deltas = loader.from_dbn_file(str(path), price_precision=2, as_legacy_cython=True)
    contract = dp.front_month_contract(symbol, session, all_deltas)
    instrument = dp.build_instrument(contract)
    want = str(instrument.id)
    deltas = [d for d in all_deltas if str(d.instrument_id) == want]
    trades = load_trade_ticks(str(tape), instrument.id)

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("V2-001"), logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(
        venue=Venue("GLBX"), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
        base_currency=USD, starting_balances=[Money(1_000_000, USD)],
        book_type=BookType.L3_MBO, trade_execution=True, queue_position=True,
        # Real per-contract commission. Without this the venue charges nothing and
        # every PnL figure is gross — which for a 1-tick-scale edge is meaningless.
        fee_model=PerContractFeeModel(Money(cfg.commission_per_side, USD)))
    engine.add_instrument(instrument)
    engine.add_data(deltas)
    engine.add_data(trades)
    strat = STRATS[strat_name](instrument, cfg)
    engine.add_strategy(strat)
    engine.run()

    # Round trips come from the strategy's captured PositionClosed events, NOT
    # from engine.cache.positions_closed(): under NETTING the cache reuses one
    # PositionId per instrument and reports a single closed position regardless
    # of how many round trips occurred. Using the cache silently reduces every
    # aggregate to the last trade.
    fills = [dict(f, session=session) for f in strat.closed_positions]
    net = sum(f["realized_pnl_usd"] for f in fills)
    wins = sum(1 for f in fills if f["realized_pnl_usd"] > 0)
    gross_win = sum(f["realized_pnl_usd"] for f in fills if f["realized_pnl_usd"] > 0)
    gross_loss = -sum(f["realized_pnl_usd"] for f in fills if f["realized_pnl_usd"] < 0)
    holds = sorted(f["hold_ms"] for f in fills if f["hold_ms"] is not None)

    r = {
        "session": session, "contract": contract, "strategy": strat_name,
        "deltas": len(deltas), "trades_fed": len(trades),
        "entries_signaled": strat.n_entries,
        "entry_fills": strat.n_fills,
        "entry_cancels": strat.n_entry_cancels,
        "timeout_exits": strat.n_timeout_exits,
        "blocked_ticks_rth": strat.blocked_ticks_rth,
        "blocked_ticks_cooldown": strat.blocked_ticks_cooldown,
        "blocked_ticks_spread": strat.blocked_ticks_spread,
        "round_trips": len(fills), "wins": wins,
        "exits_by_target": sum(1 for f in fills if f["exit_reason"] == "target"),
        "exits_by_timeout": sum(1 for f in fills if f["exit_reason"] == "timeout"),
        "median_hold_ms": holds[len(holds) // 2] if holds else 0.0,
        "net_pnl_usd": net,
        "commission_usd": sum(f["commission_usd"] for f in fills),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "fills": fills,
        "entry_log": strat.entry_log,
    }
    engine.dispose()
    return r


def _write_artifacts(tag: str, cfg: V2Config, results: list[dict], agg: dict) -> Path:
    d = OUT / tag
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({
        "engine_version": V2_ENGINE_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "config": cfg.to_dict(),
        "sessions": [r["session"] for r in results],
    }, indent=2, default=str))
    fills = [f for r in results for f in r["fills"]]
    if fills:
        with (d / "trades.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(fills[0]))
            w.writeheader()
            w.writerows(fills)
    with (d / "per_session.csv").open("w", newline="") as fh:
        cols = [k for k in results[0] if k not in ("fills", "entry_log")]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in results:
            w.writerow({k: r[k] for k in cols})
    (d / "report.txt").write_text(_format_report(cfg, results, agg))
    return d


def _format_report(cfg: V2Config, results: list[dict], agg: dict) -> str:
    L = ["=" * 68,
         f"  V2 {results[0]['strategy']}   engine v{V2_ENGINE_VERSION}",
         f"  sessions: {len(results)}  ({results[0]['session']} .. {results[-1]['session']})",
         "=" * 68,
         f"  exit_mode         : {cfg.exit_mode}",
         f"  divergence        : {cfg.divergence_bps} bps",
         f"  cooldown          : {cfg.cooldown_ns / NS:.0f} s",
         f"  RTH only          : {cfg.rth_only} ({cfg.rth_start_et}-{cfg.rth_end_et} ET)",
         f"  commission        : ${cfg.commission_per_side:.2f}/side",
         "-" * 68,
         f"  deltas fed        : {agg['deltas']:,}",
         f"  trades fed        : {agg['trades_fed']:,}",
         f"  entries signaled  : {agg['entries_signaled']:,}",
         f"  entries FILLED    : {agg['entry_fills']:,}   "
         f"({100.0 * agg['entry_fills'] / max(agg['entries_signaled'], 1):.1f}% fill rate)",
         f"  entries cancelled : {agg['entry_cancels']:,}  (unfilled after {cfg.entry_ttl_ns / 1e6:.0f} ms)",
         f"  timeout exits     : {agg['timeout_exits']:,}",
         f"  blocked ticks (rth/cool/spread): {agg['blocked_ticks_rth']:,} / "
         f"{agg['blocked_ticks_cooldown']:,} / {agg['blocked_ticks_spread']:,}   [duty cycle, NOT suppressed signals]",
         f"  ROUND TRIPS       : {agg['round_trips']:,}  (wins {agg['wins']:,}, "
         f"{100.0 * agg['wins'] / max(agg['round_trips'], 1):.1f}%)",
         f"  exits target/timeout: {agg['exits_by_target']:,} / {agg['exits_by_timeout']:,}",
         f"  commission paid   : ${agg['commission_usd']:,.2f}",
         f"  NET realized PnL  : ${agg['net_pnl_usd']:+,.2f}",
         f"  profit factor     : {agg['profit_factor']:.3f}",
         "=" * 68]
    if agg["round_trips"] < 100:
        L.append(f"  NOTE: {agg['round_trips']} trades — below the 100-trade")
        L.append("        representativeness bar this project uses. Not a verdict.")
        L.append("=" * 68)
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a V2 strategy on one or more sessions")
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--session", help="YYYY-MM-DD (single session)")
    ap.add_argument("--sessions", nargs="+", help="several YYYY-MM-DD sessions")
    ap.add_argument("--strategy", default="conjunction", choices=list(STRATS))
    ap.add_argument("--exit-mode", choices=["convergence", "fixed_ticks"], default=None,
                    help="override the frozen default (convergence)")
    ap.add_argument("--no-rth", action="store_true", help="disable the RTH signal gate")
    ap.add_argument("--cooldown-s", type=float, default=None, help="override event spacing (s)")
    ap.add_argument("--tag", default=None, help="artifact directory name under research/v2-order-book/artifacts/")
    a = ap.parse_args(argv)

    sessions = a.sessions or ([a.session] if a.session else None)
    if not sessions:
        ap.error("provide --session YYYY-MM-DD or --sessions A B C")

    cfg = V2Config()
    if a.exit_mode:
        cfg = replace(cfg, exit_mode=a.exit_mode)
    if a.no_rth:
        cfg = replace(cfg, rth_only=False)
    if a.cooldown_s is not None:
        cfg = replace(cfg, cooldown_ns=int(a.cooldown_s * NS))

    results = [run_session(a.symbol, s, a.strategy, cfg) for s in sorted(sessions)]

    sums = ["deltas", "trades_fed", "entries_signaled", "entry_fills", "entry_cancels",
            "timeout_exits", "blocked_ticks_rth", "blocked_ticks_cooldown",
            "blocked_ticks_spread", "round_trips", "wins", "exits_by_target",
            "exits_by_timeout", "net_pnl_usd", "commission_usd"]
    agg = {k: sum(r[k] for r in results) for k in sums}
    gw = sum(f["realized_pnl_usd"] for r in results for f in r["fills"] if f["realized_pnl_usd"] > 0)
    gl = -sum(f["realized_pnl_usd"] for r in results for f in r["fills"] if f["realized_pnl_usd"] < 0)
    agg["profit_factor"] = (gw / gl) if gl > 0 else float("inf")

    report = _format_report(cfg, results, agg)
    print(report, end="")
    tag = a.tag or f"V2_{a.strategy}_{sessions[0]}_{len(sessions)}sess"
    print(f"  artifacts -> {_write_artifacts(tag, cfg, results, agg)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
