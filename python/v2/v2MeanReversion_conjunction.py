"""v2MeanReversion_conjunction.py — VREVERSAL conjunction entry (see the prereg).

Enter LONG when an aggressive-sell blowoff (rolling sell volume >= p99) co-occurs
with a bid-heavy book (imbalance >= p90) — the conjunction VREVERSAL found survives
(standalone components collapse to ~0). Execution/exits/risk live in V2StrategyBase.
"""
from __future__ import annotations

from v2_core import V2StrategyBase


class ConjunctionStrategy(V2StrategyBase):
    def entry_signal(self, sell_vol: float) -> bool:
        return sell_vol >= self.sell_p99() and self.imbalance() >= self.imb_p90()
