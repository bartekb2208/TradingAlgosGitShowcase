# Mean-Reversion V1 (Connors RSI(2)) — complete results & verdict

**Compiled 2026-08-02.** Consolidates every V1 experiment run against the daily Connors RSI(2)
mean-reversion strategy defined in the *Adaptive Microstructure Trading Framework* (Version 1) and
implemented in `python/MeanReversion1.py`.

> **NOT FINANCIAL ADVICE.** Research / backtesting only. Past market data does not predict future
> results. Every result below is net of a modelled cost but omits financing, slippage beyond
> spread, and prop-firm sizing constraints.

---

## Verdict

**V1 is NOT SUPPORTED at a representative sample size.** The strategy has a *genuine but small*
per-trade edge on upward-biased equity indices, but on the daily timeframe it is too **trade-starved**
to prove profitability: the pre-registered bar of **≥ 100 trades** was missed in **both**
out-of-sample trials (94 and 93), and on the cleanest, un-tuned, de-correlated test the aggregate
edge is marginal (PF 1.17) with one of three indices net-losing.

Per the design doc's principle *"simple models must justify complex models,"* this outcome is not a
dead end — it is the **justification to proceed to V2** (L2 order-book exhaustion), which is expected
to generate 100+ trades within days and therefore can actually reach statistical significance.

---

## Method (common to all experiments)

- **Strategy (frozen):** long-only. Enter when Close > SMA200 **and** RSI(2) < 5; exit on Close >
  SMA5, ATR hard-stop (2.5×ATR), or 7-day time-stop. Sizing: 1%-risk off the ATR stop, max 5
  positions, 20% notional cap (Mandelbrot/Taleb risk overrides). Validated by `--selftest`.
- **Data hygiene (frozen):** MT5 daily exports carry epoch-seconds timestamps and Saturday/Sunday
  globex partial bars; all MT5 data cleaned to **Monday–Friday, one bar per session**
  (`mt5_adapter.py`). Yahoo cash-index data is already clean and read natively.
- **Cost model:** `cost_bps = 1.0` per side (~2 bps round-trip), conservative for e-mini futures on
  the prop firm. (The exploratory smoke test alone was run at zero cost.)
- **Discipline:** pre-registration before each out-of-sample read; test sets touched exactly once;
  no re-tuning on test results. Trials are numbered; a changed rule/grid/partition = a new trial.

---

## Results summary

| # | Experiment | Design | Cost | Trades | Pooled PF | CAGR | MaxDD | Verdict |
|---|---|---|---|---|---|---|---|---|
| 0 | Smoke (EP) | single instrument, full sample, default params | 0 | 53 | 2.84 | +0.60% | −0.81% | *exploratory — not a finding* |
| 1 | **Trial #1** cross-instrument | tune {EP, YM} → test {NQ, RTY, DAX} | 1.0/side | **94** | 1.56 | +0.59% | −2.30% | **FAIL** (94 < 100) |
| — | Negative control | 30Y Treasury (USA), fixed params | 1.0/side | 20 | 0.93 | −0.01% | −0.99% | *exploratory — edge absent* |
| 2 | **Trial #2** zero-tuning breadth | fixed params → test {FTSE, CAC, N225} | 1.0/side | **93** | 1.17 | +0.16% | −3.44% | **FAIL** (93 < 100) |

---

## Experiment detail

### 0 · Smoke test — does the signal fire? (exploratory)
EP (S&P 500) daily, 2016–2026, Mon–Fri cleaned (2,753 bars), default params, **zero cost**.
Result: 53 trades, win 83%, PF 2.84, exposure 5.1%, peak positions 1/5, CAGR +0.60%. The per-trade
edge is real and survived data cleaning, but ~5% exposure on one instrument compounds to nearly
flat. Full-sample and un-split, so **not a finding** — it only established that the machinery works
and motivated a breadth-based approach.

### 1 · Trial #1 — cross-instrument generalisation
Tuned a single global parameter set on **{EP, YM}** over a 27-combo grid (objective: net profit
factor, min-trades ≥ 10); the grid selected the **plain design-doc defaults** (rsi=5, atr=2.5,
hold=7) — i.e. optimisation added nothing. Frozen set tested once on **{NQ, RTY, DAX}**:
- 94 trades; pooled PF **1.56**; per-instrument **DAX 1.46, NQ 1.03, RTY 2.65** (all net-profitable);
  CAGR +0.59%, MaxDD −2.30%, Sharpe 0.45.
