# MR Index-Swing V1 -- trial #2 zero-tuning breadth test
Fixed params (NO tuning): {'rsi_entry': 5.0, 'atr_stop_mult': 2.5, 'max_hold': 7}   cost_bps=1.0/side
Fresh test set (Yahoo cash indices): ['CAC', 'FTSE', 'N225']

Aggregate trades : 93   (criterion: >= 100)
Pooled PF        : 1.17   (criterion: > 1.0)
Per-instrument   : FTSE(n=39, PF=1.12), CAC(n=48, PF=0.98), N225(n=6, PF=inf)
CAGR +0.16% | MaxDD -3.44% | Sharpe 0.16 (context)

VERDICT: FAIL -- not supported
