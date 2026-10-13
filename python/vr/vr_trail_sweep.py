"""vr_trail_sweep.py — how much room does the trail need to breathe?

Run A showed scalp_trail cut avg loss 76% and max drawdown 69%, but strangled
winners down to +2.6 ticks at trail = 1.5x the consolidation range. That is the
same failure recorded for OrbFirstMoveBot in research/orb-first-move/ ("503/505 exits by trail").
This sweeps the multiplier.

SESSION EXCLUSION: 2026-07-10 is dropped on an OBJECTIVE DATA-QUALITY criterion
declared here -- >5% of L3 book updates crossed (ask <= bid). Measured: 82.8% on
07-10 vs 0.3% on 07-08 and 07-13. It is excluded because its order book cannot be
reconstructed, NOT because of its P&L.

Writes to research/v-reversal/artifacts/runs/A3_trail_sweep_fixed/.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np

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
from vr_strategy import VR_ENGINE_VERSION, VRStrategy

sys.path.insert(0, str(vd.ROOT / "python" / "v2"))
from v2_core import load_trade_ticks          # noqa: E402

OUT = vd.ROOT / "research" / "v-reversal" / "artifacts" / "runs" / "A3_trail_sweep_fixed"
CROSSED_BOOK_MAX_PCT = 5.0
EXCLUDED = {"2026-07-10": "82.8% of L3 book updates crossed (ask <= bid)"}
SESSIONS = [s for s in vd.EXPLORATORY if s not in EXCLUDED]

MULTS = [1.5, 3.0, 5.0, 8.0, 12.0]


def stats(tr):
    if not tr:
        return dict(n=0)
    pnl = np.array([t["realized_pnl_usd"] for t in tr], float)
    comm = np.array([t["commission_usd"] for t in tr], float)
    gross = pnl + comm
    w, l = pnl[pnl > 0], pnl[pnl < 0]
    eq = np.cumsum(pnl)
    dd = float(-(eq - np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]).min())
    return dict(n=len(tr), win_pct=100 * (pnl > 0).mean(),
                gross_tk=gross.sum() / 12.5 / len(tr), net=pnl.sum(),
                avg_win=w.mean() if len(w) else 0.0, avg_loss=l.mean() if len(l) else 0.0,
                pf=(w.sum() / -l.sum()) if len(l) else float("inf"), maxdd=dd,
                med_hold_s=float(np.median([t["hold_ms"] for t in tr if t["hold_ms"]]) / 1000)
                if any(t["hold_ms"] for t in tr) else 0.0)


acc = {m: [] for m in MULTS}
fills, forced = {}, {}
engine = None
for s in SESSIONS:
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
            trader_id=TraderId("TRL-001"), logging=LoggingConfig(log_level="ERROR")))
        engine.add_venue(venue=Venue("GLBX"), oms_type=OmsType.NETTING,
                         account_type=AccountType.MARGIN, base_currency=USD,
                         starting_balances=[Money(1_000_000, USD)],
                         book_type=BookType.L3_MBO, trade_execution=True,
                         queue_position=True,
                         fee_model=PerContractFeeModel(Money(2.50, USD)))
        engine.add_instrument(inst)
    else:
        engine.reset(); engine.clear_data(); engine.clear_strategies()
        try:
            engine.add_instrument(inst)
        except Exception:
            pass
    engine.add_data(deltas); engine.add_data(trades)
    for m in MULTS:
        engine.reset(); engine.clear_strategies()
        st = VRStrategy(inst, replace(VRConfig(), trail_cons_mult=m))
        engine.add_strategy(st); engine.run()
        acc[m].extend(dict(t, session=s) for t in st.closed_positions)
        fills[m] = fills.get(m, 0) + st.n_fills
        forced[m] = forced.get(m, 0) + st.n_forced_flat
    print(f"  {s} done ({time.time()-t0:.0f}s)", flush=True)
if engine:
    engine.dispose()

L = ["=" * 92,
     f"  VR TRAIL SWEEP — trail room as a multiple of the consolidation range",
     f"  engine v{VR_ENGINE_VERSION}   sessions: {len(SESSIONS)} "
     f"(excluded {list(EXCLUDED)} — data quality, not P&L)",
     "=" * 92,
     f"{'trail':>7} {'fills':>6} {'n':>4} {'forced':>7} {'win%':>6} {'gross tk/tr':>12} "
     f"{'net $':>10} {'avgWin':>9} {'avgLoss':>9} {'PF':>7} {'maxDD':>9} {'hold s':>8}"]
L.append("-" * 92)
res = {}
for m in MULTS:
    d = stats(acc[m]); res[str(m)] = d
    if not d["n"]:
        L.append(f"{m:6.1f}x {0:4d}   (no trades)"); continue
    L.append(f"{m:6.1f}x {fills.get(m,0):6d} {d['n']:4d} {forced.get(m,0):7d} "
             f"{d['win_pct']:5.1f}% {d['gross_tk']:12.3f} "
             f"{d['net']:10.2f} {d['avg_win']:9.2f} {d['avg_loss']:9.2f} "
             f"{d['pf']:7.3f} {d['maxdd']:9.2f} {d['med_hold_s']:8.1f}")
L += ["=" * 92,
      "  SURVIVORSHIP FIX ACTIVE: positions still open at session end are force-closed",
      "  at the last bid and counted ('forced' column). Previously they vanished from",
      "  the stats entirely, biasing results toward whichever config held longest.",
      "  fills MUST equal n for the sample to be complete.",
      "  Breakeven needs gross > +0.40 ticks/trade.",
      "  EXCLUSION CRITERION (objective, declared before the run):",
      f"    drop any session with > {CROSSED_BOOK_MAX_PCT}% of L3 book updates crossed.",
      f"    {EXCLUDED}",
      "=" * 92]
txt = "\n".join(L)
print("\n" + txt)
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "report.txt").write_text(txt + "\n")
(OUT / "stats.json").write_text(json.dumps(
    {"engine_version": VR_ENGINE_VERSION,
     "generated_utc": datetime.now(timezone.utc).isoformat(),
     "sessions": SESSIONS, "excluded": EXCLUDED,
     "exclusion_criterion": f">{CROSSED_BOOK_MAX_PCT}% crossed L3 book updates",
     "results": res}, indent=2, default=str))
print(f"\nartifacts -> {OUT}")
