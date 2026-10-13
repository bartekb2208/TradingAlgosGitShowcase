"""vr_agreement.py — the offline detector and the engine strategy must agree.

Both drive the same VRStateMachine. If they disagree on structure count for a
session, one driver is feeding the FSM wrong (event ordering, book snapshot
alignment, aggressor sign) and every downstream number is suspect. This is the
check that makes "shared logic" a fact rather than an intention.

Exits non-zero on disagreement.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import argparse
import sys

from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

import vr_data as vd
from vr_core import VRConfig
from vr_detect import detect_session
from vr_strategy import VR_ENGINE_VERSION, VRStrategy

sys.path.insert(0, str(vd.ROOT / "python" / "v2"))
from v2_core import load_trade_ticks          # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline detector vs engine strategy")
    ap.add_argument("--session", default="2026-07-10")
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--tolerance", type=int, default=2,
                    help="allowed |offline - engine| structure-count difference")
    a = ap.parse_args(argv)
    cfg = VRConfig()

    offline = detect_session(a.symbol, a.session, cfg)

    loader = DatabentoDataLoader()
    alld = loader.from_dbn_file(str(vd.find_mbo_file(a.session)),
                                price_precision=2, as_legacy_cython=True)
    contract = vd.front_month_contract(a.symbol, a.session, alld)
    inst = vd.build_instrument(contract)
    deltas = [d for d in alld if str(d.instrument_id) == str(inst.id)]
    del alld
    trades = load_trade_ticks(str(vd.L2 / f"{a.symbol}_{a.session}_trades.parquet"), inst.id)

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("VR-AGREE"), logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(venue=Venue("GLBX"), oms_type=OmsType.NETTING,
                     account_type=AccountType.MARGIN, base_currency=USD,
                     starting_balances=[Money(1_000_000, USD)], book_type=BookType.L3_MBO,
                     trade_execution=True, queue_position=True,
                     fee_model=PerContractFeeModel(Money(cfg.commission_per_side, USD)))
    engine.add_instrument(inst)
    engine.add_data(deltas)
    engine.add_data(trades)
    strat = VRStrategy(inst, cfg)
    engine.add_strategy(strat)
    engine.run()

    diff = abs(len(offline) - strat.n_armed)
    ok = diff <= a.tolerance
    net = sum(t["realized_pnl_usd"] for t in strat.closed_positions)

    print("=" * 66)
    print(f"VR LAYER AGREEMENT — {a.symbol} {a.session}   engine v{VR_ENGINE_VERSION}")
    print("=" * 66)
    print(f"  offline detector structures : {len(offline)}")
    print(f"  engine strategy  ARMED      : {strat.n_armed}")
    print(f"  difference                  : {diff} (tolerance {a.tolerance})")
    print(f"  -> {'AGREE' if ok else 'DISAGREE — investigate before trusting any VR number'}")
    print("-" * 66)
    print(f"  entries submitted / filled  : {strat.n_entries} / {strat.n_fills}")
    print(f"  round trips                 : {len(strat.closed_positions)}")
    print(f"  net realized PnL            : ${net:+,.2f}")
    if strat.closed_positions:
        gross = net + sum(t["commission_usd"] for t in strat.closed_positions)
        print(f"  gross ticks/trade           : "
              f"{gross / 12.5 / len(strat.closed_positions):+.4f}   (breakeven +0.40)")
    print("=" * 66)
    engine.dispose()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
