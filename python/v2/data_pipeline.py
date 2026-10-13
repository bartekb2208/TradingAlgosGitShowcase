"""data_pipeline.py — one session's raw MBO -> front-month outright -> Nautilus catalog.

Phase 1 of the V2 build (see research/v2-order-book/ROADMAP.md). Reads a single
Databento GLBX MDP3 MBO file, isolates the front-month OUTRIGHT contract (parent
symbology puts every contract + calendar spread on one tape), constructs the
Nautilus instrument (the batch has no DEFINITION file), and writes the instrument
+ its OrderBookDeltas into a ParquetDataCatalog the backtest reads.

Front-month per RTH session is taken from the existing L2 manifest
(data/L2/_manifest_{SYM}.json); for non-manifest sessions (e.g. a Sunday) it
falls back to the highest-count outright in the file.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from datetime import date, datetime, timedelta, timezone

from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

ROOT = Path(__file__).resolve().parent.parent.parent      # repository root
L3 = ROOT / "data" / "L3"
L2 = ROOT / "data" / "L2"
CATALOG = Path(__file__).resolve().parent / "catalog"

# CME month codes <-> calendar month
MONTH_CODE = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
              "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}

# CME contract specs. `multiplier` is the DOLLAR value of one full index point —
# it is what converts engine PnL from points into money, so it must be right or
# every risk/PnL number downstream is wrong by that factor.
#   ES: 0.25 tick x $50 = $12.50/tick        NQ: 0.25 tick x $20 = $5.00/tick
# NOTE: TestInstrumentProvider.es_future() hardcodes multiplier=1. It is a test
# fixture, not a contract spec — do not use it for anything that reports PnL.
CONTRACT_SPECS = {
    "ES": {"multiplier": 50, "tick": "0.25", "precision": 2, "asset_class": AssetClass.INDEX},
    "NQ": {"multiplier": 20, "tick": "0.25", "precision": 2, "asset_class": AssetClass.INDEX},
}


def _third_friday(year: int, month: int) -> date:
    """CME equity-index futures expire on the third Friday of the contract month."""
    d = date(year, month, 1)
    fridays = 0
    while True:
        if d.weekday() == 4:
            fridays += 1
            if fridays == 3:
                return d
        d += timedelta(days=1)


def find_mbo_file(session: str) -> Path:
    """Locate glbx-mdp3-YYYYMMDD.mbo.dbn.zst for a YYYY-MM-DD session."""
    ymd = session.replace("-", "")
    hits = sorted(L3.glob(f"*/glbx-mdp3-{ymd}.mbo.dbn.zst"))
    if not hits:
        raise FileNotFoundError(f"no MBO file for {session} under {L3}")
    return hits[0]


def build_instrument(contract: str) -> FuturesContract:
    """Nautilus FuturesContract for a contract code like 'ESM6' (single-digit CME year).

    Built from CONTRACT_SPECS rather than the Nautilus test kit so the contract
    multiplier is the real one ($50/point on ES). The batch carries no DEFINITION
    file, so the instrument has to be constructed here.
    """
    root = contract[:2]
    spec = CONTRACT_SPECS.get(root)
    if spec is None:
        raise NotImplementedError(
            f"no contract spec for {root!r} (contract {contract!r}); "
            f"known roots: {sorted(CONTRACT_SPECS)}")

    month = MONTH_CODE[contract[2]]
    year = 2020 + int(contract[3:])              # '6' -> 2026 (valid this decade)

    expiration = datetime.combine(_third_friday(year, month),
                                  datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=14, minutes=30)
    activation = expiration - timedelta(days=365)   # listed well before the session under test

    sym = Symbol(contract)
    return FuturesContract(
        instrument_id=InstrumentId(symbol=sym, venue=Venue("GLBX")),
        raw_symbol=sym,
        asset_class=spec["asset_class"],
        exchange="XCME",
        currency=USD,
        price_precision=spec["precision"],
        price_increment=Price.from_str(spec["tick"]),
        multiplier=Quantity.from_int(spec["multiplier"]),
        lot_size=Quantity.from_int(1),
        underlying=root,
        activation_ns=int(activation.timestamp()) * 1_000_000_000,
        expiration_ns=int(expiration.timestamp()) * 1_000_000_000,
        ts_event=int(activation.timestamp()) * 1_000_000_000,
        ts_init=int(activation.timestamp()) * 1_000_000_000,
    )


def front_month_contract(symbol: str, session: str, deltas: list) -> str:
    """Front-month outright contract code (e.g. 'ESM6'): L2 manifest, else most-common outright."""
    manifest = L2 / f"_manifest_{symbol}.json"
    if manifest.exists():
        for row in json.loads(manifest.read_text()):
            if row["date"] == session:
                return row["contract"]
    counts = Counter(str(d.instrument_id) for d in deltas if "-" not in str(d.instrument_id))
    if not counts:
        raise ValueError("no outright instrument found in file")
    return counts.most_common(1)[0][0].split(".")[0]


def build_catalog(symbol: str, session: str) -> dict:
    """Load one session, filter to the front-month outright, write to the catalog."""
    path = find_mbo_file(session)
    loader = DatabentoDataLoader()
    # Nautilus's MBO loader is BOOK-ONLY (rejects include_trades=True) and returns
    # pyo3 OrderBookDelta objects (as_legacy_cython=False, for fast catalog writes).
    # The trade tape (for the V-reversal signal) is sourced from the verified L2
    # parquet (mbo_to_l2 output) in Phase 2, not here.
    deltas = loader.from_dbn_file(str(path), price_precision=2, as_legacy_cython=False)

    contract = front_month_contract(symbol, session, deltas)
    instrument = build_instrument(contract)
    want = str(instrument.id)
    front_d = [d for d in deltas if str(d.instrument_id) == want]

    CATALOG.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(CATALOG))
    catalog.write_data([instrument])
    catalog.write_data(front_d)

    back = catalog.instruments()          # read back to confirm persistence
    return {
        "session": session, "file": path.name, "contract": contract, "instrument": want,
        "total_deltas": len(deltas), "front_deltas": len(front_d),
        "catalog_instruments": [str(i.id) for i in back],
    }


def _raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    inst = build_instrument("ESM6")
    check("ESM6 instrument id matches deltas", str(inst.id) == "ESM6.GLBX")
    check("ESM6 tick is 0.25", str(inst.price_increment) == "0.25")
    check("ESU6 -> September 2026", build_instrument("ESU6").id.symbol.value == "ESU6")
    # The 50x bug: the test-kit fixture reports multiplier 1, which silently
    # divides every PnL/margin number by 50. Guard it so it cannot come back.
    check("ES multiplier is 50 (NOT the test-kit's 1)", int(inst.multiplier) == 50)
    check("ES tick value is $12.50",
          abs(float(inst.price_increment) * int(inst.multiplier) - 12.50) < 1e-9)
    nq = build_instrument("NQM6")
    check("NQ multiplier is 20", int(nq.multiplier) == 20)
    check("NQ tick value is $5.00",
          abs(float(nq.price_increment) * int(nq.multiplier) - 5.00) < 1e-9)
    check("ESM6 expires 3rd Friday Jun 2026 (2026-06-19)", _third_friday(2026, 6).isoformat() == "2026-06-19")
    check("unknown root raises", _raises(lambda: build_instrument("CLM6"), NotImplementedError))
    check("MBO file discovery finds 2026-06-08", find_mbo_file("2026-06-08").exists())
    man = L2 / "_manifest_ES.json"
    if man.exists():
        got = front_month_contract("ES", "2026-06-08", [])
        check("manifest front-month 2026-06-08 == ESM6", got == "ESM6")

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MBO session -> Nautilus catalog (V2 phase 1)")
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--session", help="YYYY-MM-DD")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return run_selftest()
    if not a.session:
        ap.error("--session YYYY-MM-DD is required (or use --selftest)")

    r = build_catalog(a.symbol, a.session)
    print(f"session {r['session']}  file {r['file']}")
    print(f"  front-month: {r['contract']} ({r['instrument']})")
    print(f"  deltas: {r['front_deltas']:,} front-month  /  {r['total_deltas']:,} total")
    print("  trades: MBO loader is book-only -> trade tape comes from the L2 parquet (Phase 2)")
    print(f"  catalog instruments: {r['catalog_instruments']}")
    print(f"  catalog -> {CATALOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
