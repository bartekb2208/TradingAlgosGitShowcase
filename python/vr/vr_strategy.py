"""vr_strategy.py — VR V1 as a NautilusTrader strategy.

Drives the SAME VRStateMachine as vr_detect.py. Detection logic is shared, not
reimplemented; this module adds execution, exits and risk. `vr_agreement.py`
asserts the two drivers find the same structures on the same session.

HARD CONSTRAINT (source doc): targets are ABSOLUTE PRICES. R is only ever a
sizing input. No code path here derives a target from a stop distance, and a
selftest asserts the target does not move when the stop mode changes.

NOT IMPLEMENTED: the falling-knife entry. The source doc says its risk cannot be
defined; encoding it would mean inventing a rule the source declines
to give.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import math
import sys

from nautilus_trader.model.enums import AggressorSide, BookType, OrderSide, TimeInForce
from nautilus_trader.trading.strategy import Strategy

from vr_core import TICK, VRConfig, VRState, VRStateMachine

VR_ENGINE_VERSION = "0.1.0"


class VRStrategy(Strategy):
    """Long-only V-Reversal. One structure at a time, one contract."""

    def __init__(self, instrument, cfg: VRConfig):
        super().__init__()
        self.instrument = instrument
        self.iid = instrument.id
        self.cfg = cfg
        self.tick = float(instrument.price_increment)
        self.sm = VRStateMachine(cfg)
        self.closed_positions: list[dict] = []
        self.n_armed = 0
        self.n_entries = 0
        self.n_fills = 0
        self._entry_id = None
        self._entry_submit_ns = 0
        self._zone: dict = {}
        self._last_entry_ns: int | None = None
        self.n_entry_cancels = 0
        self.n_skipped_would_cross = 0
        self.n_skipped_wide_stop = 0
        self.n_skipped_bad_stop = 0
        self.n_stopped_out = 0
        self._stop_order = None          # live protective stop (trailed in scalp mode)
        self._high_water = None          # best bid seen since entry
        self.n_trail_moves = 0
        self.n_crossed_book = 0
        self.n_skipped_low_rr = 0
        self.n_forced_flat = 0

    # -- pure price selection (unit-tested without an engine) -------------
    def entry_price(self, zone: dict) -> float:
        """Where the passive bid rests inside the consolidation zone.

        SPEC (paraphrased): rest the bid toward the top of the consolidation zone.
        Interpolated by `passive_entry_frac` and snapped DOWN to the tick grid so
        the order stays inside the zone.
        """
        if self.cfg.entry_mode == "marketable":
            return zone["armed_px"]        # crossed at the offer; see _on_armed
        if self.cfg.entry_mode == "breakout_confirmed":
            return zone["cons_hi"] + self.tick
        lo, hi = zone["cons_lo"], zone["cons_hi"]
        raw = lo + self.cfg.passive_entry_frac * (hi - lo)
        return math.floor(raw / self.tick + 1e-9) * self.tick

    def stop_price(self, zone: dict, entry: float | None = None) -> float:
        """Structural stop, optionally capped at a multiple of the zone range."""
        base = zone["v_low"] if self.cfg.stop_mode == "wide_v_low" else zone["cons_lo"]
        if self.cfg.stop_cons_mult is None or entry is None:
            return base
        rng = max(zone["cons_hi"] - zone["cons_lo"], self.tick)
        floor = entry - self.cfg.stop_cons_mult * rng
        return max(base, floor)          # never risk more than N zone-widths

    def reward_risk(self, zone: dict, entry: float) -> float:
        """Reward:risk, reward measured to thin_origin (the doc's structural target).

        Used as a GATE even in scalp_trail mode: thin_origin is the room the setup
        claims is available, so it is the right question to ask before committing,
        independently of how the position is later exited.
        """
        risk = entry - self.stop_price(zone, entry)
        if risk <= 0:
            return 0.0
        return (self.target_price(zone) - entry) / risk

    def trail_distance(self, zone: dict) -> float:
        """How much room the trailing stop gives, in price.

        SPEC (paraphrased): the stop may trail, but must leave the trade room. The room
        is expressed as a multiple of the CONSOLIDATION RANGE — the setup's own
        unit of structure — not a fixed tick count, so it scales with how volatile
        the zone actually was. Floored so a razor-thin zone cannot produce a stop
        that is nailed to the entry.
        """
        rng = max(zone["cons_hi"] - zone["cons_lo"], self.cfg.trail_min_ticks * self.tick)
        return self.cfg.trail_cons_mult * rng

    def target_price(self, zone: dict) -> float:
        """ABSOLUTE price: the origin of the thin move, minus a buffer.

        Deliberately NOT a function of the stop. The source doc puts the target at
        the level price can snap straight back to (the origin of the thin move),
        taking profit a few ticks before it, and treats R purely as a sizing
        input. A selftest asserts this value is
        identical under both stop modes.
        """
        return zone["thin_origin"] - self.cfg.target_buffer_ticks * self.tick

    # -- lifecycle --------------------------------------------------------
    def on_start(self) -> None:
        self.subscribe_order_book_deltas(self.iid, book_type=BookType.L3_MBO)
        self.subscribe_trade_ticks(self.iid)

    def flatten_open(self, reason: str = "session_end") -> int:
        """Force-close anything still open and RECORD it.

        SURVIVORSHIP FIX. Positions still open when the data ends never emit
        PositionClosed, so they never reach `closed_positions` and silently vanish
        from every statistic. That biases results toward configurations that hold
        longest — measured: the trail sweep's closed-trade count fell 5 -> 5 -> 4 ->
        3 -> 2 as the trail widened, with identical entry logic, and the apparent
        PF optimum sat exactly where the most trades were being dropped.

        An unresolved position is not a non-event; it is an open risk with an
        unknown outcome. Marking it to the last bid is the honest accounting.
        """
        n = 0
        for pos in self.cache.positions_open(instrument_id=self.iid):
            mark = self.sm._bid if self.sm._bid is not None else float(pos.avg_px_open)
            pnl = (mark - float(pos.avg_px_open)) * float(self.instrument.multiplier)
            self.closed_positions.append({
                "ts_opened": pos.ts_opened, "ts_closed": None,
                "hold_ms": None,
                "avg_px_open": float(pos.avg_px_open), "avg_px_close": mark,
                "realized_pnl_usd": pnl - self.cfg.commission_per_side * 2,
                "commission_usd": self.cfg.commission_per_side * 2,
                "exit_reason": reason,
                **{k: self._zone.get(k) for k in
                   ("thin_origin", "cons_hi", "cons_lo", "v_low", "retrace_frac", "trapped")},
            })
            n += 1
        self.n_forced_flat += n
        return n

    def on_stop(self) -> None:
        self.flatten_open()

    def on_reset(self) -> None:
        self.sm.reset()
        self.closed_positions = []
        self.n_armed = self.n_entries = self.n_fills = 0
        self._entry_id = None
        self._entry_submit_ns = 0
        self._zone = {}
        self._last_entry_ns = None
        self.n_entry_cancels = 0
        self.n_skipped_would_cross = 0
        self.n_skipped_wide_stop = 0
        self.n_skipped_bad_stop = 0
        self.n_stopped_out = 0
        self._stop_order = None          # live protective stop (trailed in scalp mode)
        self._high_water = None          # best bid seen since entry
        self.n_trail_moves = 0
        self.n_crossed_book = 0
        self.n_skipped_low_rr = 0
        self.n_forced_flat = 0

    def on_order_book_deltas(self, deltas) -> None:
        book = self.cache.order_book(self.iid)
        if book is None:
            return
        bp, ap = book.best_bid_price(), book.best_ask_price()
        if bp is None or ap is None:
            return
        # CROSSED-BOOK GUARD. A book with ask <= bid is a reconstruction artifact,
        # never a tradeable state. Measured on ES L3: 0.3% of updates on healthy
        # sessions (min spread -23 ticks) but 82.8% on 2026-07-10, where a phantom
        # offer sat ~93-179 ticks below the bid all session and poisoned every
        # ask-priced entry. Feeding this to the FSM or pricing an order off it
        # produces confident nonsense, so drop the update and count it.
        if float(ap) <= float(bp):
            self.n_crossed_book += 1
            return
        self._trail(float(bp))
        # PERF: bid_sizes is deliberately EMPTY. The trap gate moved from resting
        # book depth to T&S imbalance, which reads the trade window, not the book —
        # so `_bid_sizes` became write-only. Materialising book.bids() on all ~9.4M
        # delta batches cost ~33 min/session for data nothing consumed. Re-enable
        # selectively (only while CONSOLIDATING) if absorption metrics need depth.
        now = int(self.clock.timestamp_ns())
        self.sm.on_book(now, float(bp), (), float(ap))
        self._ask_now = float(ap)
        # FIX 3: expire stale entries. Without this a resting order from a dead
        # structure survives the session and fills on something unrelated.
        if (self._entry_id is not None
                and now - self._entry_submit_ns > self.cfg.entry_ttl_secs * 1e9):
            self.cancel_all_orders(self.iid)
            self._entry_id = None
            self.n_entry_cancels += 1

    def _trail(self, bid: float) -> None:
        """Ratchet the protective stop up behind the best bid. Never down."""
        if self.cfg.target_mode != "scalp_trail" or self._stop_order is None:
            return
        if self._high_water is None or bid <= self._high_water:
            return
        self._high_water = bid
        want = bid - self.trail_distance(self._zone)
        cur = float(self._stop_order.trigger_price)
        if want - cur < self.cfg.trail_step_ticks * self.tick:
            return                       # avoid churning the order on every tick
        try:
            self.modify_order(self._stop_order,
                              trigger_price=self.instrument.make_price(want))
            self.n_trail_moves += 1
        except Exception:
            pass                         # order already filled/cancelled: nothing to trail

    def on_trade_tick(self, tick) -> None:
        ts = int(tick.ts_event)
        # Compare the ENUM, never its string form: str(AggressorSide.SELLER) is
        # '2', not 'AggressorSide.SELLER'. A string test silently classifies every
        # trade as a buy, which empties the sell window and makes the blowoff gate
        # unreachable -- the engine reported 0 structures where the offline
        # detector found 3, and vr_agreement.py is what caught it.
        side = -1 if tick.aggressor_side == AggressorSide.SELLER else 1
        if self.sm.on_trade(ts, float(tick.price), float(tick.size), side) is VRState.ARMED:
            self.n_armed += 1
            self._on_armed(ts, dict(self.sm.zone))
            self.sm.reset()

    def _cooldown_ok(self, ts: int) -> bool:
        return self._last_entry_ns is None or (ts - self._last_entry_ns) >= self.cfg.cooldown_ns

    def _on_armed(self, ts: int, zone: dict) -> None:
        if not (self.portfolio.is_flat(self.iid) and self._entry_id is None
                and self._cooldown_ok(ts)):
            return
        ask = getattr(self, "_ask_now", None)
        marketable = self.cfg.entry_mode == "marketable"
        if marketable:
            if ask is None:
                return
            px = ask                       # cross the offer: guaranteed fill, known cost
        else:
            px = self.entry_price(zone)
            # A "passive" entry that crosses is not passive.
            if ask is not None and px >= ask:
                self.n_skipped_would_cross += 1
                return
        # Risk filter: bound per-trade loss before committing.
        stop = self.stop_price(zone, px)
        if self.cfg.min_rr is not None and self.reward_risk(zone, px) < self.cfg.min_rr:
            self.n_skipped_low_rr += 1
            return
        if self.cfg.max_stop_ticks is not None and (px - stop) / self.tick > self.cfg.max_stop_ticks:
            self.n_skipped_wide_stop += 1
            return
        if stop >= px:
            self.n_skipped_bad_stop += 1   # stop must sit BELOW the entry
            return
        self._zone = zone
        order = self.order_factory.limit(
            self.iid, OrderSide.BUY, self.instrument.make_qty(1),
            self.instrument.make_price(px),
            time_in_force=TimeInForce.GTC, post_only=not marketable)
        self.submit_order(order)
        self._entry_id = order.client_order_id
        self._entry_submit_ns = ts
        self._last_entry_ns = ts
        self.n_entries += 1

    def on_order_filled(self, event) -> None:
        if event.order_side != OrderSide.BUY:
            return
        self.n_fills += 1
        self._entry_id = None
        self._high_water = float(event.last_px)
        # SCALP_TRAIL: no fixed target at all. The doc's first target is a scalp that
        # is held until order flow and price turn decisively against it, so the
        # trailing stop IS the exit. Crucially this makes the
        # exit independent of the thin_origin/v_low geometry, which is what produced
        # 92-ticks-risked-for-14 under thin_origin_buffered.
        if self.cfg.target_mode == "thin_origin_buffered":
            self.submit_order(self.order_factory.limit(
                self.iid, OrderSide.SELL, self.instrument.make_qty(1),
                self.instrument.make_price(self.target_price(self._zone)),
                time_in_force=TimeInForce.GTC, reduce_only=True))
        # Protective stop in BOTH modes. The doc requires a defined risk on
        # every trade; the first cut placed only a target, leaving positions naked.
        # INITIAL STOP = the TIGHTER of the structural stop and the trail's own
        # distance. Previously the structural stop was used alone, and the trail
        # then tightened it to `bid - trail_room` on the very first uptick -- so a
        # separate `stop_cons_mult` cap measured in the same units as the trail was
        # redundant by construction (measured: byte-identical results with and
        # without it). The residual risk the cap was meant to cover is a move that
        # goes straight against the fill before any uptick, so make that explicit
        # here rather than leaving it to emerge from ordering.
        entry_px = float(event.last_px)
        init_stop = max(self.stop_price(self._zone, entry_px),
                        entry_px - self.trail_distance(self._zone))
        stop = self.order_factory.stop_market(
            self.iid, OrderSide.SELL, self.instrument.make_qty(1),
            trigger_price=self.instrument.make_price(init_stop),
            time_in_force=TimeInForce.GTC, reduce_only=True)
        self.submit_order(stop)
        self._stop_order = stop

    def _clear_exit_state(self) -> None:
        self._stop_order = None
        self._high_water = None

    def on_position_closed(self, event) -> None:
        # Accumulate HERE. cache.positions_closed() reuses one PositionId under
        # NETTING and returns a single record however many round trips occurred
        # (measured on MR V2: 28 events, cache reported 1). The event stream is
        # the population; the cache is current state.
        pos = self.cache.position(event.position_id)
        if pos is not None:
            self.closed_positions.append({
                "ts_opened": pos.ts_opened, "ts_closed": pos.ts_closed,
                "hold_ms": (pos.ts_closed - pos.ts_opened) / 1e6 if pos.ts_closed else None,
                "avg_px_open": float(pos.avg_px_open),
                "avg_px_close": float(pos.avg_px_close) if pos.avg_px_close else None,
                "realized_pnl_usd": float(pos.realized_pnl) if pos.realized_pnl is not None else 0.0,
                "commission_usd": sum(float(c) for c in pos.commissions()),
                "exit_reason": "stop_or_target",
                **{k: self._zone.get(k) for k in
                   ("thin_origin", "cons_hi", "cons_lo", "v_low", "retrace_frac", "trapped")},
            })
        self._entry_id = None
        self._clear_exit_state()


class _StubPrices:
    """Stand-in so the price methods are testable with no engine.

    Borrows the real methods unbound, so the tests exercise production code
    rather than a reimplementation of it.
    """

    def __init__(self, cfg: VRConfig):
        self.cfg = cfg
        self.tick = TICK

    entry_price = VRStrategy.entry_price
    stop_price = VRStrategy.stop_price
    target_price = VRStrategy.target_price
    trail_distance = VRStrategy.trail_distance
    reward_risk = VRStrategy.reward_risk


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    # aggressor mapping — the bug vr_agreement.py caught
    check("SELLER maps to -1 via enum comparison",
          (-1 if AggressorSide.SELLER == AggressorSide.SELLER else 1) == -1)
    check("BUYER maps to +1 via enum comparison",
          (-1 if AggressorSide.BUYER == AggressorSide.SELLER else 1) == 1)
    check("str(AggressorSide.SELLER) is NOT a name — string tests are unsafe",
          str(AggressorSide.SELLER) == "2" and not str(AggressorSide.SELLER).endswith("SELLER"))

    zone = {"thin_origin": 110.00, "cons_hi": 100.00, "cons_lo": 99.00,
            "v_low": 97.00, "armed_px": 99.50}

    check("entry_mode defaults to marketable (passive is geometrically impossible)",
          VRConfig().entry_mode == "marketable")
    check("stop_mode defaults to wide_v_low (narrow sits above a marketable entry)",
          VRConfig().stop_mode == "wide_v_low")
    s = _StubPrices(VRConfig(stop_mode="narrow_cons_lo"))
    check("narrow stop sits at cons_lo", VRStrategy.stop_price(s, zone) == 99.00)
    s = _StubPrices(VRConfig(stop_mode="wide_v_low"))
    check("wide stop sits at the V low", VRStrategy.stop_price(s, zone) == 97.00)

    s = _StubPrices(VRConfig(target_mode="thin_origin_buffered", target_buffer_ticks=2))
    check("target is thin_origin minus the buffer",
          abs(VRStrategy.target_price(s, zone) - 109.50) < 1e-9)

    # THE hard constraint: the target must not move when the stop moves.
    s_narrow = _StubPrices(VRConfig(stop_mode="narrow_cons_lo"))
    s_wide = _StubPrices(VRConfig(stop_mode="wide_v_low"))
    check("target is INDEPENDENT of stop distance (no R multiple)",
          VRStrategy.target_price(s_narrow, zone) == VRStrategy.target_price(s_wide, zone))
    #   and the stop genuinely differs between those two, so the test has teeth
    check("the two stop modes really do differ",
          VRStrategy.stop_price(s_narrow, zone) != VRStrategy.stop_price(s_wide, zone))

    #   passive entry must rest toward the TOP of the zone (spec), inside it
    s = _StubPrices(VRConfig(entry_mode="passive_zone", passive_entry_frac=0.75))
    e = VRStrategy.entry_price(s, zone)
    check("passive entry rests in the upper part of the zone", e == 99.75)
    check("passive entry stays INSIDE the zone", zone["cons_lo"] <= e <= zone["cons_hi"])
    check("passive entry is on the tick grid", abs(e / TICK - round(e / TICK)) < 1e-9)
    s0 = _StubPrices(VRConfig(entry_mode="passive_zone", passive_entry_frac=0.0))
    check("frac 0.0 reproduces the old cons_lo behaviour",
          VRStrategy.entry_price(s0, zone) == 99.00)
    check("upper-end entry is strictly better placed than cons_lo",
          VRStrategy.entry_price(s, zone) > VRStrategy.entry_price(s0, zone))
    s = _StubPrices(VRConfig(entry_mode="breakout_confirmed"))
    check("breakout entry is above cons_hi",
          VRStrategy.entry_price(s, zone) > 100.00)

    #   reward must exceed risk for the trade to be structurally coherent
    s = _StubPrices(VRConfig(stop_mode="narrow_cons_lo"))
    e, st, tg = (VRStrategy.entry_price(s, zone), VRStrategy.stop_price(s, zone),
                 VRStrategy.target_price(s, zone))
    check("target is above entry and stop is at-or-below it", tg > e >= st)

    #   VR-specific config: the inherited MR V2 cooldown must NOT be carried over
    check("cooldown defaults to 0 for VR (structures are independent by construction)",
          VRConfig().cooldown_ns == 0)
    check("entry TTL is set (no immortal resting orders)", VRConfig().entry_ttl_secs > 0)

    # --- scalp_trail: the exit must NOT depend on thin_origin/v_low geometry ---
    check("target_mode now defaults to scalp_trail", VRConfig().target_mode == "scalp_trail")
    st = _StubPrices(VRConfig(trail_cons_mult=1.5, trail_min_ticks=4))
    #   zone range = 100.00 - 99.00 = 1.00 -> trail = 1.5 x 1.00 = 1.50
    check("trail room scales with the consolidation range",
          abs(VRStrategy.trail_distance(st, zone) - 1.50) < 1e-9)
    #   a razor-thin zone must be floored, not nailed to the entry
    thin = {"cons_hi": 100.00, "cons_lo": 100.00}
    check("trail room is floored for a zero-width zone",
          abs(VRStrategy.trail_distance(st, thin) - 1.5 * 4 * TICK) < 1e-9)
    check("wider zone gives strictly more room",
          VRStrategy.trail_distance(st, {"cons_hi": 105.0, "cons_lo": 99.0})
          > VRStrategy.trail_distance(st, zone))
    #   THE point of scalp_trail: identical trail regardless of where thin_origin sits
    far = dict(zone, thin_origin=200.00)
    # --- TIER 1: RR gate and zone-relative stop cap ---
    t1 = _StubPrices(VRConfig(stop_mode="wide_v_low", stop_cons_mult=3.0,
                              target_buffer_ticks=2))
    z = {"thin_origin": 110.00, "cons_hi": 100.00, "cons_lo": 99.00, "v_low": 90.00}
    #   zone range 1.00, cap 3x -> stop floor = entry - 3.00 = 96.00, beats v_low 90.00
    check("zone-relative cap tightens a runaway stop",
          VRStrategy.stop_price(t1, z, 99.00) == 96.00)
    #   when v_low is already tighter than the cap, keep v_low
    z2 = dict(z, v_low=98.50)
    check("cap never LOOSENS a stop that is already tight",
          VRStrategy.stop_price(t1, z2, 99.00) == 98.50)
    t0 = _StubPrices(VRConfig(stop_mode="wide_v_low"))
    #   the initial stop must never be looser than the trail would immediately make it
    tz = _StubPrices(VRConfig(stop_mode="wide_v_low", trail_cons_mult=3.0))
    zz = {"thin_origin": 110.0, "cons_hi": 100.0, "cons_lo": 99.0, "v_low": 90.0}
    struct = VRStrategy.stop_price(tz, zz, 99.00)
    trail_floor = 99.00 - VRStrategy.trail_distance(tz, zz)
    check("initial stop = tighter of structural and trail (no redundant cap needed)",
          max(struct, trail_floor) == trail_floor and trail_floor > struct)

    check("cap disabled reproduces the raw structural stop",
          VRStrategy.stop_price(t0, z, 99.00) == 90.00)
    #   RR: reward to thin_origin(110) - buffer(0.50) = 109.50; entry 99, stop 96 -> 3.50
    check("reward:risk measured to thin_origin",
          abs(VRStrategy.reward_risk(t1, z, 99.00) - (109.50 - 99.00) / 3.00) < 1e-9)
    #   the measured killer: risk 92 ticks to make 14 -> RR 0.15, must be rejected at 1.0
    bad = {"thin_origin": 99.00 + 14 * TICK, "cons_hi": 99.0, "cons_lo": 99.0,
           "v_low": 99.00 - 92 * TICK}
    tb = _StubPrices(VRConfig(stop_mode="wide_v_low", target_buffer_ticks=0))
    check("the 92-risk-for-14 trade scores RR < 1.0",
          VRStrategy.reward_risk(tb, bad, 99.00) < 1.0)

    check("trail room is INDEPENDENT of thin_origin (geometry-free exit)",
          VRStrategy.trail_distance(st, far) == VRStrategy.trail_distance(st, zone))

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else 0)
