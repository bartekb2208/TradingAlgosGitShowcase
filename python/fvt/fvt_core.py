"""fvt_core.py — FVT V1 pure logic. No I/O, no engine, no network.

Every rule the strategy makes a decision on lives here so it can be tested in
milliseconds without loading a parquet.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class FVTConfig:
    """Pre-registered baseline. Defaults ARE the frozen config."""
    # --- structure ---
    swing_lookback: int = 2
    displacement_max_counter_wick: float = 0.20
    require_large_candle: bool = False
    large_candle_lookback: int = 3
    confirm_within_bars: int = 0          # 0 = displacement and break are one bar.
                                           # NOT IMPLEMENTED: no logic in this
                                           # module reads this field for values > 0;
                                           # it is a placeholder for the looser
                                           # reading described in the spec, not a
                                           # wired knob.
    # --- sessions (America/New_York wall clock) ---
    morning_start: str = "09:30"
    morning_end: str = "11:00"
    afternoon_start: str = "14:00"
    afternoon_end: str = "15:00"
    phase1_minutes: int = 15
    skip_first_minutes: int = 0           # the source's "avoid first 3m" is optional
    hard_phase_gate: bool = True
    force_flat: str = "16:00"
    # --- risk ---
    stop_mode: str = "literal"            # "literal" | "atr_multiple"
    atr_period: int = 14
    atr_stop_mult: float = 2.0            # atr_multiple mode only
    rr_target: float = 1.5
    risk_usd: float = 1000.0
    usd_per_point: float = 20.0           # NQ; ES is 50.0
    tick_size: float = 0.25
    commission_per_side: float = 2.50     # per contract
    slippage_ticks: int = 1
    max_trades_per_window: int | None = None
    min_validation_sessions: int = 20


def round_to_tick(px: float, tick: float) -> float:
    return round(px / tick) * tick


def displacement_threshold(o: float, h: float, l: float, direction: int,
                           max_cw: float = 0.20) -> float:
    """The fib-0.2 price the close must beat.

    Anchoring is decoded from the source's two worked examples: fib 1 sits at the
    candle's OPEN, fib 0 at the extreme the candle travelled to. The "counter-wick"
    is the part of the move given back before the close.
    """
    if direction > 0:
        return h - max_cw * (h - o)
    return l + max_cw * (o - l)


def is_displacement(o: float, h: float, l: float, c: float, direction: int,
                    max_cw: float = 0.20) -> bool:
    if direction > 0:
        if h <= o:                      # no travel, no denominator
            return False
        return c > displacement_threshold(o, h, l, +1, max_cw)
    if o <= l:
        return False
    return c < displacement_threshold(o, h, l, -1, max_cw)


class PivotTracker:
    """Fractal swing points, confirmed with a k-bar lag.

    A pivot at bar p is only *knowable* once bar p+k has printed. `update` is
    called once per bar and never inspects a bar it has not been given, which is
    what makes the detector lookahead-free by construction rather than by audit.
    """

    def __init__(self, k: int) -> None:
        self.k = k
        self.reset()

    def reset(self) -> None:
        self._highs: list[float] = []
        self._lows: list[float] = []
        self.last_swing_high: tuple[int, float] | None = None
        self.last_swing_low: tuple[int, float] | None = None

    def update(self, high: float, low: float) -> None:
        self._highs.append(high)
        self._lows.append(low)
        i = len(self._highs) - 1
        p = i - self.k                      # the candidate pivot
        if p - self.k < 0:                  # not enough bars on the left
            return
        hs, ls, k = self._highs, self._lows, self.k
        left = range(p - k, p)
        right = range(p + 1, p + k + 1)
        if all(hs[p] > hs[j] for j in left) and all(hs[p] > hs[j] for j in right):
            self.last_swing_high = (p, hs[p])
        if all(ls[p] < ls[j] for j in left) and all(ls[p] < ls[j] for j in right):
            self.last_swing_low = (p, ls[p])


@dataclass(frozen=True)
class StructureEvent:
    direction: int      # +1 bullish break, -1 bearish
    kind: str           # "BOS" (with bias) | "MSB" (against bias)
    level: float


class StructureTracker:
    """Breaks of the most recent confirmed swing, by CLOSE.

    `bias` is a per-window reading of direction; `pivots` are market structure.
    That is why they reset on different clocks — see the spec.
    """

    def __init__(self, k: int) -> None:
        self.pivots = PivotTracker(k)
        self.reset_session()

    def reset_session(self) -> None:
        self.pivots.reset()
        self.bias = 0
        self._consumed_high: int | None = None
        self._consumed_low: int | None = None

    def reset_bias(self) -> None:
        self.bias = 0

    def update(self, high: float, low: float, close: float) -> StructureEvent | None:
        self.pivots.update(high, low)
        sh, sl = self.pivots.last_swing_high, self.pivots.last_swing_low
        # Bullish break takes precedence if both somehow qualify on one bar.
        if sh is not None and close > sh[1] and sh[0] != self._consumed_high:
            kind = "MSB" if self.bias == -1 else "BOS"
            self._consumed_high = sh[0]
            self.bias = +1
            return StructureEvent(+1, kind, sh[1])
        if sl is not None and close < sl[1] and sl[0] != self._consumed_low:
            kind = "MSB" if self.bias == +1 else "BOS"
            self._consumed_low = sl[0]
            self.bias = -1
            return StructureEvent(-1, kind, sl[1])
        return None


def hhmm_to_minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def minutes_since_midnight(ts) -> int:
    """Wall-clock minutes in the bar's own timezone. DST is the tz database's job."""
    return ts.hour * 60 + ts.minute


def window_of(ts, cfg: FVTConfig) -> str | None:
    """Which entry window a bar belongs to. End bounds are EXCLUSIVE."""
    m = minutes_since_midnight(ts)
    if hhmm_to_minutes(cfg.morning_start) <= m < hhmm_to_minutes(cfg.morning_end):
        return "am"
    if hhmm_to_minutes(cfg.afternoon_start) <= m < hhmm_to_minutes(cfg.afternoon_end):
        return "pm"
    return None


def window_start_minutes(window: str, cfg: FVTConfig) -> int:
    return hhmm_to_minutes(cfg.morning_start if window == "am" else cfg.afternoon_start)


def phase_of(ts, cfg: FVTConfig) -> int:
    """1 = continuation phase (first phase1_minutes), 2 = reversion phase."""
    w = window_of(ts, cfg)
    if w is None:
        return 0
    offset = minutes_since_midnight(ts) - window_start_minutes(w, cfg)
    return 1 if offset < cfg.phase1_minutes else 2


def phase_gate_ok(direction: int, close: float, fair_value: float, phase: int,
                  cfg: FVTConfig) -> bool:
    """Phase 1 must point AWAY from fair value; phase 2 back TOWARD it."""
    if not cfg.hard_phase_gate:
        return True
    if close == fair_value:
        return False
    side = 1 if close > fair_value else -1
    return direction == side if phase == 1 else direction == -side


class WilderATR:
    """Streaming Wilder ATR. State carries across sessions by design — the NQ
    tape is RTH-only and a per-session reset would delete morning phase 1."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.value: float | None = None
        self.count = 0
        self._prev_close: float | None = None
        self._atr_internal: float | None = None

    # Seeds _atr_internal from the first true range, not Wilder's original
    # SMA-of-first-`period` seed. Triaged CAN STAND for V1 (noted here per review).
    @staticmethod
    def _true_range(high: float, low: float, prev_close: float | None) -> float:
        if prev_close is None:
            return high - low
        return max(high - low, abs(high - prev_close), abs(low - prev_close))

    def update(self, high: float, low: float, close: float) -> float | None:
        tr = self._true_range(high, low, self._prev_close)
        self._prev_close = close
        self.count += 1
        if self._atr_internal is None:
            self._atr_internal = tr
        else:
            self._atr_internal += (tr - self._atr_internal) / self.period
        if self.count >= self.period:
            self.value = self._atr_internal
            return self.value
        return None


