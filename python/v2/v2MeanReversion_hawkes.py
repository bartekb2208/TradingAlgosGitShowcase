"""v2MeanReversion_hawkes.py — the research-notes order-book-exhaustion entry.

Spec: my own research notes (not published), "Mean Reversion
system 2: Order book Exhaustion" -> Entry Rules: LONG. All three must hold:

  1. Divergence  : last traded price >= 3 BASIS POINTS below the micro-price
                   (a liquidity vacuum has opened up).
  2. Imbalance   : OBI = (Q_bid - Q_ask)/(Q_bid + Q_ask) > 0.30 — a "brick wall"
                   of resting bids ready to absorb the remaining selling.
  3. Exhaustion  : the aggressive-sell intensity lambda(t) has dropped >= 50%
                   from its peak within a 100 ms window — the institutional
                   seller is done firing. This is the falling-knife filter, and
                   it is the condition the whole setup turns on.

This is a DIFFERENT hypothesis from the conjunction variant (which comes from the
discretionary V-Reversal footprint doc). Same execution layer, different signal;
they are A/B'd, not merged, and each needs its own pre-registration.

Execution / exits / risk are inherited from V2StrategyBase.
"""
from __future__ import annotations

from v2_core import V2StrategyBase


class HawkesStrategy(V2StrategyBase):
    def entry_signal(self, sell_vol: float) -> bool:
        return (
            self.divergence_bps() >= self.cfg.divergence_bps
            and self.imbalance() > self.cfg.obi_threshold
            and self.intensity_exhausted()
        )
