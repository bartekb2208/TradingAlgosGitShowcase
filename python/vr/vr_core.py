"""vr_core.py — VR V1 shared finite state machine. NO nautilus imports.

Spec: research/v-reversal/DESIGN.md
Source: Cody Bengtson's *V-Reversal Setup* (2023; third-party document, not redistributed here)

This module is deliberately engine-free. Both drivers — the pandas offline
detector (`vr_detect.py`) and the Nautilus strategy (`vr_strategy.py`) — run the
SAME state machine from here, so detection logic is written and tested once.
Keeping it import-free is also what lets the FSM be selftested in milliseconds.

`SPEC:` marks a value taken from the source doc. `JUDGMENT:` marks a choice the
doc does not make -- these are the calibratable ones and the attackable ones.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import sys
from collections import deque

import numpy as np
from dataclasses import dataclass
from enum import Enum, auto

NS = 1_000_000_000
TICK = 0.25          # ES
TICK_USD = 12.50     # 0.25 x multiplier 50


class VRState(Enum):
    SEEKING = auto()
    THIN_SEEN = auto()
    CONSOLIDATING = auto()
    BLOWOFF = auto()
    RETRACING = auto()
    ARMED = auto()
    IN_TRADE = auto()


@dataclass
class VRConfig:
    # --- thin impulse (precondition: thin areas break back through cleanly) ---
    thin_min_ticks: int = 8          # JUDGMENT: minimum traverse to count as an impulse
    thin_max_secs: float = 60.0      # JUDGMENT: it must be fast to be "thin"
    thin_vpt_frac: float = 0.7       # JUDGMENT: volume-per-tick <= this x trailing median

    # --- consolidation ---
    cons_max_ticks: int = 8          # SPEC (paraphrased): zone trades within 6-8 ticks
    cons_min_secs: float = 60.0      # JUDGMENT: doc example ran 12:16-12:30
    cons_max_secs: float = 1800.0    # JUDGMENT: abandon a zone that never resolves
    #   CALIBRATION NOTE (exploratory, 2026-08-03): consolidation was originally
    #   measured CUMULATIVELY from the instant the thin impulse ended, and any single
    #   print outside the band abandoned the zone outright. That is not "ranging" --
    #   it is "never once deviated", and it was the binding constraint in the funnel
    #   (1042 thin impulses -> 27 consolidations, 2.6%). It is now a ROLLING window:
    #   the zone qualifies whenever the last `cons_min_secs` of trade has spanned
    #   <= cons_max_ticks. Self-healing, and it is what the source doc describes.
    #   Thresholds themselves are UNCHANGED.

    # --- blowoff ---
    blowoff_window_ms: int = 1000    # SPEC (paraphrased): blowoff lands within 1 second
    blowoff_mult: float = 3.0        # SPEC (paraphrased): far above the zone average
    blowoff_min_contracts: int = 250  # JUDGMENT: doc example was 1000 on ES

    # --- trapped sellers ---
    trap_levels: int = 5             # SPEC (paraphrased): imbalance across the lowest 5 ticks
    trap_pctile: float = 0.99        # SPEC(reinterpreted): the doc's 3,000+ contract figure
    #   describes an EXTREME, not an absolute quantity.
    #
    #   PROVENANCE: the source doc's example is drawn from **ESZ23** (ES Dec 2023,
    #   index ~4700). This data is **ESM6** (Jun 2026, index ~7561) -- a different
    #   contract, a different era, and a materially different tape. Porting an
    #   absolute contract count across that gap makes "rarity" an artifact of the
    #   constant rather than a property of the market.
    #
    #   A percentile preserves the doc's intent ("unusually large for this book")
    #   and stays scale-free across contracts, eras and instruments. This is the
    #   same reasoning research/v-reversal/PREREG.md applies to its own p99/p90 gates:
    #   "percentiles computed within the evaluated sample ... they are relative,
    #   not absolute."
    trap_hist: int = 4000            # JUDGMENT: rolling sample for the percentile
    trap_min_hist: int = 500         # JUDGMENT: don't gate on a tiny sample
    trap_min_frac: float = 0.5       # JUDGMENT: the net one-sided imbalance at the low
    #   must be >= this fraction of the blowoff window's aggressive-sell volume.
    #   A percentile alone ranks but does not size: against a flat distribution ANY
    #   trivial positive residual reads as "extreme" (measured: +15 imbalance beat a
    #   q99 of 14 on a perfectly two-sided blowoff). The doc means a 1,287-contract
    #   one-sided cascade, so the gate needs magnitude as well as rank. Expressed as
    #   a FRACTION so it stays free of any ESZ23-era constant.

    # --- V retrace ---
    retrace_pct: float = 0.90        # SPEC (paraphrased): whole drop recovered
    speed_ratio: float = 1.5         # SPEC (paraphrased): recovered about as fast as it fell

    # --- entry / stop / target switches (parameterised judgment) ---
    entry_mode: str = "marketable"    # marketable | passive_zone | breakout_confirmed
    #   MARKETABLE IS THE DEFAULT, and it is what the source doc prescribes at this
    #   moment (paraphrased): buy as price breaks out of the consolidation zone, with
    #   order flow on the break as optional confirmation. A passive bid is geometrically IMPOSSIBLE here — the FSM
    #   arms when the retrace reaches `retrace_pct` of the blowoff leg, which by
    #   construction leaves price at or BELOW cons_lo (measured: 0 to -6 ticks on
    #   all 6 exploratory structures). A buy limit inside the zone therefore sits
    #   above the market and post_only rejects it. Passive entry belongs to the
    #   doc's LATER phase, after price has re-entered the zone and pulled back.
    stop_mode: str = "wide_v_low"     # wide_v_low | narrow_cons_lo
    #   FORCED to wide with a marketable entry: entry lands at/below cons_lo, so a
    #   "narrow" stop AT cons_lo would sit ABOVE the entry and be instantly invalid.
    #   The doc's narrow stop presumes an entry INSIDE the zone.
    # --- TIER 1 risk structure (principled, not fitted) -----------------------
    min_rr: float | None = None      # reject a structure whose reward:risk is below this.
    #   Reward is measured to `thin_origin` — the doc's own structural target, i.e.
    #   the room the setup says is available — even in scalp_trail mode, where the
    #   exit is a trail. Risk is entry -> stop. This is NOT a fitted threshold: at
    #   the measured 60% hit rate, breakeven RR is 0.67, so 1.0 is conservative and
    #   derivable. It exists to reject structures like the measured
    #   "risk 92 ticks to make 14", which no trader would take by eye.
    stop_cons_mult: float | None = None  # cap the stop at this multiple of the
    #   consolidation range. The zone is the setup's natural unit of structure (the
    #   doc says 6-8 ticks); the two losing trades had blowoffs 5.5x and 7.5x their
    #   zone. A blowoff many times the range it broke out of is a repricing, not the
    #   overshoot-and-snap-back the hypothesis describes — so risking all of it is
    #   incoherent with the setup being traded.
    max_stop_ticks: int | None = None  # risk filter: skip a structure whose stop is
    #   further than this from entry. Not signal tuning — it bounds per-trade risk,
    #   which on a small prop account is the binding constraint (measured wide stops
    #   on the 6 exploratory structures ranged 1 to 60 ticks = $12.50 to $750).
    target_mode: str = "scalp_trail"  # scalp_trail | thin_origin_buffered
    trail_cons_mult: float = 1.5     # SPEC (paraphrased): the trail must give the trade room; room is a
    #   multiple of the CONSOLIDATION RANGE, the setup's own structural unit, so it
    #   scales with the zone's realised volatility instead of being a fixed tick count.
    trail_min_ticks: int = 4         # JUDGMENT: floor, so a razor-thin zone cannot
    #   produce a trail nailed to the entry price.
    trail_step_ticks: int = 1        # JUDGMENT: minimum improvement before re-issuing
    #   the stop, so we do not churn the order on every tick.
    target_buffer_ticks: int = 2     # SPEC (paraphrased): exit a few ticks short of the target
    passive_entry_frac: float = 0.75  # SPEC: where in the zone the passive bid rests,
    #   0.0 = cons_lo, 1.0 = cons_hi. The doc's second passive variant rests the bid toward
    #   the TOP of the consolidation zone instead. Resting at cons_lo
    #   (frac 0.0) is the most conservative reading and the least fillable one: by
    #   the time the FSM is ARMED price has already retraced UP into the zone, so a
    #   bid at the bottom only fills if the whole move round-trips again.
    entry_ttl_secs: float = 60.0     # JUDGMENT: cancel an unfilled entry after this.
    #   Without a TTL a resting order from a dead structure survives to the end of
    #   the session and can fill on something entirely unrelated.

    # --- session / risk ---
    rth_only: bool = True
    cooldown_ns: int = 0             # DELIBERATELY 0 for VR — see below.
    #   180s was inherited from MR V2, where it enforces event independence for a
    #   PERCENTILE signal that fires continuously on every tick. VR structures are
    #   ~1.5/session and independent BY CONSTRUCTION: each requires a complete
    #   consolidation -> blowoff -> retrace cycle, so two cannot overlap. Measured
    #   cost of the inherited value: it discarded 2 of 3 structures on 2026-07-10.
    #   An inherited parameter that does no work here and throws away two thirds of
    #   an already-scarce population.
    commission_per_side: float = 2.50
    max_positions: int = 1

    def __post_init__(self) -> None:
        if self.entry_mode not in ("marketable", "passive_zone", "breakout_confirmed", "both"):
            raise ValueError(f"bad entry_mode {self.entry_mode!r}")
        if self.stop_mode not in ("narrow_cons_lo", "wide_v_low"):
            raise ValueError(f"bad stop_mode {self.stop_mode!r}")
        if self.target_mode not in ("thin_origin_buffered", "scalp_trail"):
            raise ValueError(f"bad target_mode {self.target_mode!r}")


def is_thin_impulse(ticks_travelled: float, secs: float, vpt: float,
                    median_vpt: float, cfg: VRConfig) -> bool:
    """A fast, LOW-volume-per-tick move down. Thin => it breaks back cleanly."""
    if ticks_travelled < cfg.thin_min_ticks or secs > cfg.thin_max_secs:
        return False
    if median_vpt <= 0:
        return False
    return vpt <= cfg.thin_vpt_frac * median_vpt


def is_blowoff(window_sell_contracts: float, zone_avg_contracts: float,
               cfg: VRConfig) -> bool:
    """Excessive aggressive-sell volume in <= blowoff_window_ms, vs the zone average."""
    if window_sell_contracts < cfg.blowoff_min_contracts:
        return False
    if zone_avg_contracts <= 0:
        return False
    return window_sell_contracts >= cfg.blowoff_mult * zone_avg_contracts


def trapped_size(bid_sizes: list[float], cfg: VRConfig) -> float:
    """Resting bid size over the lowest `trap_levels` levels — the trapped-seller
    evidence. This quantity does not exist in bar data; it is why VR V1 is on L3.

    NOTE: callers pass the N best *populated* price levels. That equals the
    doc's lowest-five-ticks window only when levels are contiguous, which holds in liquid ES
    front month but would not in a gappy book.
    """
    return float(sum(bid_sizes[:cfg.trap_levels]))


def trap_imbalance(window_trades, v_low: float, cfg: VRConfig) -> float:
    """Aggressive SELL minus BUY volume traded within `trap_levels` ticks of the low,
    inside the blowoff window. This is the "imbalance" the source doc means.

    CLARIFIED BY THE DOC'S AUTHOR (paraphrased): "imbalance" is read off the
    time-and-sales tape rather than the footprint chart; the prints at 12:30:44
    were the blowoff bottom, and they were one-sided.

    So it is TIME & SALES -- aggressive volume TRADED at the extreme -- not resting
    book depth. In the doc's own T&S panel, 12:30:44 shows nine consecutive prints
    marked "Below" (sells hitting through the bid) from 495.75 to 498.75 totalling
    1,287 contracts inside one second. Resting depth was never the quantity.

    `window_trades` items are (ts, price, size, aggressor) with aggressor -1 = sell.
    """
    band = v_low + cfg.trap_levels * TICK
    sells = sum(sz for _, px, sz, agg in window_trades if px <= band and agg < 0)
    buys = sum(sz for _, px, sz, agg in window_trades if px <= band and agg > 0)
    return float(sells - buys)


def is_trap_extreme(current: float, history: "deque[float] | list[float]",
                    cfg: VRConfig, window_sell_volume: float = 0.0) -> bool:
    """Is this imbalance EXTREME *and* MATERIAL?

    Two conditions, because rank and size answer different questions:
      * rank -- strictly greater than the session's own percentile, so it is
        unusual for this book (scale-free across contracts and eras);
      * size -- at least `trap_min_frac` of the blowoff window's sell volume, so a
        trivial residual on a flat distribution cannot qualify on rank alone.
    """
    if len(history) < cfg.trap_min_hist:
        return False
    if window_sell_volume > 0 and current < cfg.trap_min_frac * window_sell_volume:
        return False
    q = float(np.quantile(np.fromiter(history, dtype=float), cfg.trap_pctile))
    return current > q


def retrace_frac(v_low: float, cons_lo: float, high_since_low: float) -> float:
    """Fraction of the blowoff leg (cons_lo -> v_low) recovered by high_since_low."""
    leg = cons_lo - v_low
    if leg <= 0:
        return 0.0
    return (high_since_low - v_low) / leg


class VRStateMachine:
    """Event-driven detector for the V-Reversal structure.

    Deliberately NOT time-bucketed: the source doc's blowoff is "all in a single
    second", and a fixed bucket grid would smear exactly the event the setup
    turns on. Everything is a rolling time window over the raw event stream.
    """

    def __init__(self, cfg: VRConfig):
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        """Full reset — clears session-level statistics too. Use between sessions."""
        self._vpt_hist: deque[float] = deque(maxlen=200)
        self._highs: deque[tuple[int, float]] = deque()   # monotonic-decreasing local highs
        self._trap_hist: deque[float] = deque(maxlen=self.cfg.trap_hist)
        self.abandon_zone()

    def abandon_zone(self) -> None:
        """Give up on the current structure but KEEP session-level statistics.

        The volume-per-tick median and the rolling swing-high window describe the
        session, not the zone. Clearing them on every failed consolidation (which
        is most of them) destroys the very sample the thin-impulse test compares
        against, and starves detection for the rest of the day.
        """
        self.state = VRState.SEEKING
        self.zone: dict = {}
        self._bid = self._ask = None
        self._bid_sizes: list[float] = []
        self._sells: deque[tuple[int, float]] = deque()   # (ts, size) blowoff window
        self._win: deque[tuple[int, float, float, int]] = deque()  # (ts,px,size,aggressor)
        self._zone_trades: deque[tuple[int, float]] = deque()
        self._cons_start_ts = 0
        self._blowoff_start_ts = 0
        self._high_since_low = None
        # rolling price window for the consolidation range (monotonic deques => O(1))
        self._pw: deque[tuple[int, float]] = deque()          # all prices in window
        self._pmax: deque[tuple[int, float]] = deque()        # monotonic decreasing
        self._pmin: deque[tuple[int, float]] = deque()        # monotonic increasing
        self._pw_first_ts: int | None = None   # when accumulation began (NOT evicted)

    def _swing_high_now(self, ts_ns: int, price: float) -> tuple[float, int]:
        """Highest price within the last `thin_max_secs`, and when it occurred.

        MUST be a ROLLING local high, not a session-running maximum. With a
        running max, `ts - swing_high_ts` grows without bound, so the impulse's
        elapsed-time test fails permanently once the session high is more than
        `thin_max_secs` old — detection dies after the first minute.
        Implemented as a monotonic-decreasing deque: O(1) amortised.
        """
        cut = ts_ns - int(self.cfg.thin_max_secs * NS)
        while self._highs and self._highs[0][0] < cut:
            self._highs.popleft()
        while self._highs and self._highs[-1][1] <= price:
            self._highs.pop()
        self._highs.append((ts_ns, price))
        return self._highs[0][1], self._highs[0][0]

    def _roll_range(self, ts_ns: int, price: float) -> tuple[float, float, float]:
        """(max, min, window_span_secs) of price over the last `cons_min_secs`.

        Monotonic deques so this is O(1) amortised rather than O(n) per trade —
        at ~300k trades/session an O(n) scan would dominate the whole runtime.
        """
        cut = ts_ns - int(self.cfg.cons_min_secs * NS)
        if self._pw_first_ts is None:
            self._pw_first_ts = ts_ns
        self._pw.append((ts_ns, price))
        while self._pw and self._pw[0][0] < cut:
            self._pw.popleft()
        while self._pmax and self._pmax[0][0] < cut:
            self._pmax.popleft()
        while self._pmin and self._pmin[0][0] < cut:
            self._pmin.popleft()
        while self._pmax and self._pmax[-1][1] <= price:
            self._pmax.pop()
        self._pmax.append((ts_ns, price))
        while self._pmin and self._pmin[-1][1] >= price:
            self._pmin.pop()
        self._pmin.append((ts_ns, price))
        # "Enough history" must be measured from when accumulation BEGAN, not from
        # the oldest surviving entry: eviction guarantees the oldest survivor is
        # newer than the cutoff, so a span computed across the window is strictly
        # less than cons_min_secs and the gate could never open.
        span = (ts_ns - self._pw_first_ts) / NS
        return self._pmax[0][1], self._pmin[0][1], span

    # -- book -------------------------------------------------------------
    def on_book(self, ts_ns: int, bid_px: float, bid_sizes: list[float],
                ask_px: float) -> None:
        self._bid, self._ask, self._bid_sizes = bid_px, ask_px, list(bid_sizes)

    # -- trades -----------------------------------------------------------
    def on_trade(self, ts_ns: int, price: float, size: float, aggressor: int):
        """Returns the new VRState on a transition, else None."""
        prev = self.state
        if aggressor < 0:
            self._sells.append((ts_ns, size))
        self._win.append((ts_ns, price, size, aggressor))
        cut = ts_ns - self.cfg.blowoff_window_ms * 1_000_000
        while self._sells and self._sells[0][0] < cut:
            self._sells.popleft()
        while self._win and self._win[0][0] < cut:
            self._win.popleft()

        self._swing_high, self._swing_high_ts = self._swing_high_now(ts_ns, price)
        # Sample the SAME quantity the gate tests, so the percentile is like-for-like.
        self._trap_hist.append(trap_imbalance(self._win, price, self.cfg))

        if self.state is VRState.SEEKING:
            self._seek_thin(ts_ns, price, size)
        elif self.state is VRState.THIN_SEEN:
            self._seek_consolidation(ts_ns, price, size)
        elif self.state is VRState.CONSOLIDATING:
            self._seek_blowoff(ts_ns, price, size)
        elif self.state in (VRState.BLOWOFF, VRState.RETRACING):
            self._seek_retrace(ts_ns, price)

        return self.state if self.state is not prev else None

    # -- transitions ------------------------------------------------------
    def _seek_thin(self, ts, price, size) -> None:
        ticks = (self._swing_high - price) / TICK
        secs = (ts - self._swing_high_ts) / NS
        if ticks <= 0 or secs <= 0:
            return
        vpt = size / max(ticks, 1.0)
        self._vpt_hist.append(vpt)
        median = sorted(self._vpt_hist)[len(self._vpt_hist) // 2] if self._vpt_hist else 0.0
        if is_thin_impulse(ticks, secs, vpt, median, self.cfg):
            self.zone = {"thin_origin": self._swing_high}
            self._cons_start_ts = ts
            self.zone["cons_hi"] = price
            self.zone["cons_lo"] = price
            self.state = VRState.THIN_SEEN

    def _seek_consolidation(self, ts, price, size) -> None:
        """ROLLING-window range. A zone qualifies whenever the last `cons_min_secs`
        of trade has spanned <= cons_max_ticks — it does not have to have been
        pristine since the impulse. One stray print no longer destroys the zone."""
        self._zone_trades.append((ts, size))
        hi, lo, span = self._roll_range(ts, price)
        if (ts - self._cons_start_ts) / NS > self.cfg.cons_max_secs:
            self.abandon_zone()               # zone never resolved
            return
        if span < self.cfg.cons_min_secs:
            return                            # not enough history in the window yet
        if (hi - lo) / TICK > self.cfg.cons_max_ticks:
            return                            # currently too wide; keep watching
        self.zone["cons_hi"], self.zone["cons_lo"] = hi, lo
        self.state = VRState.CONSOLIDATING

    def _seek_blowoff(self, ts, price, size) -> None:
        self.zone["cons_hi"] = max(self.zone["cons_hi"], price)
        self._zone_trades.append((ts, size))
        if (ts - self._cons_start_ts) / NS > self.cfg.cons_max_secs:
            self.abandon_zone()               # zone never resolved
            return
        if price >= self.zone["cons_lo"]:
            return                            # still inside the zone
        window_sells = sum(s for _, s in self._sells)
        zone_avg = (sum(s for _, s in self._zone_trades) / len(self._zone_trades)
                    if self._zone_trades else 0.0)
        if not is_blowoff(window_sells, zone_avg, self.cfg):
            return
        trapped = trap_imbalance(self._win, price, self.cfg)
        if not is_trap_extreme(trapped, self._trap_hist, self.cfg, window_sells):
            return                            # not an imbalanced blowoff -> no trapped sellers
        self.zone["v_low"] = price
        self.zone["blowoff_contracts"] = window_sells
        self.zone["trapped"] = trapped
        self._blowoff_start_ts = ts
        self._high_since_low = price
        self.state = VRState.BLOWOFF

    def _seek_retrace(self, ts, price) -> None:
        if price < self.zone["v_low"]:
            self.zone["v_low"] = price        # still falling: extend the V low
            self._blowoff_start_ts = ts
            self._high_since_low = price
            return
        self._high_since_low = max(self._high_since_low, price)
        self.state = VRState.RETRACING
        frac = retrace_frac(self.zone["v_low"], self.zone["cons_lo"], self._high_since_low)
        leg_secs = max((self._blowoff_start_ts - self._cons_start_ts) / NS, 1e-9)
        back_secs = (ts - self._blowoff_start_ts) / NS
        if frac >= self.cfg.retrace_pct and back_secs <= self.cfg.speed_ratio * leg_secs:
            self.zone["retrace_frac"] = frac
            self.zone["armed_ts"] = ts
            self.zone["armed_px"] = price
            self.state = VRState.ARMED


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
def _synthetic_cfg(**kw) -> VRConfig:
    """Loosened thresholds so a hand-built V fits in a few hundred events."""
    base = dict(cons_min_secs=1.0, thin_max_secs=10.0,
                blowoff_min_contracts=100, trap_min_hist=10)
    base.update(kw)
    return VRConfig(**base)


def _drive_v(sm: VRStateMachine, blowoff_size: float, trap_depth: float,
             ambient: float = 20.0, counter_buys: float = 0.0) -> None:
    """Thin impulse -> consolidation -> blowoff -> retrace, driven into `sm`.

    `ambient` is the ordinary book depth everywhere EXCEPT the blowoff, so that
    `trap_depth` is the only thing that varies between the positive case and the
    not-extreme near-miss. Using a different depth in the retrace phase would
    make the retrace book itself extreme and arm the FSM for the wrong reason.
    """
    t = 0
    for i in range(10):                                   # 1. thin impulse, light volume
        t += 100_000_000
        sm.on_book(t, 100.00 - i * TICK, [ambient] * 10, 100.25 - i * TICK)
        sm.on_trade(t, 100.00 - i * TICK, 1, -1)
    for i in range(20):                                   # 2. consolidation, 4 ticks
        t += 100_000_000
        px = 97.50 + (i % 4) * TICK
        sm.on_book(t, px, [ambient] * 10, px + TICK)
        sm.on_trade(t, px, 5, 1 if i % 2 else -1)
    for i in range(8):                                    # 3. blowoff to the V low
        t += 50_000_000
        sm.on_book(t, 97.00 - i * TICK, [trap_depth] * 10, 97.25 - i * TICK)
        if counter_buys:                                  # two-sided => not imbalanced
            # The buy must land BEFORE the sell: the gate is evaluated on every
            # trade, so a sell followed by its offset still reads one-sided at the
            # instant it is tested.
            sm.on_trade(t, 97.00 - i * TICK, counter_buys, +1)
        sm.on_trade(t, 97.00 - i * TICK, blowoff_size, -1)
    for i in range(8):                                    # 4. V retrace at similar speed
        t += 50_000_000
        px = 95.00 + i * 0.375
        sm.on_book(t, px, [ambient] * 10, px + TICK)
        sm.on_trade(t, px, 20, 1)


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    cfg = VRConfig()

    # --- thin impulse ---
    check("thin: fast low-vpt move qualifies",
          is_thin_impulse(10, 30.0, 5.0, 20.0, cfg) is True)
    check("thin: heavy vpt rejected",
          is_thin_impulse(10, 30.0, 19.0, 20.0, cfg) is False)
    check("thin: too small a traverse rejected",
          is_thin_impulse(3, 30.0, 5.0, 20.0, cfg) is False)
    check("thin: too slow rejected",
          is_thin_impulse(10, 120.0, 5.0, 20.0, cfg) is False)

    # --- blowoff ---
    check("blowoff: 1000 vs zone avg 100 qualifies", is_blowoff(1000, 100, cfg) is True)
    check("blowoff: below absolute floor rejected", is_blowoff(200, 10, cfg) is False)
    check("blowoff: not a multiple of zone avg rejected", is_blowoff(300, 200, cfg) is False)

    # --- trapped sellers: EXTREME relative to the session, not an absolute ---
    check("trap: sums exactly the bottom 5 levels",
          trapped_size([100, 200, 300, 400, 500, 9999], cfg) == 1500.0)
    #   T&S imbalance: the doc's 12:30:44 print cascade, all sells below the bid
    win = [(0, 495.75, 126, -1), (0, 496.00, 157, -1), (0, 496.25, 111, -1),
           (0, 496.50, 129, -1), (0, 496.75, 108, -1), (0, 497.00, 149, -1),
           (0, 497.25, 182, -1), (0, 497.75, 100, -1), (0, 498.75, 225, -1)]
    #   band = low + 5 ticks = 495.75 + 1.25 = 497.00 -> first six prints qualify
    check("trap: T&S imbalance sums sells within 5 ticks of the low",
          trap_imbalance(win, 495.75, cfg) == 780.0)
    check("trap: buys at the low NET OFF against sells",
          trap_imbalance(win + [(0, 496.00, 780, +1)], 495.75, cfg) == 0.0)
    check("trap: prints above the band are excluded",
          trap_imbalance([(0, 999.0, 5000, -1)], 495.75, cfg) == 0.0)
    flat = [200.0] * 1000
    check("trap: a wall equal to the p99 is NOT extreme (strict >)",
          is_trap_extreme(200.0, flat, cfg) is False)
    check("trap: a wall well above the p99 IS extreme",
          is_trap_extreme(900.0, flat, cfg) is True)
    check("trap: too little history -> never extreme",
          is_trap_extreme(9_999.0, [200.0] * 10, cfg) is False)
    #   the whole point of the reinterpretation: ES's observed MAX bottom-5 wall is
    #   ~1022, so the doc's literal 3000 could never fire. A percentile still can.
    realistic = [206.0] * 900 + [385.0] * 90 + [700.0] * 10
    check("trap: ES-realistic wall (1022) IS extreme under a percentile",
          is_trap_extreme(1022.0, realistic, cfg) is True)

    # --- retrace ---
    check("retrace: full recovery = 1.0", abs(retrace_frac(90.0, 100.0, 100.0) - 1.0) < 1e-9)
    check("retrace: half recovery = 0.5", abs(retrace_frac(90.0, 100.0, 95.0) - 0.5) < 1e-9)
    check("retrace: no leg -> 0.0", retrace_frac(100.0, 100.0, 105.0) == 0.0)

    # --- magnitude floor: rank alone is not enough ---
    hist = [10.0] * 1000
    check("trap: large one-sided imbalance passes both rank and size",
          is_trap_extreme(600.0, hist, cfg, window_sell_volume=1000.0) is True)
    check("trap: tiny imbalance beats the percentile but FAILS the size floor",
          is_trap_extreme(15.0, hist, cfg, window_sell_volume=1000.0) is False)
    check("trap: with no blowoff volume supplied, rank alone still applies",
          is_trap_extreme(15.0, hist, cfg, window_sell_volume=0.0) is True)

    # --- rolling consolidation window ---
    sm_r = VRStateMachine(VRConfig(cons_min_secs=1.0))
    #   a wide spike must NOT permanently poison the zone: once it ages out of the
    #   rolling window the range is measured on what is currently happening.
    for i in range(5):
        sm_r._roll_range(i * 100_000_000, 100.00 + (5.0 if i == 0 else 0.0))
    hi, lo, _ = sm_r._roll_range(500_000_000, 100.00)
    check("rolling range still sees the spike inside the window", (hi - lo) > 8 * TICK)
    hi, lo, span = sm_r._roll_range(2_000_000_000, 100.00)
    check("rolling range recovers once the spike ages out", (hi - lo) <= 8 * TICK)
    check("span is measured from accumulation start, not the surviving window",
          span >= 1.0)

    # --- config guards ---
    for bad in ({"entry_mode": "knife"}, {"stop_mode": "x"}, {"target_mode": "y"}):
        try:
            VRConfig(**bad)
            ok = False
        except ValueError:
            ok = True
        check(f"config rejects {list(bad)[0]}={list(bad.values())[0]}", ok)

    # --- full FSM on a hand-built synthetic V ---
    sm = VRStateMachine(_synthetic_cfg())
    _drive_v(sm, blowoff_size=100, trap_depth=200)
    check("FSM reached ARMED on the synthetic V", sm.state == VRState.ARMED)
    check("FSM captured thin_origin", sm.zone.get("thin_origin") is not None)
    check("FSM captured cons_hi/cons_lo",
          sm.zone.get("cons_hi") is not None and sm.zone.get("cons_lo") is not None)
    check("FSM captured the V low", sm.zone.get("v_low") is not None)
    check("FSM recorded the trapped size", sm.zone.get("trapped") is not None)

    # --- near-miss: ordinary volume on the down leg is not a blowoff ---
    sm2 = VRStateMachine(_synthetic_cfg())
    _drive_v(sm2, blowoff_size=2, trap_depth=200)
    check("near-miss (no blowoff) never reaches ARMED", sm2.state != VRState.ARMED)

    # --- near-miss: heavy volume at the low but TWO-SIDED -> not imbalanced ---
    #   This is the case the doc's author distinguishes: size alone is not the
    #   signal, one-sidedness at the extreme is. Passes the blowoff volume gate,
    #   must fail the imbalance gate.
    sm3 = VRStateMachine(_synthetic_cfg())
    _drive_v(sm3, blowoff_size=100, trap_depth=200, counter_buys=100)
    check("near-miss (blowoff present but NOT imbalanced) never reaches ARMED",
          sm3.state != VRState.ARMED)

    # --- reset really clears derived state ---
    sm.reset()
    check("reset returns to SEEKING with an empty zone",
          sm.state is VRState.SEEKING and sm.zone == {})

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else 0)
