"""fvt_engine.py — FVT V1 bar loop. Signals in, trade records out.

Fill resolution is hand-written rather than inherited from a matching engine, so
the one assumption that matters — what happens when a single bar contains both
brackets — is explicit, pessimistic, and counted.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import sys

import pandas as pd

from fvt_core import FVTConfig, FVTDetector, hhmm_to_minutes

TRADE_COLUMNS = [
    "session", "symbol", "window", "phase", "structure", "attempt_n", "direction",
    "signal_ts", "entry_ts", "entry_px", "exit_ts", "exit_px", "exit_reason",
    "fair_value", "atr", "tier", "sl_points", "tp_points", "contracts",
    "gross_usd", "commission_usd", "net_usd", "bars_held", "ambiguous_bar",
    "gapped_fill",
]


def resolve_bar(direction: int, o: float, h: float, l: float,
                stop: float, target: float) -> tuple[str | None, float, bool]:
    """(exit_reason, exit_price, ambiguous) for one bar of an open position.

    A stop is a stop-MARKET order: gapping through it fills at the open, worse.
    A target is a LIMIT: gapping through it still fills at the limit price.
    When one bar contains both, we cannot know the order from OHLC alone, so we
    take the stop and flag the trade.
    """
    if direction > 0:
        if o <= stop:
            return "stop", o, False
        hit_stop, hit_target = l <= stop, h >= target
        if hit_stop and hit_target:
            return "stop", stop, True
        if hit_stop:
            return "stop", stop, False
        if hit_target:
            return "target", target, False
        return None, 0.0, False
    if o >= stop:
        return "stop", o, False
    hit_stop, hit_target = h >= stop, l <= target
    if hit_stop and hit_target:
        return "stop", stop, True
    if hit_stop:
        return "stop", stop, False
    if hit_target:
        return "target", target, False
    return None, 0.0, False


def arbitrate_minute(prints, direction: int, stop: float,
                     target: float) -> str | None:
    """Walk one minute's actual trade prints in order and report which bracket
    was touched first. This is the only way to settle an ambiguous bar."""
    for px in prints:
        if direction > 0:
            if px <= stop:
                return "stop"
            if px >= target:
                return "target"
        else:
            if px >= stop:
                return "stop"
            if px <= target:
                return "target"
    return None


def arbitrate_trades(trades: list[dict], usd_per_point: float,
                     symbol: str = "NQ") -> dict:
    """Re-resolve every ambiguous trade against the tape. Returns an agreement
    summary for falsifier F6. NQ only — it is the only tape we hold.

    Compares the WHOLE net P&L series, not just the ambiguous subset, so
    `sign_agrees` answers the question that matters: does the pessimistic
    assumption change the sign of the result?
    """
    import fvt_data as fd

    net_pessimistic = sum(t["net_usd"] for t in trades)
    amb = [t for t in trades if t["ambiguous_bar"]]
    if not amb:
        return {"ambiguous": 0, "arbitrated": 0, "unarbitrated": 0,
                "flipped_to_target": 0, "net_usd_pessimistic": net_pessimistic,
                "net_usd_arbitrated": net_pessimistic, "sign_agrees": True}

    by_session: dict[str, pd.Series] = {}
    flipped, arbitrated, delta = 0, 0, 0.0
    for t in amb:
        s = t["session"]
        if s not in by_session:
            raw = pd.read_parquet(fd.L2 / f"{symbol}_{s}_trades.parquet",
                                  columns=["ts", "price"])
            idx = pd.to_datetime(raw["ts"], unit="ns", utc=True).dt.tz_convert(fd.NY)
            # kind="stable": this is the one place PRINT ORDER is the entire
            # point (arbitrate_minute walks the prints to see which bracket
            # touched first) -- the default quicksort gives no such guarantee
            # on ties.
            by_session[s] = raw.set_index(idx)["price"].sort_index(kind="stable")
        stop = t["entry_px"] - t["direction"] * t["sl_points"]
        target = t["entry_px"] + t["direction"] * t["tp_points"]
        minute = t["exit_ts"]
        # Half-open interval: [minute, minute+1min) — exclude the right endpoint
        # to avoid pulling prints from the next minute.
        minute_end = minute + pd.Timedelta(minutes=1)
        prints = by_session[s][(by_session[s].index >= minute) & (by_session[s].index < minute_end)]
        verdict = arbitrate_minute(prints, t["direction"], stop, target)
        if verdict is None:
            continue
        arbitrated += 1
        if verdict == "target":
            flipped += 1
            # Straight recomputation at the target price — no rescaling tricks.
            gross_target = (t["direction"] * (target - t["entry_px"])
                            * t["contracts"] * usd_per_point)
            delta += (gross_target - t["commission_usd"]) - t["net_usd"]
    net_arbitrated = net_pessimistic + delta
    return {"ambiguous": len(amb), "arbitrated": arbitrated,
            "unarbitrated": len(amb) - arbitrated,
            "flipped_to_target": flipped,
            "net_usd_pessimistic": net_pessimistic,
            "net_usd_arbitrated": net_arbitrated,
            "sign_agrees": (net_pessimistic >= 0) == (net_arbitrated >= 0)}


def run_backtest(bars, cfg: FVTConfig, symbol: str) -> tuple[list[dict], dict]:
    signals, counters = FVTDetector(cfg).run(bars)
    by_ts = {s.ts: s for s in signals}
    counters.update({"signals": len(signals), "ambiguous_trades": 0,
                     "skipped_in_position": 0, "skipped_no_next_bar": 0})

    trades: list[dict] = []
    index = list(bars.index)
    opens = bars["open"].to_numpy(float)
    highs = bars["high"].to_numpy(float)
    lows = bars["low"].to_numpy(float)
    closes = bars["close"].to_numpy(float)
    flat_min = hhmm_to_minutes(cfg.force_flat)
    flat_h, flat_m = divmod(flat_min, 60)
    slip = cfg.slippage_ticks * cfg.tick_size

    pos: dict | None = None
    pending: object = None

    for i, ts in enumerate(index):
        # 1) fill anything armed by the previous bar
        if pending is not None and pos is None:
            s = by_ts[pending]
            entry = opens[i] + slip * s.direction
            pos = {"sig": s, "entry_ts": ts, "entry_i": i, "entry_px": entry,
                   "stop": entry - s.direction * s.sl_points,
                   "target": entry + s.direction * s.tp_points,
                   "ambiguous": False,
                   # Deadline captured at fill time, not re-derived from each
                   # bar's own minute-of-day (see note below for why). Built
                   # from the FILL bar's date components plus the configured
                   # flat time, freshly localised in the bar's own timezone --
                   # deliberately NOT `ts.normalize() + Timedelta(minutes=...)`.
                   # That form adds an ABSOLUTE duration to a wall-clock
                   # midnight, which is wrong across a DST transition: on a
                   # spring-forward date it lands at 17:00 NY instead of
                   # 16:00 (and 15:00 on a fall-back date), because the clock
                   # skips or repeats an hour between midnight and 16:00 that
                   # a fixed-duration add can't see. Constructing a new
                   # Timestamp from (year, month, day, hour, minute, tz=...)
                   # re-localises against the tz database for THAT date, so
                   # it is correct on both sides of a transition.
                   "flat_at": pd.Timestamp(year=ts.year, month=ts.month, day=ts.day,
                                           hour=flat_h, minute=flat_m, tz=ts.tz)}
        pending = None

        # 2) manage an open position on this bar. The force-flat deadline is
        # checked BEFORE resolve_bar: bar timestamps are bar-OPEN, so a bar at
        # or after the deadline spans wall-clock time entirely AFTER the flat
        # cutoff. Its own high/low must never be allowed to fill a bracket --
        # that would keep the position economically exposed past the cutoff
        # and silently deflate the session_close honesty count.
        #
        # The deadline is an ABSOLUTE timestamp (pos["flat_at"]), not a
        # per-bar `minutes_since_midnight(ts) >= flat_min` test: 220 days in
        # the ES file have no bar at or after 16:00 (a data-export gap), so a
        # bar-presence-dependent test can never fire on those days -- an open
        # position would silently survive into the next available bar, whose
        # minute-of-day is again < flat_min, and get managed as though the
        # deadline had never existed. Comparing against the captured deadline
        # timestamp fires on the first bar of ANY later day, gap or not.
        if pos is not None:
            if ts >= pos["flat_at"]:
                reason, px, amb = "session_close", opens[i], False
            else:
                s = pos["sig"]
                reason, px, amb = resolve_bar(s.direction, opens[i], highs[i], lows[i],
                                              pos["stop"], pos["target"])
            if amb:
                pos["ambiguous"] = True
            if reason is not None:
                trades.append(_close(pos, ts, i, px, reason, cfg, symbol))
                if pos["ambiguous"]:
                    counters["ambiguous_trades"] += 1
                pos = None

        # 3) arm a signal seen on THIS bar for the next bar's open
        if ts in by_ts:
            if pos is not None:
                counters["skipped_in_position"] += 1
            elif i + 1 >= len(index):
                counters["skipped_no_next_bar"] += 1
            else:
                pending = ts

    if pos is not None:                     # data ran out mid-trade: the last
        i = len(index) - 1                  # bar was already evaluated above
        trades.append(_close(pos, index[i], i, closes[i], "session_close", cfg, symbol))
    return trades, counters


def _close(pos: dict, ts, i: int, px: float, reason: str,
           cfg: FVTConfig, symbol: str) -> dict:
    s = pos["sig"]
    gross = s.direction * (px - pos["entry_px"]) * s.contracts * cfg.usd_per_point
    comm = 2.0 * cfg.commission_per_side * s.contracts
    return {
        # Trading day = the ENTRY day, not the exit day. A trade that force-
        # flats on a later calendar day (see the flat_at deadline above) must
        # still key to the day it was actually opened -- `session` is what
        # arbitrate_trades() uses to look up that day's parquet tape, and
        # keying it to the exit day would point a cross-day trade at the
        # wrong (or a nonexistent) session file.
        "session": pos["entry_ts"].normalize().strftime("%Y-%m-%d"), "symbol": symbol,
        "window": s.window, "phase": s.phase, "structure": s.structure,
        "attempt_n": s.attempt_n, "direction": s.direction, "signal_ts": s.ts,
        "entry_ts": pos["entry_ts"], "entry_px": pos["entry_px"],
        "exit_ts": ts, "exit_px": px, "exit_reason": reason,
        "fair_value": s.fair_value, "atr": s.atr, "tier": s.tier,
        "sl_points": s.sl_points, "tp_points": s.tp_points,
        "contracts": s.contracts, "gross_usd": gross, "commission_usd": comm,
        "net_usd": gross - comm, "bars_held": i - pos["entry_i"],
        "ambiguous_bar": pos["ambiguous"],
        # A stop that filled anywhere other than the stop level gapped through it.
        "gapped_fill": reason == "stop" and abs(px - pos["stop"]) > 1e-9,
    }


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    NY = "America/New_York"

    def base_frame(start="2026-04-06 09:00", end="2026-04-06 16:05"):
        idx = pd.date_range(start, end, freq="1min", tz=NY)
        n = len(idx)
        o = [100.0] * n; h = [100.5] * n; lo = [99.5] * n; c = [100.0] * n
        for i in range(n):
            if i % 4 == 0:
                h[i], c[i] = 101.0, 100.8
            elif i % 4 == 2:
                lo[i], c[i] = 99.0, 99.2
        return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c}, index=idx)

    def set_bar(frame, ts_str: str, o: float, h: float, l: float, c: float) -> None:
        frame.loc[pd.Timestamp(ts_str, tz=NY), ["open", "high", "low", "close"]] = [o, h, l, c]

    cfg = FVTConfig(stop_mode="atr_multiple", usd_per_point=50.0)

    # --- Long-side bracket resolution ---------------------------------------
    check("long: bar that reaches only the target exits at the target",
          resolve_bar(+1, o=100.0, h=112.0, l=99.0, stop=95.0, target=110.0)
          == ("target", 110.0, False))
    check("long: bar that reaches only the stop exits at the stop",
          resolve_bar(+1, o=100.0, h=101.0, l=94.0, stop=95.0, target=110.0)
          == ("stop", 95.0, False))
    check("long: a bar containing BOTH resolves to the stop and is flagged",
          resolve_bar(+1, o=100.0, h=112.0, l=94.0, stop=95.0, target=110.0)
          == ("stop", 95.0, True))
    check("long: an untouched bar does not exit",
          resolve_bar(+1, o=100.0, h=105.0, l=98.0, stop=95.0, target=110.0)
          == (None, 0.0, False))
    check("long: a gap through the stop fills at the OPEN, worse than the stop",
          resolve_bar(+1, o=90.0, h=91.0, l=89.0, stop=95.0, target=110.0)
          == ("stop", 90.0, False))
    check("long: a gap through the target still fills at the LIMIT price",
          resolve_bar(+1, o=115.0, h=116.0, l=114.0, stop=95.0, target=110.0)
          == ("target", 110.0, False))

    # --- Short-side bracket resolution (mirrors every long-side branch) -----
    check("short: bar that reaches only the stop exits at the stop",
          resolve_bar(-1, o=100.0, h=106.0, l=99.0, stop=105.0, target=90.0)
          == ("stop", 105.0, False))
    check("short: bar that reaches only the target exits at the target",
          resolve_bar(-1, o=100.0, h=104.0, l=88.0, stop=105.0, target=90.0)
          == ("target", 90.0, False))
    check("short: a bar containing BOTH resolves to the stop and is flagged",
          resolve_bar(-1, o=100.0, h=106.0, l=88.0, stop=105.0, target=90.0)
          == ("stop", 105.0, True))
    check("short: an untouched bar does not exit",
          resolve_bar(-1, o=100.0, h=102.0, l=98.0, stop=105.0, target=90.0)
          == (None, 0.0, False))
    check("short: a gap through the stop fills at the OPEN",
          resolve_bar(-1, o=110.0, h=111.0, l=109.0, stop=105.0, target=90.0)
          == ("stop", 110.0, False))
    check("short: a gap through the target still fills at the LIMIT price",
          resolve_bar(-1, o=85.0, h=86.0, l=84.0, stop=105.0, target=90.0)
          == ("target", 90.0, False))

    # --- End-to-end on a synthetic session -----------------------------------
    frame = base_frame()
    frame.loc[frame.index[35], ["open", "high", "low", "close"]] = [100.0, 110.0, 99.9, 109.8]
    trades, counters = run_backtest(frame, cfg, symbol="TEST")
    check("the synthetic session produces a trade", len(trades) >= 1)
    t = trades[0]
    check("entry is at the bar AFTER the signal, never the signal bar",
          t["entry_ts"] > t["signal_ts"])
    check("entry price is the next bar's open plus slippage",
          abs(t["entry_px"]
              - (float(frame.loc[t["entry_ts"], "open"]) + cfg.slippage_ticks * cfg.tick_size))
          < 1e-9)
    check("exit reason is one of the three legal ones",
          t["exit_reason"] in ("target", "stop", "session_close"))
    check("commission is charged on both sides of every contract",
          abs(t["commission_usd"] - 2 * cfg.commission_per_side * t["contracts"]) < 1e-9)
    check("net is gross minus commission",
          abs(t["net_usd"] - (t["gross_usd"] - t["commission_usd"])) < 1e-9)
    check("every spec'd column is present", set(TRADE_COLUMNS) <= set(t))
    check("ambiguity is counted", "ambiguous_trades" in counters)

    # --- One position at a time: a fixture that genuinely produces TWO trades,
    # so the overlap check has something to compare (a single-trade frame makes
    # `all()` vacuously true and proves nothing). Trade 1 fires in the morning
    # window and is forced to hit its target within minutes by a huge favorable
    # spike (high=112, close pulled back to 100.2 so the spike itself is not a
    # displaced candle and cannot register as a second signal). Trade 2 fires
    # independently in the afternoon window, well after trade 1 closed. ------
    frameA = base_frame()
    set_bar(frameA, "2026-04-06 09:35", 100.0, 110.0, 99.9, 109.8)   # trade-1 signal
    set_bar(frameA, "2026-04-06 09:40", 100.0, 112.0, 99.5, 100.2)   # forces trade-1 to target
    set_bar(frameA, "2026-04-06 14:05", 100.0, 130.0, 99.9, 129.0)   # trade-2 signal
    tradesA, _ = run_backtest(frameA, cfg, symbol="TEST")
    check("the overlap fixture genuinely produces two trades, not one",
          len(tradesA) == 2)
    check("no two trades overlap in time",
          len(tradesA) >= 2
          and all(tradesA[i]["exit_ts"] <= tradesA[i + 1]["entry_ts"]
                  for i in range(len(tradesA) - 1)))

    # --- Force flat: a REAL phase-2 signal that survives untouched to the
    # deadline. The pm window opens flat (fair value 100.0); an O==H ramp (no
    # denominator -> is_displacement always False, so none of it can itself
    # fire a signal) carves a swing low at 102.0, confirmed by 14:05. At 14:20
    # (offset 20 -> phase 2) a bearish displacement candle closes at 101.6:
    # below the 102.0 swing low (a break) but above the 100.0 fair value, so
    # phase_gate_ok's phase-2 "back toward fair value" test passes. The short
    # entry's brackets (~+5.3 stop, ~-7.9 target) are far outside the ordinary
    # ~99.5-101 oscillation that resumes afterward, so nothing fires it early
    # and the position is still open when the loop reaches 16:00. -----------
    frameB = base_frame()
    set_bar(frameB, "2026-04-06 14:00", 100.0, 100.5, 99.8, 100.2)
    set_bar(frameB, "2026-04-06 14:01", 106.0, 106.0, 103.0, 103.0)
    set_bar(frameB, "2026-04-06 14:02", 106.0, 106.0, 104.0, 104.0)
    set_bar(frameB, "2026-04-06 14:03", 106.0, 106.0, 102.0, 102.0)   # swing-low candidate
    set_bar(frameB, "2026-04-06 14:04", 106.0, 106.0, 102.5, 102.5)
    set_bar(frameB, "2026-04-06 14:05", 106.0, 106.0, 102.8, 102.8)   # confirms it
    for m in range(6, 20):
        set_bar(frameB, f"2026-04-06 14:{m:02d}", 106.0, 106.0, 103.0, 103.0)
    set_bar(frameB, "2026-04-06 14:20", 103.0, 103.2, 101.5, 101.6)   # phase-2 signal
    tradesB, _ = run_backtest(frameB, cfg, symbol="TEST")
    check("the force-flat fixture's phase-2 signal actually fires, producing one trade",
          len(tradesB) == 1)
    tb = tradesB[0] if tradesB else None
    check("a position still open at the deadline is force-flat there, reported as "
          "session_close, not carried past 16:00",
          tb is not None and tb["exit_reason"] == "session_close"
          and tb["exit_ts"] == pd.Timestamp("2026-04-06 16:00", tz=NY))

    # --- 16:00 precedence: flatten wins even when the deadline bar's own
    # range would have reached a bracket. Same fixture as above, but the
    # 16:00 bar's low is rigged to 50.0 -- far past the ~91.8 target. If
    # resolve_bar were still consulted on this bar the trade would wrongly
    # exit "target"; the deadline check must short-circuit before that. -----
    frameC = frameB.copy()
    set_bar(frameC, "2026-04-06 16:00", 100.0, 100.5, 50.0, 100.2)
    tradesC, _ = run_backtest(frameC, cfg, symbol="TEST")
    check("the precedence fixture produces exactly one trade", len(tradesC) == 1)
    tc = tradesC[0] if tradesC else None
    check("a deadline bar whose own range would hit a bracket still force-flats at "
          "the bar's open, never at the bracket",
          tc is not None and tc["exit_reason"] == "session_close"
          and abs(tc["exit_px"] - 100.0) < 1e-9)

    # --- Force flat on a spring-forward DST date: same frameB pattern (short,
    # brackets ~+105/-91.8, never touched by the ordinary ~99.5-101
    # oscillation) replayed on 2026-03-08 -- the actual US spring-forward
    # Sunday, when the clock jumps 02:00 -> 03:00 EST->EDT. The buggy form
    # (`ts.normalize() + Timedelta(minutes=960)`) adds an ABSOLUTE 16-hour
    # duration to a midnight that is captured at -05:00, landing at 17:00
    # EDT, not 16:00 -- a full hour late. A huge favorable move is planted at
    # 16:30 (inside that bogus extra hour, low=50.0, far past the ~91.8
    # target): under the bug the position would still read as open at 16:30
    # and resolve_bar would hit the rigged target there; the correct,
    # wall-clock deadline force-flats at 16:00 and never lets the loop reach
    # that bar with the position still open. -------------------------------
    frameF = base_frame(start="2026-03-08 09:00", end="2026-03-08 17:05")
    set_bar(frameF, "2026-03-08 14:00", 100.0, 100.5, 99.8, 100.2)
    set_bar(frameF, "2026-03-08 14:01", 106.0, 106.0, 103.0, 103.0)
    set_bar(frameF, "2026-03-08 14:02", 106.0, 106.0, 104.0, 104.0)
    set_bar(frameF, "2026-03-08 14:03", 106.0, 106.0, 102.0, 102.0)   # swing-low candidate
    set_bar(frameF, "2026-03-08 14:04", 106.0, 106.0, 102.5, 102.5)
    set_bar(frameF, "2026-03-08 14:05", 106.0, 106.0, 102.8, 102.8)   # confirms it
    for m in range(6, 20):
        set_bar(frameF, f"2026-03-08 14:{m:02d}", 106.0, 106.0, 103.0, 103.0)
    set_bar(frameF, "2026-03-08 14:20", 103.0, 103.2, 101.5, 101.6)   # phase-2 signal
    set_bar(frameF, "2026-03-08 16:30", 100.0, 100.5, 50.0, 100.2)    # rigged target hit
    tradesF, _ = run_backtest(frameF, cfg, symbol="TEST")
    check("the DST fixture's phase-2 signal fires, producing one trade",
          len(tradesF) == 1)
    tf = tradesF[0] if tradesF else None
    check("on a spring-forward date the force-flat deadline is wall-clock 16:00 NY, "
          "not 17:00 -- the position never survives to the 16:30 bar where the "
          "rigged bracket sits, so it must exit at 16:00 as session_close",
          tf is not None and tf["exit_reason"] == "session_close"
          and tf["exit_ts"] == pd.Timestamp("2026-03-08 16:00", tz=NY))

    # --- Force flat across a session gap: 220 real ES days have no bar at or
    # after 16:00 at all (a data-export gap), so a bar-presence-dependent
    # deadline test (`minutes_since_midnight(ts) >= flat_min`) never sees a
    # triggering bar on those days -- the position survives into the next
    # available bar, whose minute-of-day is again well under 16:00, and gets
    # managed as though the deadline had never existed. Truncate frameB's day
    # right after the entry fill, well before 16:00 (so day 1 never presents a
    # deadline-eligible bar), then resume trading the NEXT calendar day with
    # the same quiet ~99.5-101 oscillation frameB proved never reaches this
    # short's brackets (stop ~105, target ~91.8). The only thing that can
    # close the trade is the ABSOLUTE deadline captured at fill time.
    frameE = frameB.loc[:pd.Timestamp("2026-04-06 14:30", tz=NY)].copy()
    day2 = base_frame(start="2026-04-07 09:30", end="2026-04-07 09:40")
    frameE = pd.concat([frameE, day2])
    tradesE, _ = run_backtest(frameE, cfg, symbol="TEST")
    te = tradesE[0] if tradesE else None
    check("a position that survives a day with no bar at/after 16:00 is force-flat "
          "on the very first bar of the NEXT session, not silently carried forward "
          "as though the deadline never happened",
          len(tradesE) == 1 and te is not None
          and te["exit_ts"] == pd.Timestamp("2026-04-07 09:30", tz=NY)
          and te["exit_reason"] == "session_close"
          and abs(te["exit_px"] - float(day2.loc[pd.Timestamp("2026-04-07 09:30", tz=NY),
                                                  "open"])) < 1e-9)

    # --- End of data mid-trade: the fallback must exit at the LAST bar's
    # CLOSE, not its open -- that bar was already evaluated by the loop (its
    # own resolve_bar check found no exit), so its open is stale by the time
    # the fallback runs. Truncate the force-flat fixture a few bars after
    # entry, well before 16:00, with the final bar's open and close deliberately
    # different so exiting at the wrong one would be caught. -----------------
    frameD = frameB.copy()
    set_bar(frameD, "2026-04-06 14:25", 100.0, 100.3, 99.5, 99.6)
    frameD = frameD.loc[:pd.Timestamp("2026-04-06 14:25", tz=NY)]
    tradesD, _ = run_backtest(frameD, cfg, symbol="TEST")
    check("data running out mid-trade produces exactly one trade", len(tradesD) == 1)
    td = tradesD[0] if tradesD else None
    check("end-of-data fallback exits at the last bar's CLOSE, not its open",
          td is not None and td["exit_ts"] == pd.Timestamp("2026-04-06 14:25", tz=NY)
          and abs(td["exit_px"] - 99.6) < 1e-9)

    # --- Tick arbitration ---------------------------------------------------
    ticks_stop_first = pd.Series([100.0, 97.0, 94.0, 111.0])
    ticks_tgt_first = pd.Series([100.0, 105.0, 111.0, 94.0])
    check("arbitration says stop when the stop printed first",
          arbitrate_minute(ticks_stop_first, +1, stop=95.0, target=110.0) == "stop")
    check("arbitration says target when the target printed first",
          arbitrate_minute(ticks_tgt_first, +1, stop=95.0, target=110.0) == "target")
    check("arbitration returns None when neither level printed",
          arbitrate_minute(pd.Series([100.0, 101.0]), +1, 95.0, 110.0) is None)
    check("short arbitration reads the levels the other way up",
          arbitrate_minute(pd.Series([100.0, 106.0, 89.0]), -1,
                           stop=105.0, target=90.0) == "stop")
    check("the pessimistic engine assumption and arbitration agree when the "
          "stop really did come first",
          resolve_bar(+1, 100.0, 111.0, 94.0, 95.0, 110.0)[0]
          == arbitrate_minute(ticks_stop_first, +1, 95.0, 110.0))
    check("short arbitration reports target when the target printed first",
          arbitrate_minute(pd.Series([100.0, 95.0, 89.0]), -1,
                           stop=105.0, target=90.0) == "target")

    # --- arbitrate_trades: early-return case ---
    # No ambiguous trades: should return neutral verdict with net sums and sign_agrees=True
    clean_trades = [
        {"net_usd": 150.0, "ambiguous_bar": False},
        {"net_usd": -75.0, "ambiguous_bar": False},
        {"net_usd": 225.0, "ambiguous_bar": False},
    ]
    result = arbitrate_trades(clean_trades, usd_per_point=50.0, symbol="NQ")
    check("arbitrate_trades early-return: ambiguous count is 0",
          result["ambiguous"] == 0)
    check("arbitrate_trades early-return: arbitrated count is 0",
          result["arbitrated"] == 0)
    check("arbitrate_trades early-return: net_pessimistic is the full sum",
          result["net_usd_pessimistic"] == 300.0)
    check("arbitrate_trades early-return: net_arbitrated equals pessimistic when "
          "nothing flips",
          result["net_usd_arbitrated"] == result["net_usd_pessimistic"])
    check("arbitrate_trades early-return: sign_agrees is True when nothing changes",
          result["sign_agrees"] is True)
    check("arbitrate_trades early-return: unarbitrated count is 0",
          result["unarbitrated"] == 0)

    # --- arbitrate_trades: flip case with real arbitration ---
    # One trade: exit minute 09:35 on 2026-04-06, tape range 24324.25–24373.75,
    # both stop and target inside range, target prints first in the tape.
    # Fixture: entry=24345, stop=24330, target=24360, contracts=1, usd_per_point=20
    # Pessimistic (stop first): gross = 1*(24330-24345)*1*20 = -300, net = -300 - 5 = -305
    # Arbitrated (target first): gross = 1*(24360-24345)*1*20 = 300, net = 300 - 5 = 295
    # Delta = 295 - (-305) = 600. net_arbitrated = -305 + 600 = 295.
    # Pessimistic is negative, arbitrated is positive → sign_agrees must be False.
    flip_trades = [
        {
            "session": "2026-04-06",
            "direction": +1,
            "entry_px": 24345.0,
            "exit_px": 24330.0,              # pessimistic exit at stop
            "exit_reason": "stop",
            "exit_ts": pd.Timestamp("2026-04-06 09:35", tz=NY),
            "sl_points": 15.0,               # stop = 24345 - 15 = 24330
            "tp_points": 15.0,               # target = 24345 + 15 = 24360
            "contracts": 1,
            "commission_usd": 5.0,
            "net_usd": -305.0,               # -15*1*20 - 5
            "ambiguous_bar": True,
        }
    ]
    result_flip = arbitrate_trades(flip_trades, usd_per_point=20.0, symbol="NQ")
    check("arbitration counts the trades it actually settled",
          result_flip["arbitrated"] == 1)
    check("arbitration flips a genuinely target-first trade",
          result_flip["flipped_to_target"] == 1)
    check("arbitrated net recomputes at the target price",
          abs(result_flip["net_usd_arbitrated"] - 295.0) < 1e-9)
    check("sign_agrees is False when the pessimistic assumption flips the sign",
          result_flip["sign_agrees"] is False)
    check("a settled trade is not double-counted as unarbitrated",
          result_flip["unarbitrated"] == 0)

    # --- arbitrate_trades: a minute the tape genuinely cannot settle --------
    # Same session/minute as the flip case above (tape range 24324.25-
    # 24373.75), but this trade's bracket levels (stop=20000, target=30000)
    # never print in that window, so arbitrate_minute returns None for it.
    # An unresolvable minute must be counted as unarbitrated, not silently
    # dropped -- otherwise `ambiguous > 0` with `arbitrated == 0` would report
    # sign_agrees from zero evidence.
    unresolvable_trade = {
        "session": "2026-04-06", "direction": +1, "entry_px": 24345.0,
        "exit_px": 20000.0, "exit_reason": "stop",
        "exit_ts": pd.Timestamp("2026-04-06 09:35", tz=NY),
        "sl_points": 4345.0,             # stop = 24345 - 4345 = 20000 (never prints)
        "tp_points": 5655.0,             # target = 24345 + 5655 = 30000 (never prints)
        "contracts": 1, "commission_usd": 5.0, "net_usd": -100.0,
        "ambiguous_bar": True,
    }
    result_mixed = arbitrate_trades(list(flip_trades) + [unresolvable_trade],
                                    usd_per_point=20.0, symbol="NQ")
    check("a trade whose bracket levels never print in its minute's tape is "
          "counted as unarbitrated, not silently dropped from the total",
          result_mixed["ambiguous"] == 2 and result_mixed["arbitrated"] == 1
          and result_mixed["unarbitrated"] == 1)

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else 0)
