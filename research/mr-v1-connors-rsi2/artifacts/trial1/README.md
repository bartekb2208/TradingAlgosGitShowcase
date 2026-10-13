# MR Index-Swing -- cross-instrument test result
Frozen params (tuned on EP+YM): {'rsi_entry': 5.0, 'atr_stop_mult': 2.5, 'max_hold': 7}
Test set: ['DAX', 'NQ', 'RTY']  cost_bps=1.0/side

Aggregate trades : 94   (criterion: >= 100)
Pooled PF        : 1.56   (criterion: > 1.0)
Per-instrument   : DAX(n=22, PF=1.46), NQ(n=36, PF=1.03), RTY(n=36, PF=2.65)
CAGR +0.59% | MaxDD -2.30% | Sharpe 0.45 (context only)

VERDICT: FAIL -- not supported
