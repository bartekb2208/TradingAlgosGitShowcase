# ORB First-Move — and the −78% that was an execution bug

**Verdict: abandoned. After the execution bugs were fixed, the strategy itself printed PF 0.66.**
**What's published:** the full cBot, [`cbots/OrbFirstMoveBot.cs`](../../cbots/OrbFirstMoveBot.cs)
(1,200 lines).

> Research only. Not financial advice. Don't run this on a live account.

## The idea

A rule-for-rule mechanisation of a discretionary opening-range framework: the opening range is the
*first meaningful move* after the open rather than a fixed window, entries need a momentum
candle that closes well outside the range, stops sit tight under the breakout candle, the trail
follows market structure, and management shifts from one-minute to five-minute bars once a trade
is 1.5R in profit. Every place the source framework was silent became a named, labelled
parameter, so the translation can be audited line by line. It's a separate hypothesis from the
[ORB 1.1 survivor](../survivors/orb-1.1.md) and shares no code or validation with it.

## The run that lost 78%

The first full run on XAUUSD (gold) took the account down 78%. Reading the cTrader event export
instead of the summary showed the strategy hadn't done most of that. The execution layer had:

1. **Stops were silently dropped.** cTrader rounds `stopLossPips` to an integer on the server.
   A 0.82-pip stop becomes 1 pip (which distorts R), and **anything under 0.5 pips becomes 0, so
   the position opens with no stop at all**. The event log confirmed it: `Create Position` with
   `sl=null`. The trail logic then computed its first "tightening" from that missing baseline
   and installed a stop **39R** from entry.
2. **Sizing exploded as stops shrank.** Fixed-fractional sizing divides risk by stop distance. On
   a quiet morning with a $0.06 stop it sized **10,842 oz of gold, roughly 200× leverage**.
3. **Bars were processed one bar late.** The bot read "the last closed bar" as `Count − 2` inside
   the `BarClosed` event, but when the event fires relative to the new bar's append depends on the
   build. The session printed a minute late, and entries filled 60+ seconds after the signal
   close.

### The fixes (all in the published source)

| Problem | Fix | Where |
|---|---|---|
| Server rounds the stop to integer pips | Send the order with a **ceilinged** pip stop, then set the exact stop **by price** (`ModifyStopLossPrice`, no rounding), then **verify** `Position.StopLoss` is set, and flatten on the spot if it isn't | [`OrbFirstMoveBot.cs:896`](../../cbots/OrbFirstMoveBot.cs#L896) |
| A trail update finds no stop | Treated as an emergency: restore the initial stop or flatten. Never compute a trail from a null baseline | [`:971`](../../cbots/OrbFirstMoveBot.cs#L971) |
| Leverage blows up on tiny stops | A notional cap: no trade may exceed N× balance whatever the sizing formula says | [`:875`](../../cbots/OrbFirstMoveBot.cs#L875) |
| Stop floor was ATR-only (self-referential on quiet days) | Floor = max(k×ATR, m×live spread, minimum pips) | header, *Execution safety* |
| News-tick air pockets | Abandon the entry if the live quote is more than 0.5×ATR from the signal close | [`:832`](../../cbots/OrbFirstMoveBot.cs#L832) |
| Stale bar indexing | Bars resolved **by timestamp** (closed iff `OpenTime + period <= Server.Time`), each processed exactly once | [`:478`](../../cbots/OrbFirstMoveBot.cs#L478) |

These guards were then ported into every bot written afterwards, including the MQL5 work.

## The clean run, and why it was abandoned

With execution fixed, the strategy ran cleanly and still lost: **PF 0.66, 503 of 505 exits by the
trail, and only 4.8% of trades ever reached 1.5R.** The trail was strangling every trade before it
could become a runner.

A "fresh derivation" pass re-read the framework sentence by sentence and removed everything that
had been imported from earlier bots instead of from the source: a noon entry cutoff (the
framework's own example trades include a +7R re-entry more than two hours after the open), a hard
attempt cap, and a 2% daily halt that pre-empted the framework's real stopping rule. The trail was
rebuilt as one structure rule instead of three tightest-wins constraints.

It didn't rescue it. The lesson that survived is about the execution layer, and it is the
reason this bot is published.
