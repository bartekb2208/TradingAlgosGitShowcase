"""v2_core.py — shared building blocks for the V2 order-book-exhaustion strategies.

Spec of record: my own research notes (not published), section
"Mean Reversion system 2: Order book Exhaustion". That doc is the MECHANICAL
setup (divergence / OBI / Hawkes exhaustion / convergence exit). It is NOT the
same hypothesis as Cody Bengtson's *V-Reversal Setup* (2023; third-party document, not redistributed here), which is a
discretionary footprint method (consolidation zones, blowoff, trapped traders,
structural stops) and lives in `python/vr/v_reversal_prototype.py`. Do not merge them.

Contains:
  * load_trade_ticks : L2 trade-tape parquet (mbo_to_l2 output) -> Nautilus TradeTick list
  * micro_price / book_imbalance : pure microstructure metrics
  * RollingPercentile : online window tracker for the p99/p90 conjunction thresholds
  * V2StrategyBase : execution / exits / risk, shared by both entry variants

The metric functions stay pure so they can be unit-tested without an engine.
"""
from __future__ import annotations

import math
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide, BookType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy

# Bump on ANY change to signal, execution, exit or risk semantics. Stamped into
# every backtest artifact; results carrying different versions are not comparable.
V2_ENGINE_VERSION = "1.1.0"

_ET = ZoneInfo("America/New_York")
NS = 1_000_000_000


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class V2Config:
    """Every tunable. Serialized into each backtest artifact for reproducibility.

    `SPEC:` marks a value taken verbatim from the research-notes doc.
    `JUDGMENT:` marks a choice the doc does not make — these are the ones that
    need calibrating, and the ones a falsification argument can attack.
    """
    # --- conjunction entry (VREVERSAL prereg) ---------------------------------
    window_ms: int = 1000            # rolling window for blowoff / imbalance percentiles
    sell_pct: float = 0.99           # SPEC(prereg): aggressive-sell-volume p99
    imb_pct: float = 0.90            # SPEC(prereg): book-imbalance p90
    hist: int = 4000                 # JUDGMENT: rolling-percentile sample size

    # --- Hawkes entry (research-notes doc) ------------------------------------
    divergence_bps: float = 3.0      # SPEC: last trade >= 3 BASIS POINTS below micro-price.
    #   The doc says "3 basis points", i.e. RELATIVE to price (0.03%), not absolute
    #   ticks. On ES @ ~7561 that is 2.27 points = ~9.1 ticks. An earlier reading of
    #   "3 points" (= 12 ticks) was wrong. bps is also the right unit: it stays
    #   comparable across price levels and across ES/NQ.
    obi_threshold: float = 0.30      # SPEC: OBI > 0.30 ("brick wall of buyers")
    lam_tau_ns: int = 50_000_000     # JUDGMENT: 50 ms sell-intensity decay constant
    peak_window_ns: int = 100_000_000  # SPEC: lambda peak measured over a 100 ms window
    exhaustion_frac: float = 0.5     # SPEC: lambda dropped to <= 50% of its recent peak
    min_peak: float = 3.0            # JUDGMENT: require a real sell burst first

    # --- execution / exits ----------------------------------------------------
    exit_mode: str = "convergence"   # SPEC: "convergence" | "fixed_ticks" (legacy v0.1)
    #   convergence  = rest the profit target at the micro-price observed at fill.
    #                  The dislocation IS the edge, so the target must scale with it.
    #   fixed_ticks  = the old +1-tick cap. Kept ONLY to reproduce v0.1 numbers.
    fixed_target_ticks: int = 1      # used only when exit_mode == "fixed_ticks"
    min_target_ticks: int = 1        # a sell target must sit >= this above our fill
    exit_timeout_ns: int = 2 * NS    # SPEC: 2-second time stop
    entry_ttl_ns: int = 300_000_000  # JUDGMENT: cancel an unfilled entry after 300 ms

    # --- risk -----------------------------------------------------------------
    spread_abort_mult: float = 3.0   # SPEC: abort if spread > 3x rolling avg
    max_positions: int = 1           # SPEC: inventory limit
    cooldown_ns: int = 180 * NS      # SPEC(prereg): 180 s event-independence spacing.
    #   The prereg's +1.60-tick result is measured on events >= 180 s apart; without
    #   the same spacing the strategy trades a different (unvalidated) population.

    # --- session --------------------------------------------------------------
    rth_only: bool = True            # SPEC(prereg): "RTH only"
    rth_start_et: time = time(9, 30)
    rth_end_et: time = time(16, 0)

    # --- costs ----------------------------------------------------------------
    commission_per_side: float = 2.50  # JUDGMENT: USD/contract/side, all-in retail
    #   (CME exchange+clearing ~$1.33 + FCM commission). $5.00 round turn = 0.4 ES ticks.

    # --- adverse-selection controls (docs/RISK_MANAGEMENT_OPTIONS.md 2.2 / 2.4) --
    # BOTH DEFAULT OFF. With both off this engine reproduces v1.0.2 EXACTLY, so
    # they can be A/B'd against the existing baseline rather than confounding it.
    # Picked from measured mechanism, not from sweeping a menu:
    #   - OBI at fill has median 0.000 against an entry requirement of >= p90
    #   - 96.3% of fills occur at spread > 1 tick vs a session-typical 1 tick
    cancel_on_obi_decay: bool = False   # 2.2: kill a live entry if the bid wall evaporates
    obi_cancel_floor: float = 0.0       #      cancel when current OBI drops below this
    max_entry_spread_ticks: float | None = None   # 2.4: absolute spread gate, in ticks
    #   The existing `spread_abort_mult` is RELATIVE to a rolling EWMA of ~1 tick,
    #   so its 3x trigger sits near 3 ticks and essentially never binds at the
    #   2.06-tick spread where fills actually happen. This is the absolute gate.

    def __post_init__(self) -> None:
        if self.exit_mode not in ("convergence", "fixed_ticks"):
            raise ValueError(f"exit_mode must be 'convergence' or 'fixed_ticks', got {self.exit_mode!r}")

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["rth_start_et"] = self.rth_start_et.isoformat()
        d["rth_end_et"] = self.rth_end_et.isoformat()
        d["engine_version"] = V2_ENGINE_VERSION
        return d


