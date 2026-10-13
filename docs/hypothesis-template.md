# &lt;Strategy name&gt; — pre-registration (trial #&lt;n&gt;)

**Written &lt;YYYY-MM-DD&gt;, BEFORE any out-of-sample data has been read.** Nothing below may change
after results exist. A changed rule, grid or partition is a new pre-registration with the trial
count incremented.

> Research only. Not financial advice.

<!--
How to use this template: fill every section before running anything that touches the test or
sealed data. If a section can't be filled, that is the finding: the idea isn't ready to test.
Delete these comments when done.
-->

## Hypothesis

One sentence. What effect exists, on what instrument and timeframe, and in which direction.

**Why it might exist.** The mechanism: who is on the other side of the trade, and why they keep
paying. A literature anchor if there is one (paper, author, year). If there's no mechanism, say
so; it lowers the prior and should raise the bar.

**Source.** Where the rules come from (paper, book, strategy sheet, own observation). If the source
is discretionary prose, link the section of this document that turns it into rules.

## Prior evidence (already spent)

Anything already looked at: exploratory runs, smoke tests, eyeballed charts. Report it so the test
below can't be mistaken for a first look, and label it **exploratory, not a finding**.

| Finding | Evidence | Data used (now burned) |
|---|---|---|
| | | |

## Frozen definition

Every rule needed to reproduce a trade, with no "roughly" left in it.

- **Universe / instrument / timeframe:**
- **Entry:**
- **Exit(s):**
- **Stop / sizing:**
- **Filters:**
- **Session / calendar rules (time zone, DST handling):**

**Judgment calls.** Each place the source is silent or ambiguous, the choice made, and why.
Each one is a degree of freedom; count them.

| # | The source says | Implemented as | Why |
|---|---|---|---|
| 1 | | | |

## Datasets and roles

| Role | Instrument(s) | Window | Sessions / bars | Touched |
|---|---|---|---|---|
| Tune (in-sample) | | | | freely |
| Test (out-of-sample) | | | | **once** |
| Validation / sealed | | | | **once, last** |

How leakage is prevented structurally (separate files or directories, test set never loaded
during tuning, walk-forward embargo):

## Costs and execution model

Commission, spread, slippage, financing/swaps, fill model (bar close, next open, queue position),
how intrabar ambiguity is resolved (default: to the stop), and what is **not** modelled.

## Selection procedure (if anything is tuned)

The grid, the fitness metric, and how one set is chosen from it (plateau centre by per-dimension
marginals, never the single best pass). Freeze the chosen set to a file **before** the test set
is read.

## Gates (pre-committed)

All must pass for the idea to be called supported.

| # | Gate | Threshold |
|---|---|---|
| 1 | Profitable after costs | net > 0 |
| 2 | Profit factor | ≥ |
| 3 | Max drawdown | ≤ |
| 4 | Sample adequacy | ≥ trades |
| 5 | Consistency (years / instruments positive) | ≥ of |

## Falsification

**Hard falsifier:** the single condition that ends the line, **with no parameter sweep to rescue
it**.

**Validity gates:** conditions that, if failed, make the result *uninterpretable* (too few trades,
untrustworthy fills, misaligned data). Failing one is not the same as the strategy being bad, and
the report must not conflate the two.

## Controls (null models)

What the result must beat, not just zero:

- [ ] Random entry under the same filters, identical exits (per-trade return, not net)
- [ ] Time-shifted entry (does the precise trigger moment matter?)
- [ ] Untouched instrument(s) the config has never seen
- [ ] Exposure-matched buy-and-hold (if mostly long)

## Explicitly not claimed

What a pass would *not* show: other instruments, other regimes, scaled size, live fills.

## Outputs

Files the run must produce (report, machine-readable stats, trade log, equity curve), each stamped
with the engine version and the full config.

---

## Results (filled in once, after the run)

**Run date:** · **Engine version:** · **Verdict:**

| # | Gate / falsifier | Result | Pass? |
|---|---|---|---|
| | | | |

What changes because of this result, and what must not be done next (for example "do not re-tune
on this window; it is now spent").