def brackets(atr: float, cfg: FVTConfig) -> tuple[float, float, str]:
    """(stop_points, target_points, tier). Target is always rr_target x stop."""
    if cfg.stop_mode == "literal":
        if atr > 20.0:
            sl, tier = 50.0, "high"
        elif atr >= 7.0:
            sl, tier = 25.0, "mid"
        else:
            sl, tier = 16.5, "low"
    elif cfg.stop_mode == "atr_multiple":
        sl, tier = cfg.atr_stop_mult * atr, "atr"
    else:
        raise ValueError(f"unknown stop_mode: {cfg.stop_mode!r}")
    return sl, cfg.rr_target * sl, tier


def position_size(sl_points: float, cfg: FVTConfig) -> int:
    """Floor division only. 0 means REFUSE the trade, never round up."""
    risk_per_contract = sl_points * cfg.usd_per_point
    if risk_per_contract <= 0:
        return 0
    return int(cfg.risk_usd // risk_per_contract)


@dataclass(frozen=True)
class Signal:
    ts: object
    direction: int
    structure: str          # "BOS" | "MSB"
    phase: int              # 1 | 2
    window: str             # "am" | "pm"
    fair_value: float
    atr: float
    tier: str
    sl_points: float
    tp_points: float
    contracts: int
    close: float
    attempt_n: int


class FVTDetector:
    """One continuous pass over a bar frame, emitting signals inside the windows.

    ATR runs unbroken across the whole frame; structure resets on each new NY
    calendar date; bias resets at each window open. See the spec for why those
    three clocks differ.
    """

    def __init__(self, cfg: FVTConfig) -> None:
        self.cfg = cfg

    def run(self, bars) -> tuple[list[Signal], dict]:
        cfg = self.cfg
        atr = WilderATR(cfg.atr_period)
        struct = StructureTracker(cfg.swing_lookback)
        counters = {"skipped_not_warm": 0, "skipped_phase_gate": 0,
                    "skipped_no_size": 0, "windows_no_anchor": 0,
                    "bars": 0, "structure_events": 0}
        signals: list[Signal] = []

        cur_date = None
        cur_window: str | None = None
        anchors: dict[tuple, float] = {}
        attempts: dict[tuple, int] = {}
        ranges: list[float] = []

        for ts, row in zip(bars.index, bars.itertuples(index=False)):
            o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
            counters["bars"] += 1

            day = ts.normalize()
            if day != cur_date:
                cur_date, cur_window = day, None
                struct.reset_session()
                ranges = []

            a = atr.update(h, l, c)

            # Window transition must land BEFORE this bar's structure update: bias
            # is a per-window reading, so a window's first bar has to be judged with
            # bias already reset, not evaluated on the stale bias and then wiped.
            win = window_of(ts, cfg)
            if win != cur_window:
                cur_window = win
                if win is not None:
                    struct.reset_bias()
                    key = (day, win)
                    if minutes_since_midnight(ts) == window_start_minutes(win, cfg):
                        anchors[key] = o
                    else:
                        counters["windows_no_anchor"] += 1

            ev = struct.update(h, l, c)
            if ev is not None:
                counters["structure_events"] += 1

            prev_ranges = list(ranges)
            ranges.append(h - l)
            if len(ranges) > cfg.large_candle_lookback:
                ranges.pop(0)

            if win is None or ev is None:
                continue
            key = (day, win)
            if key not in anchors:
                continue
            offset = minutes_since_midnight(ts) - window_start_minutes(win, cfg)
            if offset < cfg.skip_first_minutes:
                continue
            if a is None:
                counters["skipped_not_warm"] += 1
                continue
            if not is_displacement(o, h, l, c, ev.direction,
                                   cfg.displacement_max_counter_wick):
                continue
            if cfg.require_large_candle:
                window_ranges = prev_ranges[-cfg.large_candle_lookback:]
                if window_ranges and (h - l) <= max(window_ranges):
                    continue
            phase = phase_of(ts, cfg)
            fv = anchors[key]
            if not phase_gate_ok(ev.direction, c, fv, phase, cfg):
                counters["skipped_phase_gate"] += 1
                continue
            sl, tp, tier = brackets(a, cfg)
            n = position_size(sl, cfg)
            if n <= 0:
                counters["skipped_no_size"] += 1
                continue
            if (cfg.max_trades_per_window is not None
                    and attempts.get(key, 0) >= cfg.max_trades_per_window):
                continue
            attempts[key] = attempts.get(key, 0) + 1
            signals.append(Signal(ts=ts, direction=ev.direction, structure=ev.kind,
                                  phase=phase, window=win, fair_value=fv, atr=a,
                                  tier=tier, sl_points=sl, tp_points=tp,
                                  contracts=n, close=c, attempt_n=attempts[key]))
        return signals, counters


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    # --- Source-document regression -------------------------------------
    # The source PDF prints exact fib prices on two worked examples. They admit
    # exactly one anchoring: fib 1 = the candle's OPEN, fib 0 = the extreme the
    # candle travelled to, and the close must land inside the 0->0.2 band.
    # If either of these fails, the decoding is wrong and everything downstream
    # is void. TradingView rounds the drawn level to the 0.25 tick.
    #
    # Example 1 (bullish): 0 = 13,086.25   0.2 = 13,081.25   1 = 13,061.50
    lvl = displacement_threshold(o=13_061.50, h=13_086.25, l=13_061.50, direction=+1)
    check("source example 1 fib-0.2 level == 13,081.25",
          round_to_tick(lvl, 0.25) == 13_081.25)
    # Example 2 (bearish): 0 = 25,415.00   0.2 = 25,417.25   1 = 25,426.00
    lvl = displacement_threshold(o=25_426.00, h=25_426.00, l=25_415.00, direction=-1)
    check("source example 2 fib-0.2 level == 25,417.25",
          round_to_tick(lvl, 0.25) == 25_417.25)

    # --- Pass/fail either side of the threshold -------------------------
    check("bullish close above the 0.2 level is displacement",
          is_displacement(13_061.50, 13_086.25, 13_061.50, 13_082.00, +1) is True)
    check("bullish close below the 0.2 level is not",
          is_displacement(13_061.50, 13_086.25, 13_061.50, 13_080.00, +1) is False)
    check("bearish close below the 0.2 level is displacement",
          is_displacement(25_426.00, 25_426.00, 25_415.00, 25_416.00, -1) is True)
    check("bearish close above the 0.2 level is not",
          is_displacement(25_426.00, 25_426.00, 25_415.00, 25_419.00, -1) is False)

    # --- Degenerate candles ---------------------------------------------
    check("bullish candle with high == open has no denominator",
          is_displacement(100.0, 100.0, 99.0, 100.0, +1) is False)
    check("bearish candle with low == open has no denominator",
          is_displacement(100.0, 101.0, 100.0, 100.0, -1) is False)
    check("a flat doji is never displacement either way",
          is_displacement(100.0, 100.0, 100.0, 100.0, +1) is False
          and is_displacement(100.0, 100.0, 100.0, 100.0, -1) is False)

    # --- Config defaults are the pre-registered baseline ------------------
    c = FVTConfig()
    check("default counter-wick threshold is the source's 20%",
          c.displacement_max_counter_wick == 0.20)
    check("large-candle filter defaults off (source calls it discretionary)",
          c.require_large_candle is False)
    check("default target is a fixed 1.5R", c.rr_target == 1.5)
    check("phase gate defaults hard", c.hard_phase_gate is True)
    check("displacement and break default to the same bar", c.confirm_within_bars == 0)

    # --- Fractal pivots --------------------------------------------------
    # A clean peak at index 3 with k=2: highs rise then fall symmetrically.
    highs = [10.0, 11.0, 12.0, 15.0, 12.5, 11.5, 11.0]
    lows = [9.0, 9.5, 10.0, 11.0, 10.5, 9.5, 9.0]
    pt = PivotTracker(k=2)
    seen_at: list[tuple[int, float] | None] = []
    for h, l in zip(highs, lows):
        pt.update(h, l)
        seen_at.append(pt.last_swing_high)
    check("no swing high before enough bars exist", seen_at[3] is None)
    check("peak at index 3 is not confirmed on bar 4 (k=2 needs 2 bars after)",
          seen_at[4] is None)
    check("peak at index 3 confirms exactly on bar 5", seen_at[5] == (3, 15.0))
    check("confirmed peak persists", seen_at[6] == (3, 15.0))

    # Swing low mirrors it: a trough at index 3.
    lows2 = [12.0, 11.0, 10.0, 7.0, 10.5, 11.5, 12.0]
    highs2 = [13.0, 12.5, 12.0, 11.0, 11.5, 12.5, 13.0]
    pt = PivotTracker(k=2)
    lows_seen: list[tuple[int, float] | None] = []
    for h, l in zip(highs2, lows2):
        pt.update(h, l)
        lows_seen.append(pt.last_swing_low)
    check("trough at index 3 confirms exactly on bar 5", lows_seen[5] == (3, 7.0))

    # NO-LOOKAHEAD: replaying a truncated series must give identical results on
    # the overlap. If a pivot ever used a future bar, these would diverge.
    pt_short = PivotTracker(k=2)
    short_seen = []
    for h, l in zip(highs[:5], lows[:5]):
        pt_short.update(h, l)
        short_seen.append(pt_short.last_swing_high)
    check("truncated replay matches full replay on the overlap",
          short_seen == seen_at[:5])

    # Ties do not make a pivot: strict inequality on both sides.
    pt = PivotTracker(k=2)
    for h in [10.0, 11.0, 12.0, 12.0, 11.0, 10.0, 9.0]:
        pt.update(h, h - 1.0)
    check("an equal-high plateau is not a swing high", pt.last_swing_high is None)

    # reset() clears state between sessions.
    pt = PivotTracker(k=2)
    for h, l in zip(highs, lows):
        pt.update(h, l)
    pt.reset()
    check("reset clears the confirmed swing high", pt.last_swing_high is None)

    # --- BOS / MSB -------------------------------------------------------
    # Build a series with a confirmed swing high at index 3 (=15.0), then break it.
    st = StructureTracker(k=2)
    bars = [  # (high, low, close)
        (10.0, 9.0, 9.5), (11.0, 9.5, 10.5), (12.0, 10.0, 11.5),
        (15.0, 11.0, 14.5), (12.5, 10.5, 11.0), (11.5, 9.5, 10.0),
        (16.0, 10.0, 15.5),                     # closes above 15.0 -> break
    ]
    events = [st.update(*b) for b in bars]
    check("no structure event before the level exists",
          all(e is None for e in events[:6]))
    check("break by close emits an event", events[6] is not None)
    check("break with no prior bias is labelled BOS", events[6].kind == "BOS")
    check("break direction is bullish", events[6].direction == +1)
    check("event carries the broken level", events[6].level == 15.0)
    check("bias is now up", st.bias == +1)

    # A wick through the level with a close below it is NOT a break.
    st = StructureTracker(k=2)
    bars_wick = bars[:6] + [(16.0, 10.0, 14.9)]
    events = [st.update(*b) for b in bars_wick]
    check("a wick through the level without a close past it is not a break",
          events[6] is None)

    # The same level cannot fire twice.
    st = StructureTracker(k=2)
    for b in bars:
        st.update(*b)
    again = st.update(16.5, 15.0, 16.2)
    check("a consumed level does not re-trigger", again is None)

    # Opposing break after an established up-bias is an MSB.
    st = StructureTracker(k=2)
    seq = [
        (10.0, 9.0, 9.5), (11.0, 9.5, 10.5), (12.0, 10.0, 11.5),
        (15.0, 11.0, 14.5), (12.5, 10.5, 11.0), (11.5, 9.5, 10.0),
        (16.0, 10.0, 15.5),                     # BOS up, bias = +1
        (16.5, 14.0, 16.0), (16.2, 13.0, 15.0), # builds a swing low at index 9
        (15.5, 12.0, 13.0), (15.0, 12.5, 13.5), (14.5, 12.8, 13.2),
        (14.0, 11.0, 11.5),                     # closes below the swing low
    ]
    kinds = [e.kind if e else None for e in (st.update(*b) for b in seq)]
    check("an opposing break after an up-bias is labelled MSB", "MSB" in kinds)
    check("bias flipped down after the MSB", st.bias == -1)

    # Session reset clears levels AND bias; bias reset alone keeps levels.
    st.reset_session()
    check("session reset clears bias", st.bias == 0)
    check("session reset clears pivot levels", st.pivots.last_swing_high is None)

    # --- Sessions, phase, fair-value gate --------------------------------
    NY = "America/New_York"
    cfg = FVTConfig()

    def t(s: str):
        return pd.Timestamp(s, tz=NY)

    check("09:29 is outside every window", window_of(t("2026-04-06 09:29"), cfg) is None)
    check("09:30 opens the morning window", window_of(t("2026-04-06 09:30"), cfg) == "am")
    check("10:59 is the last morning signal bar",
          window_of(t("2026-04-06 10:59"), cfg) == "am")
    check("11:00 is outside (end is exclusive)",
          window_of(t("2026-04-06 11:00"), cfg) is None)
    check("13:59 is outside", window_of(t("2026-04-06 13:59"), cfg) is None)
    check("14:00 opens the afternoon window",
          window_of(t("2026-04-06 14:00"), cfg) == "pm")
    check("14:59 is the last afternoon signal bar",
          window_of(t("2026-04-06 14:59"), cfg) == "pm")
    check("15:00 is outside", window_of(t("2026-04-06 15:00"), cfg) is None)

    check("09:30 is phase 1", phase_of(t("2026-04-06 09:30"), cfg) == 1)
    check("09:44 is the last phase-1 minute", phase_of(t("2026-04-06 09:44"), cfg) == 1)
    check("09:45 is phase 2 (boundary belongs to phase 2)",
          phase_of(t("2026-04-06 09:45"), cfg) == 2)
    check("14:14 is phase 1 in the afternoon",
          phase_of(t("2026-04-06 14:14"), cfg) == 1)
    check("14:15 is phase 2 in the afternoon",
          phase_of(t("2026-04-06 14:15"), cfg) == 2)

    # DST: the wall-clock rules must hold on both transition days.
    check("09:30 is still the open on the spring-forward Monday",
          window_of(t("2026-03-09 09:30"), cfg) == "am")
    check("09:30 is still the open after the fall-back Sunday",
          window_of(t("2026-11-02 09:30"), cfg) == "am")

    # Phase gate: phase 1 trades AWAY from fair value, phase 2 back TOWARD it.
    check("phase 1 long above fair value is allowed (moving away)",
          phase_gate_ok(+1, close=100.0, fair_value=99.0, phase=1, cfg=cfg) is True)
    check("phase 1 long below fair value is blocked (that is reversion)",
          phase_gate_ok(+1, close=98.0, fair_value=99.0, phase=1, cfg=cfg) is False)
    check("phase 2 long below fair value is allowed (moving back)",
          phase_gate_ok(+1, close=98.0, fair_value=99.0, phase=2, cfg=cfg) is True)
    check("phase 2 long above fair value is blocked",
          phase_gate_ok(+1, close=100.0, fair_value=99.0, phase=2, cfg=cfg) is False)
    check("phase 2 short above fair value is allowed",
          phase_gate_ok(-1, close=100.0, fair_value=99.0, phase=2, cfg=cfg) is True)
    check("a signal exactly at fair value is skipped in either phase",
          phase_gate_ok(+1, 99.0, 99.0, 1, cfg) is False
          and phase_gate_ok(+1, 99.0, 99.0, 2, cfg) is False)
    check("the gate is a no-op when hard_phase_gate is off",
          phase_gate_ok(+1, 98.0, 99.0, 1, FVTConfig(hard_phase_gate=False)) is True)

    # --- ATR --------------------------------------------------------------
    a = WilderATR(period=3)
    check("ATR is None before it is warm", a.update(10.0, 9.0, 9.5) is None)
    a.update(10.5, 9.5, 10.0)
    check("ATR still cold at 2 of 3 bars", a.value is None)
    v = a.update(11.0, 10.0, 10.5)
    check("ATR is warm on the period-th bar", v is not None and v > 0)
    check("ATR true range includes the gap from the prior close",
          WilderATR._true_range(high=20.0, low=19.0, prev_close=10.0) == 10.0)
    check("true range with no prior close is the bar range",
          WilderATR._true_range(high=20.0, low=19.0, prev_close=None) == 1.0)

    # --- Literal tier table (the source's own numbers) ---------------------
    cfg_nq = FVTConfig(stop_mode="literal", usd_per_point=20.0)
    check("ATR above 20 -> 50pt stop", brackets(25.0, cfg_nq)[:2] == (50.0, 75.0))
    check("ATR of exactly 20 is the mid tier", brackets(20.0, cfg_nq)[:2] == (25.0, 37.5))
    check("ATR of exactly 7 is the mid tier", brackets(7.0, cfg_nq)[:2] == (25.0, 37.5))
    check("ATR below 7 -> 16.5pt stop", brackets(5.0, cfg_nq)[:2] == (16.5, 24.75))
    check("tier labels", [brackets(x, cfg_nq)[2] for x in (25.0, 12.0, 5.0)]
          == ["high", "mid", "low"])
    check("every literal target is exactly 1.5R",
          all(abs(tp - 1.5 * sl) < 1e-9
              for sl, tp, _ in (brackets(x, cfg_nq) for x in (25.0, 12.0, 5.0))))

    # --- ATR-multiple mode -------------------------------------------------
    cfg_es = FVTConfig(stop_mode="atr_multiple", usd_per_point=50.0)
    sl, tp, tier = brackets(3.0, cfg_es)
    check("atr_multiple stop is 2x ATR", abs(sl - 6.0) < 1e-9)
    check("atr_multiple target is 1.5R", abs(tp - 9.0) < 1e-9)
    check("atr_multiple tier label", tier == "atr")

    # --- Sizing reproduces the source's contract column exactly -------------
    check("50pt stop sizes to 1 NQ contract", position_size(50.0, cfg_nq) == 1)
    check("25pt stop sizes to 2 NQ contracts", position_size(25.0, cfg_nq) == 2)
    check("16.5pt stop sizes to 3 NQ contracts", position_size(16.5, cfg_nq) == 3)
    check("sizing rounds DOWN, never up into over-risk",
          position_size(30.0, cfg_nq) == 1)          # 1000/600 = 1.67 -> 1
    check("a stop too wide to afford one contract refuses the trade",
          position_size(400.0, cfg_nq) == 0)         # 1000/8000 = 0.125 -> 0
    check("realised risk never exceeds the budget",
          position_size(30.0, cfg_nq) * 30.0 * 20.0 <= cfg_nq.risk_usd)

    # --- Detector end-to-end on a synthetic session -----------------------
    # 09:00-09:29 premarket drift builds pivots and warms ATR; then a swing high
    # forms and a displacement candle closes through it during phase 1, ABOVE
    # fair value, which the phase-1 gate must allow.
    NY = "America/New_York"
    idx = pd.date_range("2026-04-06 09:00", "2026-04-06 09:40", freq="1min", tz=NY)
    o = [100.0] * len(idx)
    h = [100.5] * len(idx)
    lo = [99.5] * len(idx)
    c = [100.0] * len(idx)
    # zig-zag the premarket so pivots exist and ATR is warm
    for i in range(len(idx)):
        if i % 4 == 0:
            h[i], c[i] = 101.0, 100.8
        elif i % 4 == 2:
            lo[i], c[i] = 99.0, 99.2
    frame = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c}, index=idx)
    # Bar 09:35 is a clean bullish displacement that closes above everything.
    frame.loc[frame.index[35], ["open", "high", "low", "close"]] = [100.0, 110.0, 99.9, 109.8]
    det = FVTDetector(FVTConfig(stop_mode="atr_multiple", usd_per_point=50.0))
    sigs, counters = det.run(frame)
    check("detector produced at least one signal", len(sigs) >= 1)
    s = sigs[0]
    check("signal is inside a window", s.window in ("am", "pm"))
    check("signal is bullish", s.direction == +1)
    check("signal carries a structure label", s.structure in ("BOS", "MSB"))
    check("fair value is the 09:30 bar's open",
          s.fair_value == float(frame.loc[frame.index[30], "open"]))
    check("stop and target are populated", s.sl_points > 0 and s.tp_points > 0)
    check("target is 1.5x the stop", abs(s.tp_points - 1.5 * s.sl_points) < 1e-9)
    check("attempt numbering starts at 1", s.attempt_n == 1)
    check("counters are reported", "skipped_not_warm" in counters
          and "skipped_phase_gate" in counters)

    # A session with no 09:30 bar yields no morning signals and is counted.
    gapped = frame.drop(frame.index[30])
    _, counters2 = FVTDetector(FVTConfig(stop_mode="atr_multiple")).run(gapped)
    check("a window with no anchor bar is skipped and counted",
          counters2["windows_no_anchor"] >= 1)

    # No signal may reference a bar the detector had not yet seen: truncating
    # the frame after the signal bar must not change the signals before it.
    sigs_trunc, _ = FVTDetector(
        FVTConfig(stop_mode="atr_multiple", usd_per_point=50.0)
    ).run(frame.iloc[:37])
    check("truncated detector run matches the full run on the overlap",
          [x.ts for x in sigs_trunc] == [x.ts for x in sigs if x.ts <= frame.index[36]])

    # --- Regression: bias must reset BEFORE the window's first bar is judged ---
    # Premarket sets bias -1 via a bearish BOS. A fresh swing high is then built
    # and left un-triggered until the window's own anchor bar (09:30) closes
    # through it: with the fix, bias is reset to 0 before that bar's own break is
    # evaluated, so it is BOS (not MSB against the stale premarket bias) -- and
    # the bias it establishes (+1) must survive to correctly label the NEXT,
    # opposing break in the same window as MSB. (Verified by hand-reverting the
    # ordering: the buggy order mislabels these MSB then BOS -- exactly inverted.)
    bias_rows = (
        [(17.0, 17.0, 16.0, 16.5), (16.5, 16.5, 15.0, 15.5), (15.5, 15.5, 13.5, 14.0),
         (14.5, 14.5, 12.0, 12.5), (15.0, 15.0, 13.0, 14.5), (16.0, 16.0, 14.0, 15.5),
         (16.5, 16.5, 11.0, 11.5)]                                   # bearish BOS -> bias -1
        + [(10.0, 10.0, 9.0, 9.5), (11.0, 11.0, 9.5, 10.5), (12.0, 12.0, 10.0, 11.5),
           (15.0, 15.0, 11.0, 14.5), (12.5, 12.5, 10.5, 11.0), (11.5, 11.5, 9.5, 10.0)]
        + [(10.5, 11.5, 9.5, 10.5)] * 17                              # flat: kills time to 09:30
        + [(12.0, 20.0, 11.9, 19.5)]                                  # 09:30 anchor + bullish break
        + [(19.0, 19.5, 15.0, 15.5), (15.0, 15.5, 10.0, 10.5), (10.0, 10.5, 5.0, 9.5),
           (9.5, 9.8, 6.5, 9.6), (9.6, 9.7, 7.0, 9.6)]                 # new trough forms, unbroken
        + [(9.6, 9.8, 3.0, 3.5)]                                      # bearish break of that trough
    )
    bias_idx = pd.date_range("2026-04-06 09:00", periods=len(bias_rows), freq="1min", tz=NY)
    bias_frame = pd.DataFrame(
        {"open": [r[0] for r in bias_rows], "high": [r[1] for r in bias_rows],
         "low": [r[2] for r in bias_rows], "close": [r[3] for r in bias_rows]},
        index=bias_idx)
    bias_sigs, _ = FVTDetector(
        FVTConfig(stop_mode="atr_multiple", usd_per_point=50.0)
    ).run(bias_frame)
    check("window-open bar's own break is BOS on a freshly-reset bias, and the "
          "next opposing break in the same window is correctly MSB against it",
          len(bias_sigs) == 2
          and bias_sigs[0].structure == "BOS" and bias_sigs[0].direction == +1
          and bias_sigs[1].structure == "MSB" and bias_sigs[1].direction == -1)

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else 0)
