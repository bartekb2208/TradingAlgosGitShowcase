# FVT V1 — JJ Simon's "Fair Value Theory", mechanised

**Verdict: falsified on the hard falsifier. Net PF 0.66 on 2,885 ES trades against a bar of
1.10; gross expectancy was negative before costs.**

> Research only. Not financial advice.

The source is a free public strategy sheet:
[*JJ Simon's Fair Value Theory NQ Strategy*](https://fxreplay.com/strategies/jj-simons-fair-value-theory-nq-strategy)
on FX Replay. The claim: in the 09:30–11:00 and 14:00–15:00 New York windows, price continues away
from "fair value" (the 09:30 and 14:00 prices) for the first 10–15 minutes and then reverts to it;
enter on a displacement candle plus a break of structure, target 1.5R.

| File | What it is |
|---|---|
| [`DESIGN.md`](DESIGN.md) | Turning discretionary prose into rules, including two errors found in the source |
| [`artifacts/report.txt`](artifacts/report.txt) | Full falsifier report |
| [`artifacts/stats.json`](artifacts/stats.json) | Machine-readable stats |

**Code:** [`python/fvt/`](../../python/fvt/): `fvt_core.py` (structure, displacement, signals),
`fvt_engine.py` (bar loop, bracket resolution, ambiguity accounting), `fvt_data.py` (bar access),
`fvt_stats.py` (splits, metrics, falsifier verdicts).

Three things in the design are worth reading:

- **The source's own worked examples are a regression test.** The sheet prints exact Fibonacci
  prices for two trades. The displacement-candle definition was decoded so that it reproduces both
  to the cent, and a test fails if it ever stops doing so.
- **Validity gates are kept separate from claim tests.** Failing a validity gate (sample size,
  fill fidelity) makes a result *uninterpretable*, which is different from the strategy being bad,
  and the report never conflates the two.
- **A passing check that isn't support.** F5 (phase discrimination) technically passed by 0.19 pp,
  but both phases lost money. The report prints an explicit note that this must not be read as
  evidence for the strategy.

ES was used as an adjacent-instrument proxy because the NQ history available was too short; the
report states that a result on ES is evidence about the entry model, not proof about NQ.