# ---------------------------------------------------------------------------
# Trade-tape converter (L2 parquet -> Nautilus TradeTick)
# ---------------------------------------------------------------------------
def load_trade_ticks(parquet: str, instrument_id: InstrumentId,
                     price_precision: int = 2, size_precision: int = 0,
                     limit: int | None = None) -> list[TradeTick]:
    """Convert an mbo_to_l2 trade tape (ts,price,size,aggressor) to TradeTicks."""
    df = pd.read_parquet(parquet)
    if limit is not None:
        df = df.head(limit)
    ticks: list[TradeTick] = []
    for i, (ts, price, size, aggr) in enumerate(
        zip(df["ts"], df["price"], df["size"], df["aggressor"])
    ):
        side = AggressorSide.SELLER if int(aggr) < 0 else AggressorSide.BUYER
        t = int(ts)
        ticks.append(TradeTick(
            instrument_id=instrument_id,
            price=Price(float(price), price_precision),
            size=Quantity(int(size), size_precision),
            aggressor_side=side,
            trade_id=TradeId(str(i)),
            ts_event=t,
            ts_init=t,
        ))
    return ticks


# ---------------------------------------------------------------------------
# Pure microstructure metrics
# ---------------------------------------------------------------------------
def micro_price(bid_px: float, bid_sz: float, ask_px: float, ask_sz: float) -> float:
    """Size-weighted mid: leans toward the side with LESS size (more pressure)."""
    tot = bid_sz + ask_sz
    if tot <= 0:
        return (bid_px + ask_px) / 2.0
    return (bid_px * ask_sz + ask_px * bid_sz) / tot


def book_imbalance(bid_sz: float, ask_sz: float) -> float:
    """(bid - ask) / (bid + ask) in [-1, 1]; positive = bid-heavy (selling absorbed)."""
    tot = bid_sz + ask_sz
    return 0.0 if tot <= 0 else (bid_sz - ask_sz) / tot


