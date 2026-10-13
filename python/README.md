# Python research code

> Research only. Not financial advice. No market data is included.

| Path | What it is | Study |
|---|---|---|
| `MeanReversion1.py` | Daily Connors RSI(2) backtest engine: indicators, sizing, portfolio loop, report | [MR V1](../research/mr-v1-connors-rsi2/README.md) |
| `crossval.py` | Tune on one instrument set, freeze, test once on another; test data structurally unreachable during tuning | MR V1 trial 1 |
| `breadth_test.py` | Zero-tuning breadth test on fresh instruments | MR V1 trial 2 |
| `mt5_adapter.py` | Cleans MT5 daily exports (epoch-second timestamps, weekend Globex partial bars that distort RSI/ATR) | shared |
| `fvt/` | 1-minute bar engine: structure detection, bracket resolution with ambiguity accounting, falsifier report | [FVT V1](../research/fvt-v1/README.md) |
| `vr/` | Footprint-setup detector and strategy on NautilusTrader, plus random-entry and time-shift null models | [V-Reversal](../research/v-reversal/README.md) |
| `v2/` | Market-by-order pipeline and queue-aware NautilusTrader backtests | [V2](../research/v2-order-book/README.md) |

The scripts locate each other by path, so run them from this directory.

## Setup

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r python/requirements.txt
```

## Selftests

Fourteen modules carry a `--selftest`. The eight below need no market data and run in CI:

```bash
cd python
python MeanReversion1.py --selftest
python mt5_adapter.py --selftest
python fvt/fvt_core.py --selftest
python fvt/fvt_stats.py --selftest
python vr/vr_core.py --selftest
python vr/vr_detect.py --selftest
python vr/vr_strategy.py --selftest
python v2/v2_core.py --selftest
```

The other six (`crossval.py`, `breadth_test.py`, `fvt/fvt_engine.py`, `fvt/fvt_data.py`,
`vr/vr_data.py`, `v2/data_pipeline.py`) open the real data files as part of their checks, so they
fail with `FileNotFoundError` here. That's expected: the data is licensed or broker-owned and isn't
redistributed.

## A run with no data

The MR V1 engine can generate synthetic, upward-drifting index-like series and backtest them end
to end:

```bash
python MeanReversion1.py --generate-sample-data --no-plot
```

It writes `data/` and `figures/` into the current directory (both gitignored). **The numbers it
prints are a smoke test of the machinery, not a result**: the synthetic series are built with an
upward drift, so a long-only dip-buyer is expected to make money on them.

The scripts read market data from `data/` at the repository root (gitignored, not included) and
write their outputs into the matching `research/<study>/artifacts/` folder.