- **FAIL — solely on trade count (94 < 100).** The edge generalised across instruments, but the
  sample fell 6 trades short. Caveat: {EP, YM, NQ, RTY, DAX} are all US large-cap indices and highly
  correlated, so this is a *moderate* generalisation test.
- Artifacts: `research/mr-v1-connors-rsi2/artifacts/trial1/`.

### Negative control — is the edge really about *upside bias*? (exploratory)
Identical fixed-param V1 on the **30-year US Treasury** (USADaily, 153.8 → 109.3 over the window — a
multi-year *bear*, i.e. the opposite of upside-biased). Result: **20 trades, PF 0.93 (net-losing),
exposure 2.5%.** The 200-day trend filter correctly kept the strategy out of the bond bear, and the
few trades it did take had **no edge**. This is the predicted behaviour if the equity-index edge is
genuinely tied to structural upside bias (rather than "buy any dip"). Suggestive, not proof (n = 20,
no pre-registration).

### 2 · Trial #2 — zero-tuning breadth on fresh, de-correlated indices
**No optimisation at all** — design-doc default params applied once to three *unseen*, geographically
de-correlated upward-biased indices (Yahoo cash): **FTSE 100 (UK), CAC 40 (FR), Nikkei 225 (JP)**,
pooled as one portfolio.
- 93 trades; pooled PF **1.17**; per-instrument **FTSE 1.12, CAC 0.98 (net-losing), N225 PF∞ but only
  6 trades**; CAGR +0.16%, MaxDD −3.44%, Sharpe 0.16.
- **FAIL — again on trade count (93 < 100).** On the hardest, cleanest test (no tuning, low
  correlation, weaker-drift indices) the edge is **marginal**: aggregate PF just 1.17, one of three
  indices actually net-losing, and Nikkei too sparse (6 trades) to interpret.
- Artifacts: `research/mr-v1-connors-rsi2/artifacts/trial2/`.

---

## Interpretation

1. **The edge is real but small.** Every equity-index test is net-positive per trade (PF 1.12–2.84);
   the negative control (bonds, PF 0.93) shows it is tied to the upside-bias mechanism as
   hypothesised.
2. **The daily timeframe is trade-starved.** At ~5 trades/instrument/year, two independent
   out-of-sample trials both stalled at 93–94 trades — the 100-trade representativeness bar is
   structurally hard to clear with a handful of instruments. This is the decisive constraint.
3. **Optimisation is not the lever.** Trial #1's grid returned the defaults; Trial #2 used no tuning
   at all and produced a *weaker* result once the correlation advantage was removed. More parameters
   would only add overfitting risk, not edge.
4. **Net conclusion:** V1 daily mean-reversion is a plausible, mechanism-consistent, but
   **non-representative and marginal** edge — not a business on its own at this timeframe.

## Explicitly not claimed / limitations
- Thin samples (≤ 94 trades per trial); wide error bars; Nikkei effectively uninterpretable at n = 6.
- Fills at close/stop; slippage only via `cost_bps`; overnight financing and prop-firm
  contract/drawdown limits not modelled (the %-notional engine is a proxy for whole-contract futures).
- Cross-instrument OOS among equity indices is weakened by their correlation (mitigated, not removed,
  by adding UK/FR/JP).
- No regime filter (the design doc's Ehlers Hilbert trend/cycle discriminator is unbuilt).

## Decision
**Close V1.** The pre-registered bar was not met, but the failure mode is *insufficient
representativeness on a trade-starved timeframe*, not a broken hypothesis — which is precisely the
design doc's trigger to move to the higher-frequency **V2 (order-book exhaustion, L2 / Databento →
NautilusTrader)**, where 100+ trades accrue in days. V2 is not to begin until V1 is closed (this doc).

## Provenance
- Pre-registrations: `research/mr-v1-connors-rsi2/PREREG_trial1_cross_instrument.md` (trial #1), `research/mr-v1-connors-rsi2/PREREG_trial2_breadth.md` (trial #2).
- Engine & harness: `python/{MeanReversion1.py, mt5_adapter.py, crossval.py, breadth_test.py}`.
- Result artifacts: `research/mr-v1-connors-rsi2/artifacts/trial1/`, `research/mr-v1-connors-rsi2/artifacts/trial2/`.
- Data: MT5 daily exports `data/L1/` (EP, YM, NQ, RTY, DAX, USA); Yahoo cash `data/yahoo/` (FTSE, CAC, N225).