def rth_bounds_ns(ts_ns: int, start_et: time, end_et: time) -> tuple[int, int]:
    """[start, end) of the ET session containing `ts_ns`, as epoch nanoseconds.

    Resolved through the America/New_York zone so US DST is handled by the
    tzdb rather than by a hardcoded UTC offset (the ES tape shifts between
    13:30 and 14:30 UTC across the year).
    """
    d = datetime.fromtimestamp(ts_ns // NS, tz=timezone.utc).astimezone(_ET).date()
    lo = int(datetime.combine(d, start_et, tzinfo=_ET).timestamp()) * NS
    hi = int(datetime.combine(d, end_et, tzinfo=_ET).timestamp()) * NS
    return lo, hi


class RollingPercentile:
    """Online percentile over the last `maxlen` samples (for the p99/p90 gates)."""

    def __init__(self, maxlen: int):
        self._buf: deque[float] = deque(maxlen=maxlen)

    def add(self, x: float) -> None:
        self._buf.append(float(x))

    def quantile(self, q: float) -> float:
        if not self._buf:
            return float("inf")
        return float(np.quantile(np.fromiter(self._buf, dtype=float), q))

    def __len__(self) -> int:
        return len(self._buf)


# ---------------------------------------------------------------------------
# Strategy base — execution / exits / risk shared by both entry variants
# ---------------------------------------------------------------------------
class V2StrategyBase(Strategy):
    """Passive-limit order-book-exhaustion LONG.

    On the entry signal, join the best bid. On fill, rest the reversion target at
    the micro-price observed at fill time (spec: "exit ... the millisecond last
    traded price converges back to the micro price"). Exit on a 2-second timeout
    if convergence has not happened. Subclasses implement `entry_signal`.

    Engine v1.0.0 — frozen. See `research/v2-order-book/ENGINE_FROZEN.md`. Sizing is 1 contract by
    design: this measures whether the edge exists per-contract, net of real
    commission. Position sizing is a separate question and is deliberately absent.
    """

    def __init__(self, instrument, cfg: V2Config):
        super().__init__()
        self.instrument = instrument
        self.iid = instrument.id
        self.cfg = cfg
        self.tick = float(instrument.price_increment)
        # metric state
        self._sells: deque[tuple[int, float]] = deque()
        self._sell_pctl = RollingPercentile(cfg.hist)
        self._imb_pctl = RollingPercentile(cfg.hist)
        self._spread_ewma: float | None = None
        self._lam = 0.0                                    # sell-intensity lambda(t)
        self._lam_ts = 0
        self._lam_peak: deque[tuple[int, float]] = deque() # (ts, lambda) over peak window
        self._last_trade_px: float | None = None
        self.bid = self.ask = self.bid_sz = self.ask_sz = None
        # order/position state
        self._entry_id = None
        self._entry_submit_ns = 0
        self._exit_deadline_ns: int | None = None
        self._last_entry_ns: int | None = None             # cooldown anchor
        # session state (RTH bounds cached per calendar day)
        self._rth_day_ns: int | None = None
        self._rth_lo = self._rth_hi = 0
        # counters / diagnostics.
        # NOTE ON SEMANTICS: the `blocked_*` counters count TRADE TICKS on which a
        # gate was closed — they are a duty-cycle measure, NOT a count of suppressed
        # signals. The entry signal is only evaluated after every gate passes (the
        # p99 quantile is expensive), so a signal that never got evaluated is never
        # counted. Do not read `blocked_cooldown` as "signals thrown away".
        self.n_entries = 0
        self.blocked_ticks_rth = 0
        self.blocked_ticks_cooldown = 0
        self.blocked_ticks_spread = 0
        self.blocked_ticks_wide_spread = 0   # risk 2.4: absolute spread gate
        self.n_cancel_obi_decay = 0          # risk 2.2: entry killed, bid wall gone
        self.n_fills = 0
        self.n_entry_cancels = 0
        self.n_timeout_exits = 0
        self.entry_log: list[dict] = []
        self.closed_positions: list[dict] = []   # authoritative round-trip population
        self._timeout_pending = False
        self._pos_entry_px: float | None = None  # excursion tracking (diagnostic)
        self._pos_mfe = 0.0
        self._pos_mae = 0.0
        self._pos_ctx: dict = {}

    def on_start(self) -> None:
        self.subscribe_order_book_deltas(self.iid, book_type=BookType.L3_MBO)
        self.subscribe_trade_ticks(self.iid)

    def on_reset(self) -> None:
        """Clear ALL derived state so a reused engine cannot leak across runs.

        Nautilus retains loaded strategies across `BacktestEngine.reset()`. Any
        field left un-cleared here silently carries percentile history, lambda
        state or counters from the previous config into the next one — which
        looks like a real difference between configs and is not.
        """
        self._sells.clear()
        self._sell_pctl = RollingPercentile(self.cfg.hist)
        self._imb_pctl = RollingPercentile(self.cfg.hist)
        self._spread_ewma = None
        self._lam = 0.0
        self._lam_ts = 0
        self._lam_peak.clear()
        self._last_trade_px = None
        self.bid = self.ask = self.bid_sz = self.ask_sz = None
        self._entry_id = None
        self._entry_submit_ns = 0
        self._exit_deadline_ns = None
        self._last_entry_ns = None
        self._rth_day_ns = None
        self._rth_lo = self._rth_hi = 0
        self.n_entries = self.n_fills = self.n_entry_cancels = self.n_timeout_exits = 0
        self.blocked_ticks_rth = self.blocked_ticks_cooldown = self.blocked_ticks_spread = 0
        self.blocked_ticks_wide_spread = 0
        self.n_cancel_obi_decay = 0
        self.entry_log = []
        self.closed_positions = []
        self._timeout_pending = False
        self._pos_entry_px = None
        self._pos_mfe = self._pos_mae = 0.0
        self._pos_ctx = {}

    # -- book updates: refresh top-of-book, spread, imbalance; manage orders --
    def on_order_book_deltas(self, deltas) -> None:
        book = self.cache.order_book(self.iid)
        if book is None:
            return
        bp, ap = book.best_bid_price(), book.best_ask_price()
        if bp is None or ap is None:
            return
        self.bid, self.ask = float(bp), float(ap)
        self.bid_sz = float(book.best_bid_size() or 0.0)
        self.ask_sz = float(book.best_ask_size() or 0.0)
        spr = self.ask - self.bid
        self._spread_ewma = spr if self._spread_ewma is None else 0.995 * self._spread_ewma + 0.005 * spr
        self._imb_pctl.add(book_imbalance(self.bid_sz, self.ask_sz))
        # Excursion tracking while a position is open (DIAGNOSTIC ONLY — never
        # read by any trading decision). MFE/MAE are what distinguish "the edge is
        # absent" from "the edge is there but the exit gives it back".
        if self._pos_entry_px is not None:
            self._pos_mfe = max(self._pos_mfe, self.bid - self._pos_entry_px)
            self._pos_mae = min(self._pos_mae, self.bid - self._pos_entry_px)
        self._manage_orders()

    # -- trade updates: roll aggressive-sell volume, test entry --
    def on_trade_tick(self, tick) -> None:
        ts = int(tick.ts_event)
        self._last_trade_px = float(tick.price)
        # sell-intensity lambda(t): decay to now, bump on aggressive sells, track recent peak
        if self._lam_ts:
            self._lam *= math.exp(-(ts - self._lam_ts) / self.cfg.lam_tau_ns)
        self._lam_ts = ts
        if tick.aggressor_side == AggressorSide.SELLER:
            self._lam += 1.0
            self._sells.append((ts, float(tick.size)))
        self._lam_peak.append((ts, self._lam))
        pcut = ts - self.cfg.peak_window_ns
        while self._lam_peak and self._lam_peak[0][0] < pcut:
            self._lam_peak.popleft()
        # rolling aggressive-sell volume (for the conjunction percentile gate)
        cutoff = ts - self.cfg.window_ms * 1_000_000
        while self._sells and self._sells[0][0] < cutoff:
            self._sells.popleft()
        sell_vol = sum(s for _, s in self._sells)
        self._sell_pctl.add(sell_vol)
        if self._can_enter(ts) and self.entry_signal(sell_vol):
            self._enter(ts)

    # -- gates --
    def _warm(self) -> bool:
        return len(self._sell_pctl) > 1000 and len(self._imb_pctl) > 1000 and self.bid is not None

    def _spread_ok(self) -> bool:
        return self._spread_ewma is None or (self.ask - self.bid) <= self.cfg.spread_abort_mult * self._spread_ewma

    def in_rth(self, ts: int) -> bool:
        """SPEC(prereg): entries only inside the RTH cash session.

        The book is deliberately NOT gated — it must replay the whole session to
        warm up, or orders resting from before the window would be invisible.
        Only the SIGNAL is gated.
        """
        if not self.cfg.rth_only:
            return True
        day = ts // (86_400 * NS)
        if day != self._rth_day_ns:
            self._rth_lo, self._rth_hi = rth_bounds_ns(ts, self.cfg.rth_start_et, self.cfg.rth_end_et)
            self._rth_day_ns = day
        return self._rth_lo <= ts < self._rth_hi

    def _cooldown_ok(self, ts: int) -> bool:
        """SPEC(prereg): events must be >= cooldown_ns apart to count as independent."""
        return self._last_entry_ns is None or (ts - self._last_entry_ns) >= self.cfg.cooldown_ns

    def _can_enter(self, ts: int) -> bool:
        if not (self._warm() and self.portfolio.is_flat(self.iid)
                and self._entry_id is None and self._exit_deadline_ns is None):
            return False
        if not self.in_rth(ts):
            self.blocked_ticks_rth += 1
            return False
        if not self._cooldown_ok(ts):
            self.blocked_ticks_cooldown += 1
            return False
        if not self._spread_ok():
            self.blocked_ticks_spread += 1
            return False
        if not self._entry_spread_ok():
            self.blocked_ticks_wide_spread += 1
            return False
        return True

    def _entry_spread_ok(self) -> bool:
        """RISK 2.4 — absolute spread gate (None = disabled, v1.0.2 behaviour).

        Distinct from `_spread_ok`, which is RELATIVE to a rolling EWMA and so
        tracks the spread upward instead of refusing it. 96.3% of measured fills
        happened at spread > 1 tick, so an absolute ceiling is the control that
        actually bites.
        """
        cap = self.cfg.max_entry_spread_ticks
        if cap is None or self.bid is None:
            return True
        return (self.ask - self.bid) / self.tick <= cap + 1e-9

    # -- signal helpers (used by subclasses) --
    def imbalance(self) -> float:
        return book_imbalance(self.bid_sz, self.ask_sz)

    def sell_p99(self) -> float:
        return self._sell_pctl.quantile(self.cfg.sell_pct)

    def imb_p90(self) -> float:
        return self._imb_pctl.quantile(self.cfg.imb_pct)

    def micro_price_now(self) -> float | None:
        if self.bid is None:
            return None
        return micro_price(self.bid, self.bid_sz, self.ask, self.ask_sz)

    def divergence_ticks(self) -> float:
        """How many ticks the last trade sits BELOW the micro-price (>0 = dislocated down)."""
        mp = self.micro_price_now()
        if mp is None or self._last_trade_px is None:
            return 0.0
        return (mp - self._last_trade_px) / self.tick

    def divergence_bps(self) -> float:
        """Divergence below micro-price in BASIS POINTS — the spec's unit.

        (micro - last_trade) / micro * 10_000. Relative, so it is comparable
        across price levels and instruments; the spec threshold is 3 bps.
        """
        mp = self.micro_price_now()
        if mp is None or self._last_trade_px is None or mp <= 0:
            return 0.0
        return (mp - self._last_trade_px) / mp * 10_000.0

    def intensity_exhausted(self) -> bool:
        """Sell intensity lambda(t) has dropped to <= exhaustion_frac of its recent peak."""
        if not self._lam_peak:
            return False
        peak = max(v for _, v in self._lam_peak)
        return peak >= self.cfg.min_peak and self._lam <= self.cfg.exhaustion_frac * peak

    def entry_signal(self, sell_vol: float) -> bool:
        raise NotImplementedError

    # -- execution / exits --
    def _snap_up(self, px: float) -> float:
        """Round a sell target UP onto the tick grid (never offer below target)."""
        return math.ceil(px / self.tick - 1e-9) * self.tick

    def exit_target(self, entry_px: float, micro_at_fill: float | None) -> float:
        """The reversion target.

        SPEC: exit when the traded price converges back to the micro-price, so the
        target IS the micro-price at fill — it scales with the dislocation that
        justified the trade. A fixed cap would keep the losers (2-second timeout
        exits at market) while truncating exactly the right tail that produces the
        measured +1.60-tick mean.

        Floored at entry + `min_target_ticks` so the order is always a valid
        passive offer above our own fill.
        """
        floor = entry_px + self.cfg.min_target_ticks * self.tick
        if self.cfg.exit_mode == "fixed_ticks":
            return self._snap_up(entry_px + self.cfg.fixed_target_ticks * self.tick)
        raw = micro_at_fill if micro_at_fill is not None else floor
        return self._snap_up(max(raw, floor))

    def _enter(self, ts: int) -> None:
        order = self.order_factory.limit(
            self.iid, OrderSide.BUY, self.instrument.make_qty(1),
            self.instrument.make_price(self.bid),
            time_in_force=TimeInForce.GTC, post_only=True)
        self.submit_order(order)
        self._entry_id = order.client_order_id
        self._entry_submit_ns = ts
        self._last_entry_ns = ts            # cooldown starts at SIGNAL, not at fill
        self.n_entries += 1
        self.entry_log.append({
            "ts": ts, "bid": self.bid, "ask": self.ask,
            "bid_sz": self.bid_sz, "ask_sz": self.ask_sz,
            "micro": self.micro_price_now(), "obi": self.imbalance(),
            "div_bps": self.divergence_bps(), "lam": self._lam,
        })

    def on_order_filled(self, event) -> None:
        if event.order_side == OrderSide.BUY:            # entry filled -> rest target
            self.n_fills += 1
            self._entry_id = None
            self._exit_deadline_ns = int(self.clock.timestamp_ns()) + self.cfg.exit_timeout_ns
            entry_px = float(event.last_px)
            mp_fill = self.micro_price_now()
            self._pos_entry_px = entry_px
            self._pos_mfe = self._pos_mae = 0.0
            self._pos_ctx = {
                "entry_micro": mp_fill,
                "entry_div_bps": self.divergence_bps(),
                "entry_div_ticks": self.divergence_ticks(),
                "entry_obi": self.imbalance(),
                "entry_spread_ticks": (self.ask - self.bid) / self.tick if self.bid else None,
                "entry_lam": self._lam,
                "target_px": None,
            }
            tgt = self.exit_target(entry_px, mp_fill)
            self._pos_ctx["target_px"] = tgt
            self._pos_ctx["target_ticks"] = (tgt - entry_px) / self.tick
            sell = self.order_factory.limit(
                self.iid, OrderSide.SELL, self.instrument.make_qty(1),
                self.instrument.make_price(tgt),
                time_in_force=TimeInForce.GTC, reduce_only=True)
            self.submit_order(sell)

    def on_position_closed(self, event) -> None:
        # CAPTURE EVERY ROUND TRIP HERE — do not enumerate them from the cache later.
        # Under OmsType.NETTING Nautilus reuses ONE PositionId per instrument, so
        # `cache.positions_closed()` returns a single record no matter how many
        # open->flat cycles occurred (measured: 28 PositionClosed events, cache
        # reported 1). Every PnL aggregate built from the cache is therefore the
        # last trade only. The event stream is the authoritative population.
        pos = self.cache.position(event.position_id)
        if pos is not None:
            self.closed_positions.append({
                "ts_opened": pos.ts_opened,
                "ts_closed": pos.ts_closed,
                "hold_ms": (pos.ts_closed - pos.ts_opened) / 1e6 if pos.ts_closed else None,
                "avg_px_open": float(pos.avg_px_open),
                "avg_px_close": float(pos.avg_px_close) if pos.avg_px_close else None,
                "realized_pnl_usd": float(pos.realized_pnl) if pos.realized_pnl is not None else 0.0,
                "commission_usd": sum(float(c) for c in pos.commissions()),
                "exit_reason": "timeout" if self._timeout_pending else "target",
                "mfe_ticks": self._pos_mfe / self.tick,
                "mae_ticks": self._pos_mae / self.tick,
                **self._pos_ctx,
            })
        self._pos_entry_px = None
        self._pos_mfe = self._pos_mae = 0.0
        self._pos_ctx = {}
        self._timeout_pending = False
        self._exit_deadline_ns = None
        self._entry_id = None

    def _manage_orders(self) -> None:
        now = int(self.clock.timestamp_ns())
        if self._entry_id is not None and now - self._entry_submit_ns > self.cfg.entry_ttl_ns:
            self.cancel_all_orders(self.iid)             # stale unfilled entry
            self._entry_id = None
            self.n_entry_cancels += 1
        # RISK 2.2 -- the entry demanded OBI >= p90, but OBI at fill has median
        # 0.000: the bid wall we joined is gone by the time we are filled. Kill a
        # live entry the moment the condition that justified it has decayed.
        elif (self._entry_id is not None and self.cfg.cancel_on_obi_decay
              and self.bid is not None
              and self.imbalance() < self.cfg.obi_cancel_floor):
            self.cancel_all_orders(self.iid)
            self._entry_id = None
            self.n_cancel_obi_decay += 1
        if self._exit_deadline_ns is not None and now >= self._exit_deadline_ns:
            self.n_timeout_exits += 1
            self._timeout_pending = True
            self.cancel_all_orders(self.iid)             # timeout: dump the position
            for pos in self.cache.positions_open(instrument_id=self.iid):
                self.close_position(pos)
            self._exit_deadline_ns = None


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
class _StubGate:
    """Stand-in for the spread gate: no engine, just bid/ask/cfg."""

    def __init__(self, tick: float, cfg: V2Config, bid: float, ask: float):
        self.tick, self.cfg, self.bid, self.ask = tick, cfg, bid, ask


class _StubExit:
    """Minimal stand-in so exit_target can be tested without a Nautilus engine."""

    def __init__(self, tick: float, cfg: V2Config):
        self.tick = tick
        self.cfg = cfg

    _snap_up = V2StrategyBase._snap_up


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

    # micro-price: heavier ask -> price pulled toward the bid
    mp = micro_price(100.0, 10, 101.0, 30)          # (100*30 + 101*10)/40 = 100.25
    check("micro_price weighting", abs(mp - 100.25) < 1e-9)
    check("micro_price mid when equal", abs(micro_price(100, 5, 101, 5) - 100.5) < 1e-9)

    # imbalance
    check("imbalance bid-heavy", abs(book_imbalance(30, 10) - 0.5) < 1e-9)
    check("imbalance balanced", abs(book_imbalance(10, 10)) < 1e-9)

    # rolling percentile
    rp = RollingPercentile(1000)
    for v in range(1, 101):
        rp.add(v)
    check("rolling p99 ~ 99", abs(rp.quantile(0.99) - 99.0) <= 1.0)

    # --- RTH bounds (DST-correct, resolved via tzdb not a fixed offset) ---
    #   2026-06-19 is EDT (UTC-4) -> 09:30 ET = 13:30 UTC
    jun = int(datetime(2026, 6, 19, 15, 0, tzinfo=timezone.utc).timestamp()) * NS
    lo, hi = rth_bounds_ns(jun, time(9, 30), time(16, 0))
    lo_utc = datetime.fromtimestamp(lo // NS, tz=timezone.utc)
    check("RTH open Jun (EDT) = 13:30 UTC", (lo_utc.hour, lo_utc.minute) == (13, 30))
    check("RTH window is 6.5 h", (hi - lo) == int(6.5 * 3600) * NS)
    #   2026-01-15 is EST (UTC-5) -> 09:30 ET = 14:30 UTC. A hardcoded offset fails here.
    jan = int(datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc).timestamp()) * NS
    lo_j, _ = rth_bounds_ns(jan, time(9, 30), time(16, 0))
    lo_j_utc = datetime.fromtimestamp(lo_j // NS, tz=timezone.utc)
    check("RTH open Jan (EST) = 14:30 UTC (DST handled)", (lo_j_utc.hour, lo_j_utc.minute) == (14, 30))

    # --- divergence in basis points (the spec's unit) ---
    #   micro 7561.00, last trade 2.2683 pts lower -> exactly 3.0 bps
    check("3 bps of 7561 is ~2.268 pts", abs(7561.0 * 3.0 / 10_000.0 - 2.2683) < 1e-3)
    check("3 bps on ES is ~9.07 ticks (NOT 12)", abs((7561.0 * 3.0 / 10_000.0) / 0.25 - 9.073) < 1e-2)

    # --- config guards ---
    check("bad exit_mode rejected", _raises(lambda: V2Config(exit_mode="nope"), ValueError))
    check("default exit_mode is convergence", V2Config().exit_mode == "convergence")
    check("default cooldown is 180 s (prereg independence)", V2Config().cooldown_ns == 180 * NS)
    check("default peak window is 100 ms (spec)", V2Config().peak_window_ns == 100_000_000)
    check("default divergence is 3 bps (spec)", V2Config().divergence_bps == 3.0)
    check("config serialises", isinstance(V2Config().to_dict()["rth_start_et"], str))

    # --- risk controls 2.2 / 2.4 default OFF (must reproduce v1.0.2 exactly) ---
    c0 = V2Config()
    check("2.2 cancel_on_obi_decay defaults OFF", c0.cancel_on_obi_decay is False)
    check("2.4 max_entry_spread_ticks defaults OFF", c0.max_entry_spread_ticks is None)

    # 2.4 absolute spread gate
    g = _StubGate(tick=0.25, cfg=V2Config(max_entry_spread_ticks=1.0), bid=100.00, ask=100.25)
    check("2.4 allows a 1-tick spread", V2StrategyBase._entry_spread_ok(g) is True)
    g2 = _StubGate(tick=0.25, cfg=V2Config(max_entry_spread_ticks=1.0), bid=100.00, ask=100.50)
    check("2.4 blocks a 2-tick spread", V2StrategyBase._entry_spread_ok(g2) is False)
    g3 = _StubGate(tick=0.25, cfg=V2Config(), bid=100.00, ask=101.00)
    check("2.4 disabled passes any spread", V2StrategyBase._entry_spread_ok(g3) is True)
    #   the measured fill regime (2.06 ticks) must be rejected by a 1-tick cap
    g4 = _StubGate(tick=0.25, cfg=V2Config(max_entry_spread_ticks=1.0), bid=100.00, ask=100.515)
    check("2.4 rejects the measured 2.06-tick fill regime",
          V2StrategyBase._entry_spread_ok(g4) is False)

    # --- exit target: convergence vs the legacy fixed cap ---
    et = _StubExit(tick=0.25, cfg=V2Config())
    #   entry 7550.00, micro-price at fill 7552.30 -> target snaps UP to 7552.50
    check("convergence target = micro-price snapped up",
          abs(V2StrategyBase.exit_target(et, 7550.00, 7552.30) - 7552.50) < 1e-9)
    #   micro-price BELOW our fill (adverse) -> floor at entry + 1 tick, never below
    check("convergence target floored at entry+1tick",
          abs(V2StrategyBase.exit_target(et, 7550.00, 7549.00) - 7550.25) < 1e-9)
    #   the dislocation is the edge: a 9-tick divergence must NOT be capped at 1 tick
    tgt = V2StrategyBase.exit_target(et, 7550.00, 7552.25)
    check("9-tick dislocation yields a 9-tick target", abs(tgt - 7550.00 - 2.25) < 1e-9)
    et_fixed = _StubExit(tick=0.25, cfg=V2Config(exit_mode="fixed_ticks"))
    check("fixed_ticks mode reproduces the +1-tick v0.1 behaviour",
          abs(V2StrategyBase.exit_target(et_fixed, 7550.00, 7552.30) - 7550.25) < 1e-9)

    # trade converter on the real tape (small slice)
    tape = Path(__file__).resolve().parent.parent.parent / "data" / "L2" / "ES_2026-06-08_trades.parquet"
    if tape.exists():
        ticks = load_trade_ticks(str(tape), InstrumentId.from_str("ESM6.GLBX"), limit=500)
        check("converter produced 500 ticks", len(ticks) == 500)
        check("tick has correct instrument", str(ticks[0].instrument_id) == "ESM6.GLBX")
        check("aggressor mapped to enum",
              ticks[0].aggressor_side in (AggressorSide.BUYER, AggressorSide.SELLER))
    else:
        print("  (skip converter check — trade tape not found)")

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else 0)
