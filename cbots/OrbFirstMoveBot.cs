using System;
using System.Collections.Generic;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    // =====================================================================================
    // ORB First-Move  —  mechanisation of Bartek's discretionary ORB framework
    // Designed for US100 / US500 CFD (indices, gold), M1 chart REQUIRED.   —  BACKTESTABLE
    // -------------------------------------------------------------------------------------
    // This is NOT the ORB v1 baseline (a separate, simpler bot, not published). It codifies the
    // discretionary framework rule-for-rule; every judgment call is a named parameter so
    // the mechanical translation can be audited and tuned. Framework -> mechanics:
    //
    // 1) BIAS (3 checks, evaluated at entry, in the BREAKOUT direction):
    //      a. price > daily EMA(15) > daily SMA(30)  (reverse for bearish). Daily values
    //         are taken from the last CLOSED daily bar (no intraday repaint).
    //      b. both EMA and SMA sloping up (value > value SlopeLookbackDays ago) / down.
    //      c. overnight move in the same direction: session open price minus the prior
    //         RTH close (15:59 NY close), magnitude >= OvernightMinMovePct of price.
    //         (Proxy for "overnight futures": the CFD itself trades the overnight session.)
    //      All 3 aligned with the trade direction = A+ setup -> RiskPercentAPlus.
    //      Anything less = B setup -> RiskPercentB. B setups still trade BOTH ways
    //      ("downsize and stay open either way"); bias only ever changes size.
    //
    // 2) OPENING RANGE = FIRST MEANINGFUL MOVE (not a fixed window):
    //      Range builds from the 09:30 NY open for at least MinRangeMinutes (5). From that
    //      point it keeps extending while the opening run is still going, and completes at
    //      the first bar close where the dominant leg (larger excursion from the open) has
    //        - consolidated: the last ConsolidationBars bars all failed to extend the
    //          dominant extreme ("consolidates for at least 2-3 candles"), or
    //        - reversed: close gave back >= ReversalRetraceFrac of the open->extreme leg,
    //      hard-capped at MaxRangeMinutes. OR high/low = session extremes over the window.
    //      "Minute 6 continuing the opening move" is inside the range by construction,
    //      so it can never be a breakout ("I extend to the end of that run").
    //
    // 3) ENTRY = BREAKOUT WITH MOMENTUM (m1 bar close):
    //      Fresh breakout requires a CROSSING close (prev close inside, this close beyond
    //      the OR edge) so a candle can only break out of the range, never re-trigger in
    //      open space. The candle must be big (range >= BreakoutAtrMult x max(m1 ATR,
    //      avg formation-bar range) - "clearly larger than recent opening candles /
    //      short-term ATR") with little hesitation wick (outer wick <= MaxHesitationWickFrac
    //      of the candle). Then:
    //        - > MinOutsideFrac (50%) of the candle beyond the edge -> enter at close.
    //        - strong but <= 50% outside -> arm a ONE-bar confirmation: next bar must
    //          close beyond the edge in the direction ("second confirming candle").
    //
    // 4) STOPS ("treat the start of every breakout like a scalp"):
    //      Default: under the breakout candle (structure low/high -/+ StopBufferAtr x ATR).
    //      Aggressive (at/just inside the range edge, EdgeStopInsetAtr inside): only when
    //      the breakout candle is very strong (>= StrongAtrMult x ATR AND >= StrongOutsideFrac
    //      outside), or when a confirmation candle was needed and it is itself strong.
    //      Confirmed entries otherwise stop under the LOWER of the two candles.
    //
    // 5) TRAILING (every management-TF bar close, stop only ever tightens):
    //      "Trail under retracements, 4-bar consolidations, or roughly a 40% retrace of
    //      small impulses / 60% of big ones" - three descriptions of ONE activity (the
    //      stop sits under the most recent meaningful structure), NOT three simultaneous
    //      tightest-wins constraints:
    //        - a completed retracement (pullback that ends with a new extreme) moves the
    //          stop under its low - but only if it was deep enough to BE a retracement
    //          (>= MinPullbackAtr x stage ATR; a 2-tick m1 dip is noise, not structure);
    //        - a ConsolidationTrailBars-bar (4) stall that fails to extend -> stop under it;
    //        - the give-back line (extreme - 40% of the current impulse, 60% once the
    //          impulse >= BigImpulseAtrMult x ATR) engages only after price has impulsed
    //          >= MinImpulseAtr x ATR beyond the signal candle - until then the birth stop
    //          stands "tight, under the breakout candle", exactly as stated.
    //      Cutting off some winners is accepted by design; re-entries recapture the move.
    //
    // 6) EASE-UP AT 1.5R: at >= EaseUpR open profit the trade is a RUNNER and management
    //      shifts to the 5-minute series (same trail rules, 5m bars/ATR); optionally to
    //      15m at Stage3R if UseStage3 ("maybe 15m if it keeps running"). If AllowSwingHold
    //      and the trade is A+ and a runner at the timed flat, it holds past the close as
    //      a swing ("if in line with daily bias, let it develop into a swing") - ON by
    //      default because it is IN the framework; disable for prop overnight rules.
    //
    // 7) RE-ENTRIES ("part of the plan", momentum only, never because price tagged a level):
    //        - a fresh crossing breakout of either OR edge is always a new setup (a clean
    //          retest back inside the range re-arms this automatically);
    //        - after a side has triggered once, a pullback re-entry in that direction:
    //          price came back to within RetestProximityAtr x ATR of the edge OR retraced
    //          >= ReentryRetraceFrac of the post-breakout extension ("doesn't have to be a
    //          full retest"), then a momentum candle closes beyond the edge in-direction.
    //          Stop tight under the signal candle.
    //
    // 8) ORB-DAY RULE: attempts are counted at entry. After MaxOrbAttempts (3) attempts
    //      with no runner (none reached EaseUpR), entries stop for the day ("stop calling
    //      it an ORB day" - this bot stands down; other strategies are other bots).
    //      There is NO attempt cap on runner days: the framework does not have one; the
    //      momentum + pullback requirements are its natural bound.
    //
    //      NOT-FRAMEWORK parameters (the text is silent; each is a labeled judgment call):
    //        - Play Window: OFF by default (example trades show +7R re-entries 2h+ after
    //          the open); optional research lever only;
    //        - timed flat at FlatHour:FlatMinute NY for non-swing positions (a scalp that
    //          never trailed out by the close has to end somewhere in a backtest);
    //        - MinPullbackAtr / MinImpulseAtr trail gates (what "meaningful" means on m1);
    //        - execution guards below. NY clock is self-contained US-DST.
    //
    //      House guards (execution plumbing, not strategy): fixed-fractional sizing at the
    //      structural stop, min stop-distance floor, spread <= MaxSpreadRiskFrac of the
    //      stop distance (cost-vs-risk, instrument-agnostic), daily-loss BACKSTOP at 5%
    //      (distant by design - the 3-attempt rule is the framework's own halt and must
    //      fire first; 3 A+ losses ~= -3% never trips it), one position at a time.
    //
    // EXECUTION SAFETY (post-mortem of the 2026-07-11 XAUUSD run, -78% balance):
    //   * cTrader rounds stopLossPips to INTEGER pips server-side: 0.82 -> 1 distorts R,
    //     and anything < 0.5 pips -> 0 -> the SL is SILENTLY DROPPED (position runs
    //     naked; confirmed in events.json: Create Position with sl=null). Fix: orders go
    //     out with a CEILINGed pip stop, then the exact structural stop is set by PRICE
    //     (ModifyStopLossPrice, no rounding), then Position.StopLoss is VERIFIED - if the
    //     position cannot be protected it is closed on the spot.
    //   * Stop-distance floor is no longer ATR-only (self-referential on quiet mornings):
    //     distance >= max(MinStopAtr x ATR, MinStopSpreadMult x live spread, MinStopPips).
    //   * Notional cap: fixed-fractional sizing explodes as stops shrink (observed:
    //     10,842 oz gold / ~200x leverage on a $0.06 stop). No trade may exceed
    //     MaxNotionalMult x balance in notional, whatever the sizing formula says.
    //   * Fast-tape guard: entry is abandoned when the live quote is more than
    //     MaxEntrySlippageAtr x ATR away from the signal close (news-tick air pockets).
    //   * A trailing update on a position with a missing SL is an EMERGENCY (restore the
    //     initial stop or flatten), never a trail computed from a null baseline.
    //   * Bar indexing: Bars.BarClosed append-timing is build-dependent, and a fixed
    //     Count-2 was observed processing bars ONE BAR STALE (session prints a minute
    //     late; entries fill 60s+ after the signal close). Bars are now resolved by
    //     TIMESTAMP - a bar is closed iff OpenTime + period <= Server.Time - and each
    //     closed bar is processed exactly once.
    //
    // Backtest with TICK data for stop/trail fidelity (m1 bar-close logic works in OHLC
    // mode but intrabar stop fills will be optimistic). Class OrbFirstMoveBot, helpers Ofm...
    // =====================================================================================
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class OrbFirstMoveBot : Robot
    {
        #region Parameters - Session (NY time)

        [Parameter("Open Hour (NY)", Group = "Session (NY)", DefaultValue = 9, MinValue = 0, MaxValue = 23)]
        public int OpenHourNy { get; set; }

        [Parameter("Open Minute (NY)", Group = "Session (NY)", DefaultValue = 30, MinValue = 0, MaxValue = 59)]
        public int OpenMinuteNy { get; set; }

        // OFF (0) by default. Not in the framework - its bound is the 3-attempt rule -
        // and Bartek's own example trades refute a window: his +7R ES re-entry fired
        // ~2h17m after the open. Kept only as an optional research lever.
        [Parameter("Play Window (min after open, 0 = off)", Group = "Session (NY)", DefaultValue = 0, MinValue = 0, MaxValue = 390)]
        public int PlayWindowMinutes { get; set; }

        [Parameter("Flat Hour (NY)", Group = "Session (NY)", DefaultValue = 15, MinValue = 0, MaxValue = 23)]
        public int FlatHourNy { get; set; }

        [Parameter("Flat Minute (NY)", Group = "Session (NY)", DefaultValue = 55, MinValue = 0, MaxValue = 59)]
        public int FlatMinuteNy { get; set; }

        #endregion

        #region Parameters - Opening Range

        [Parameter("Min Range Minutes", Group = "Opening Range", DefaultValue = 5, MinValue = 2, MaxValue = 30)]
        public int MinRangeMinutes { get; set; }

        [Parameter("Max Range Minutes", Group = "Opening Range", DefaultValue = 15, MinValue = 5, MaxValue = 60)]
        public int MaxRangeMinutes { get; set; }

        [Parameter("Run Ends: Stalled Bars", Group = "Opening Range", DefaultValue = 3, MinValue = 2, MaxValue = 5)]
        public int ConsolidationBars { get; set; }

        [Parameter("Run Ends: Reversal Retrace (frac)", Group = "Opening Range", DefaultValue = 0.5, MinValue = 0.2, MaxValue = 1.0)]
        public double ReversalRetraceFrac { get; set; }

        #endregion

        #region Parameters - Bias (A+ / B)

        [Parameter("Daily EMA Period", Group = "Bias", DefaultValue = 15, MinValue = 2, MaxValue = 200)]
        public int DailyEmaPeriod { get; set; }

        [Parameter("Daily SMA Period", Group = "Bias", DefaultValue = 30, MinValue = 2, MaxValue = 200)]
        public int DailySmaPeriod { get; set; }

        [Parameter("Slope Lookback (days)", Group = "Bias", DefaultValue = 1, MinValue = 1, MaxValue = 5)]
        public int SlopeLookbackDays { get; set; }

        [Parameter("Overnight Min Move (% of price)", Group = "Bias", DefaultValue = 0.05, MinValue = 0.0, MaxValue = 2.0)]
        public double OvernightMinMovePct { get; set; }

        #endregion

        #region Parameters - Entry

        [Parameter("ATR Period", Group = "Entry", DefaultValue = 14, MinValue = 5, MaxValue = 100)]
        public int AtrPeriod { get; set; }

        [Parameter("Breakout Candle (xATR)", Group = "Entry", DefaultValue = 1.2, MinValue = 0.5, MaxValue = 5.0)]
        public double BreakoutAtrMult { get; set; }

        [Parameter("Min Fraction Outside Range", Group = "Entry", DefaultValue = 0.5, MinValue = 0.1, MaxValue = 0.95)]
        public double MinOutsideFrac { get; set; }

        [Parameter("Max Hesitation Wick (frac)", Group = "Entry", DefaultValue = 0.25, MinValue = 0.05, MaxValue = 0.9)]
        public double MaxHesitationWickFrac { get; set; }

        [Parameter("Very Strong Candle (xATR)", Group = "Entry", DefaultValue = 1.5, MinValue = 1.0, MaxValue = 8.0)]
        public double StrongAtrMult { get; set; }

        [Parameter("Very Strong Fraction Outside", Group = "Entry", DefaultValue = 0.7, MinValue = 0.5, MaxValue = 1.0)]
        public double StrongOutsideFrac { get; set; }

        [Parameter("Allow Longs", Group = "Entry", DefaultValue = true)]
        public bool AllowLongs { get; set; }

        [Parameter("Allow Shorts", Group = "Entry", DefaultValue = true)]
        public bool AllowShorts { get; set; }

        #endregion

        #region Parameters - Stops

        [Parameter("Stop Buffer (xATR)", Group = "Stops", DefaultValue = 0.10, MinValue = 0.0, MaxValue = 1.0)]
        public double StopBufferAtr { get; set; }

        [Parameter("Edge Stop Inset (xATR)", Group = "Stops", DefaultValue = 0.10, MinValue = 0.0, MaxValue = 1.0)]
        public double EdgeStopInsetAtr { get; set; }

        [Parameter("Min Stop Distance (xATR)", Group = "Stops", DefaultValue = 0.15, MinValue = 0.05, MaxValue = 2.0)]
        public double MinStopAtr { get; set; }

        [Parameter("Min Stop Distance (pips)", Group = "Stops", DefaultValue = 2.0, MinValue = 1.0, MaxValue = 100.0)]
        public double MinStopPips { get; set; }

        [Parameter("Min Stop Distance (xSpread)", Group = "Stops", DefaultValue = 3.0, MinValue = 1.0, MaxValue = 20.0)]
        public double MinStopSpreadMult { get; set; }

        #endregion

        #region Parameters - Trailing / Stages

        [Parameter("Consolidation Trail Bars", Group = "Trailing", DefaultValue = 4, MinValue = 2, MaxValue = 10)]
        public int ConsolidationTrailBars { get; set; }

        // a pullback must be MEANINGFUL before its low becomes the trailed stop -
        // on m1 a 2-tick dip is not "a retracement" (judgment param, not framework)
        [Parameter("Min Retracement Depth (xATR)", Group = "Trailing", DefaultValue = 0.5, MinValue = 0.0, MaxValue = 3.0)]
        public double MinPullbackAtr { get; set; }

        // the 40/60% impulse give-back line only engages once the trade has actually
        // impulsed beyond the signal candle - the birth stop stays "under the
        // breakout candle" as stated (judgment param, not framework)
        [Parameter("Impulse Gate (xATR past signal)", Group = "Trailing", DefaultValue = 1.0, MinValue = 0.0, MaxValue = 5.0)]
        public double MinImpulseAtr { get; set; }

        [Parameter("Small Impulse Retrace (frac)", Group = "Trailing", DefaultValue = 0.4, MinValue = 0.1, MaxValue = 0.9)]
        public double SmallImpulseRetraceFrac { get; set; }

        [Parameter("Big Impulse Retrace (frac)", Group = "Trailing", DefaultValue = 0.6, MinValue = 0.1, MaxValue = 0.95)]
        public double BigImpulseRetraceFrac { get; set; }

        [Parameter("Big Impulse Threshold (xATR)", Group = "Trailing", DefaultValue = 3.0, MinValue = 1.0, MaxValue = 10.0)]
        public double BigImpulseAtrMult { get; set; }

        [Parameter("Trail Buffer (xATR)", Group = "Trailing", DefaultValue = 0.10, MinValue = 0.0, MaxValue = 1.0)]
        public double TrailBufferAtr { get; set; }

        [Parameter("Ease-Up / Runner Threshold (R)", Group = "Trailing", DefaultValue = 1.5, MinValue = 0.5, MaxValue = 5.0)]
        public double EaseUpR { get; set; }

        [Parameter("Use 15m Stage", Group = "Trailing", DefaultValue = true)]
        public bool UseStage3 { get; set; }

        [Parameter("15m Stage Threshold (R)", Group = "Trailing", DefaultValue = 4.0, MinValue = 1.0, MaxValue = 20.0)]
        public double Stage3R { get; set; }

        // framework: "If it's in line with daily bias, I may let it develop into a swing"
        // - so ON by default; turn off only for prop-firm no-overnight rules
        [Parameter("Allow Swing Hold (A+ runners)", Group = "Trailing", DefaultValue = true)]
        public bool AllowSwingHold { get; set; }

        #endregion

        #region Parameters - Re-entry / Attempts

        [Parameter("Retest Proximity (xATR)", Group = "Re-entry", DefaultValue = 0.5, MinValue = 0.0, MaxValue = 3.0)]
        public double RetestProximityAtr { get; set; }

        [Parameter("Re-entry Retrace (frac)", Group = "Re-entry", DefaultValue = 0.4, MinValue = 0.1, MaxValue = 1.0)]
        public double ReentryRetraceFrac { get; set; }

        // the framework's ONLY halt: N attempts with no runner ends the ORB day.
        // Runner days are deliberately uncapped - the framework has no such cap.
        [Parameter("Max ORB Attempts (no runner)", Group = "Re-entry", DefaultValue = 3, MinValue = 1, MaxValue = 10)]
        public int MaxOrbAttempts { get; set; }

        #endregion

        #region Parameters - Risk

        [Parameter("Risk A+ Setup (%)", Group = "Risk", DefaultValue = 1.0, MinValue = 0.05, MaxValue = 5.0)]
        public double RiskPercentAPlus { get; set; }

        [Parameter("Risk B Setup (%)", Group = "Risk", DefaultValue = 0.33, MinValue = 0.05, MaxValue = 5.0)]
        public double RiskPercentB { get; set; }

        // BACKSTOP only. Must sit beyond 3 x A+ risk so it can never fire before the
        // framework's own halt (the 3-attempt rule). At 2% it was preempting it.
        [Parameter("Max Daily Loss (% of balance)", Group = "Risk", DefaultValue = 5.0, MinValue = 0.5, MaxValue = 25.0)]
        public double MaxDailyLossPercent { get; set; }

        // cost-vs-risk guard: spread may not exceed this fraction of the stop distance
        // (instrument-agnostic - a fixed pip cap encoded index conventions, not gold's)
        [Parameter("Max Spread (frac of stop)", Group = "Risk", DefaultValue = 0.25, MinValue = 0.0, MaxValue = 1.0)]
        public double MaxSpreadRiskFrac { get; set; }

        [Parameter("Max Spread (pips, 0 = off)", Group = "Risk", DefaultValue = 0.0, MinValue = 0.0, MaxValue = 50.0)]
        public double MaxSpreadPips { get; set; }

        [Parameter("Max Notional (x balance)", Group = "Risk", DefaultValue = 10.0, MinValue = 1.0, MaxValue = 100.0)]
        public double MaxNotionalMult { get; set; }

        [Parameter("Max Entry Slippage (xATR)", Group = "Risk", DefaultValue = 0.5, MinValue = 0.1, MaxValue = 3.0)]
        public double MaxEntrySlippageAtr { get; set; }

        #endregion

        #region Parameters - Meta

        [Parameter("Trade Label", Group = "Meta", DefaultValue = "ORBFM")]
        public string TradeLabel { get; set; }

        [Parameter("Show Diagnostics", Group = "Meta", DefaultValue = true)]
        public bool ShowDiagnostics { get; set; }

        #endregion

        // --- Helper types ---
        private enum OfmStage { M1 = 0, M5 = 1, M15 = 2 }

        private sealed class OfmFormBar
        {
            public double High, Low, Range;
            public bool ExtUp, ExtDn;
        }

        private sealed class OfmPending          // armed one-bar breakout confirmation
        {
            public bool IsLong;
            public double CandleLow, CandleHigh; // breakout candle structure for the stop
        }

        private sealed class OfmTrade
        {
            public bool IsLong;
            public int PositionId;
            public double Entry;
            public double InitialStop;
            public double RiskPrice;             // |entry - initial stop|
            public double RiskAmount;            // account-ccy risk at entry
            public bool IsAPlus;
            public DateTime EntryTimeUtc;
            public OfmStage Stage;
            public double Extreme;               // best price since entry (m1 resolution)
            public double StageExtreme;          // best price on the management TF
            public int PbCount;                  // stage bars since the extreme last extended
            public double PbExtreme;             // worst pullback price in the current stall
            public double LegStart;              // current impulse anchor (signal structure, then each completed retracement)
            public double ImpulseGateRef;        // signal-candle extreme; give-back line arms past it
            public bool ImpulseOn;               // give-back line armed (latched)
            public bool Runner;                  // reached EaseUpR
            public bool SwingHold;
        }

        // --- Indicators / series ---
        private AverageTrueRange _atrM1, _atrM5, _atrM15;
        private Bars _barsM5, _barsM15, _barsD1;
        private ExponentialMovingAverage _emaD;
        private SimpleMovingAverage _smaD;

        // --- Day state ---
        private DateTime _dayNy = DateTime.MinValue;
        private double _lastRthClose = double.NaN;   // rolling last RTH close (survives resets)
        private double _overnightRef = double.NaN;   // prior RTH close frozen at day start
        private double _sessionOpen = double.NaN;
        private DateTime _sessionOpenUtc;
        private double _overnightMove = double.NaN;

        // --- Range state ---
        private readonly List<OfmFormBar> _formBars = new List<OfmFormBar>();
        private double _formHigh = double.MinValue, _formLow = double.MaxValue;
        private bool _rangeFormed;
        private double _orHigh = double.NaN, _orLow = double.NaN;
        private double _avgFormRange;

        // --- Entry / attempt state ---
        private OfmPending _pending;
        private OfmTrade _trade;
        private int _attemptsToday;
        private bool _runnerToday;
        private bool _longTriggered, _shortTriggered;
        private double _postHiLong = double.MinValue, _reLoLong = double.MaxValue;
        private double _postLoShort = double.MaxValue, _reHiShort = double.MinValue;
        private double _dailyPnl;
        private bool _haltPrintedToday;

        // last processed bar open time per series (timestamp-based bar resolution)
        private DateTime _lastM1Done = DateTime.MinValue;
        private DateTime _lastM5Done = DateTime.MinValue;
        private DateTime _lastM15Done = DateTime.MinValue;

        // =====================================================================
        // Lifecycle
        // =====================================================================

        protected override void OnStart()
        {
            if (Bars.TimeFrame != TimeFrame.Minute)
            {
                Print("ERROR: OrbFirstMoveBot requires an m1 chart (the framework reads the 1-minute auction). Chart is {0}. Stopping.", Bars.TimeFrame);
                Stop();
                return;
            }
            if (MinRangeMinutes >= MaxRangeMinutes)
            {
                Print("ERROR: Min Range Minutes ({0}) must be < Max Range Minutes ({1}). Stopping.", MinRangeMinutes, MaxRangeMinutes);
                Stop();
                return;
            }

            _atrM1 = Indicators.AverageTrueRange(Bars, AtrPeriod, MovingAverageType.Exponential);
            _barsM5 = MarketData.GetBars(TimeFrame.Minute5);
            _barsM15 = MarketData.GetBars(TimeFrame.Minute15);
            _atrM5 = Indicators.AverageTrueRange(_barsM5, AtrPeriod, MovingAverageType.Exponential);
            _atrM15 = Indicators.AverageTrueRange(_barsM15, AtrPeriod, MovingAverageType.Exponential);
            _barsD1 = MarketData.GetBars(TimeFrame.Daily);
            _emaD = Indicators.ExponentialMovingAverage(_barsD1.ClosePrices, DailyEmaPeriod);
            _smaD = Indicators.SimpleMovingAverage(_barsD1.ClosePrices, DailySmaPeriod);

            Bars.BarClosed += OnM1Closed;
            _barsM5.BarClosed += OnM5Closed;
            _barsM15.BarClosed += OnM15Closed;
            Positions.Closed += OnPositionClosed;

            Print("OrbFirstMove started on {0}. Range {1}-{2}min (stall {3} / retrace {4:F2}) | breakout {5}xATR out>{6:P0} wick<{7:P0} | A+ {8}% / B {9}% | {10} attempts unless runner | play window {11}",
                SymbolName, MinRangeMinutes, MaxRangeMinutes, ConsolidationBars, ReversalRetraceFrac,
                BreakoutAtrMult, MinOutsideFrac, MaxHesitationWickFrac,
                RiskPercentAPlus, RiskPercentB, MaxOrbAttempts,
                PlayWindowMinutes > 0 ? PlayWindowMinutes + "min" : "off (full session)");
        }

        protected override void OnStop()
        {
            Bars.BarClosed -= OnM1Closed;
            if (_barsM5 != null) _barsM5.BarClosed -= OnM5Closed;
            if (_barsM15 != null) _barsM15.BarClosed -= OnM15Closed;
            Positions.Closed -= OnPositionClosed;
        }

        // =====================================================================
        // NY session clock (self-contained US DST: 2nd Sun Mar - 1st Sun Nov)
        // =====================================================================

        private static bool OfmIsUsDst(DateTime utc)
        {
            int y = utc.Year;
            var mar = new DateTime(y, 3, 1);
            int firstSunMar = ((7 - (int)mar.DayOfWeek) % 7) + 1;
            var dstStart = new DateTime(y, 3, firstSunMar + 7, 7, 0, 0);   // 02:00 EST = 07:00 UTC
            var nov = new DateTime(y, 11, 1);
            int firstSunNov = ((7 - (int)nov.DayOfWeek) % 7) + 1;
            var dstEnd = new DateTime(y, 11, firstSunNov, 6, 0, 0);        // 02:00 EDT = 06:00 UTC
            return utc >= dstStart && utc < dstEnd;
        }

        private static DateTime OfmToNy(DateTime utc) => utc.AddHours(OfmIsUsDst(utc) ? -4 : -5);

        // =====================================================================
        // Bar resolution. Bars.BarClosed append-timing is build-dependent (a fixed
        // Count-2 was observed running one bar STALE), so the just-closed bar is
        // resolved by timestamp: closed iff OpenTime + period <= Server.Time.
        // Each closed bar is processed exactly once, backlog bounded.
        // =====================================================================

        private void DrainClosedBars(Bars bars, TimeSpan period, ref DateTime lastDone, Action<Bars, int> process)
        {
            int last = bars.Count - 1;
            while (last >= 0 && bars.OpenTimes[last].Add(period) > Server.Time) last--;
            if (last < 0 || bars.OpenTimes[last] <= lastDone) return;

            int first = last;
            while (first > 0 && bars.OpenTimes[first - 1] > lastDone) first--;
            first = Math.Max(first, last - 3);   // bound replay after gaps/reloads

            for (int i = first; i <= last; i++)
            {
                lastDone = bars.OpenTimes[i];
                process(bars, i);
            }
        }

        private void OnM1Closed(BarClosedEventArgs e)
        {
            DrainClosedBars(e.Bars, TimeSpan.FromMinutes(1), ref _lastM1Done, ProcessM1Bar);
        }

        // =====================================================================
        // Core loop (m1 bar close)
        // =====================================================================

        private void ProcessM1Bar(Bars bars, int idx)
        {
            if (idx < AtrPeriod + 1) return;

            var utc = bars.OpenTimes[idx];
            var ny = OfmToNy(utc);

            if (ny.Date != _dayNy) ResetDay(ny.Date);

            // manage the open trade on every bar (incl. overnight for swing holds)
            if (_trade != null) ManageTrade(bars, idx);

            bool weekday = ny.DayOfWeek != DayOfWeek.Saturday && ny.DayOfWeek != DayOfWeek.Sunday;
            var t = ny.TimeOfDay;
            var openT = new TimeSpan(OpenHourNy, OpenMinuteNy, 0);
            if (!weekday || t < openT || t >= new TimeSpan(16, 0, 0)) return;

            _lastRthClose = bars.ClosePrices[idx];

            if (double.IsNaN(_sessionOpen))
            {
                _sessionOpen = bars.OpenPrices[idx];
                _sessionOpenUtc = utc;
                _overnightMove = double.IsNaN(_overnightRef) ? double.NaN : _sessionOpen - _overnightRef;
                if (ShowDiagnostics)
                    Print("Session {0:ddd dd MMM}: open {1} | overnight move {2} (ref {3})",
                        ny, _sessionOpen, double.IsNaN(_overnightMove) ? "n/a" : _overnightMove.ToString("F2"), _overnightRef);
            }

            // timed flat / swing hold
            if (t >= new TimeSpan(FlatHourNy, FlatMinuteNy, 0))
            {
                HandleFlatTime(bars, idx);
                return;
            }

            if (!_rangeFormed)
            {
                UpdateFormation(bars, idx, utc);
                return;
            }

            TrackReentryExtremes(bars, idx);

            if (_trade == null) ScanForEntry(bars, idx, t);
        }

        private void ResetDay(DateTime nyDate)
        {
            _dayNy = nyDate;
            _overnightRef = _lastRthClose;
            _sessionOpen = double.NaN;
            _overnightMove = double.NaN;
            _rangeFormed = false;
            _formBars.Clear();
            _formHigh = double.MinValue;
            _formLow = double.MaxValue;
            _orHigh = _orLow = double.NaN;
            _avgFormRange = 0;
            _pending = null;
            _attemptsToday = 0;
            _runnerToday = false;
            _longTriggered = _shortTriggered = false;
            _postHiLong = double.MinValue; _reLoLong = double.MaxValue;
            _postLoShort = double.MaxValue; _reHiShort = double.MinValue;
            _dailyPnl = 0;
            _haltPrintedToday = false;
            // NOTE: _trade deliberately survives the reset (swing holds run past midnight).
        }

        // =====================================================================
        // Opening range: the first meaningful move
        // =====================================================================

        private void UpdateFormation(Bars bars, int idx, DateTime utc)
        {
            double hi = bars.HighPrices[idx], lo = bars.LowPrices[idx], cl = bars.ClosePrices[idx];
            var fb = new OfmFormBar
            {
                High = hi,
                Low = lo,
                Range = hi - lo,
                ExtUp = _formBars.Count == 0 || hi > _formHigh,
                ExtDn = _formBars.Count == 0 || lo < _formLow
            };
            _formBars.Add(fb);
            _formHigh = Math.Max(_formHigh, hi);
            _formLow = Math.Min(_formLow, lo);

            double elapsedMin = (utc.AddMinutes(1) - _sessionOpenUtc).TotalMinutes;
            if (elapsedMin < MinRangeMinutes) return;

            // dominant leg = the larger excursion from the session open
            double upLeg = _formHigh - _sessionOpen;
            double dnLeg = _sessionOpen - _formLow;
            bool up = upLeg >= dnLeg;
            double leg = up ? upLeg : dnLeg;

            // "consolidates for at least N candles": last N bars all failed to extend the run
            bool consolidated = _formBars.Count >= ConsolidationBars;
            if (consolidated)
                for (int i = _formBars.Count - ConsolidationBars; i < _formBars.Count; i++)
                    if (up ? _formBars[i].ExtUp : _formBars[i].ExtDn) { consolidated = false; break; }

            // "runs until it reverses": close gave back >= frac of the open->extreme leg
            bool reversed = leg > 0 && (up
                ? cl <= _formHigh - ReversalRetraceFrac * upLeg
                : cl >= _formLow + ReversalRetraceFrac * dnLeg);

            if (consolidated || reversed || elapsedMin >= MaxRangeMinutes)
            {
                _rangeFormed = true;
                _orHigh = _formHigh;
                _orLow = _formLow;
                double sum = 0;
                foreach (var b in _formBars) sum += b.Range;
                _avgFormRange = sum / _formBars.Count;
                if (ShowDiagnostics)
                    Print("RANGE formed in {0:F0}min ({1}): [{2} .. {3}] width {4:F2} | first move {5}",
                        elapsedMin,
                        consolidated ? "consolidated" : reversed ? "reversed" : "max window",
                        _orLow, _orHigh, _orHigh - _orLow, up ? "up" : "down");
            }
        }

        // =====================================================================
        // Entry engine
        // =====================================================================

        private void TrackReentryExtremes(Bars bars, int idx)
        {
            double hi = bars.HighPrices[idx], lo = bars.LowPrices[idx];
            if (_longTriggered)
            {
                _postHiLong = Math.Max(_postHiLong, hi);
                if (_trade == null) _reLoLong = Math.Min(_reLoLong, lo);
            }
            if (_shortTriggered)
            {
                _postLoShort = Math.Min(_postLoShort, lo);
                if (_trade == null) _reHiShort = Math.Max(_reHiShort, hi);
            }
        }

        private void ScanForEntry(Bars bars, int idx, TimeSpan nyTime)
        {
            // optional play window (0 = off; the framework's own bound is the 3-attempt
            // rule, and the example trades place big re-entry winners 2h+ after the open)
            if (PlayWindowMinutes > 0)
            {
                var windowEnd = new TimeSpan(OpenHourNy, OpenMinuteNy, 0) + TimeSpan.FromMinutes(PlayWindowMinutes);
                if (nyTime >= windowEnd) { _pending = null; return; }
            }
            if (!EntryGuardsPass()) { _pending = null; return; }

            double atr = _atrM1.Result[idx];
            if (double.IsNaN(atr) || atr <= 0) return;

            double hi = bars.HighPrices[idx], lo = bars.LowPrices[idx];
            double op = bars.OpenPrices[idx], cl = bars.ClosePrices[idx];
            double rng = hi - lo;
            double refRng = Math.Max(atr, _avgFormRange);   // "recent opening candles / short-term ATR"

            // ---- 1) armed one-bar confirmation from the previous bar ----
            if (_pending != null)
            {
                var p = _pending;
                _pending = null;
                if (p.IsLong && cl > _orHigh && cl > op)
                {
                    bool strongConfirm = rng >= StrongAtrMult * refRng;
                    double stop = strongConfirm
                        ? _orHigh - EdgeStopInsetAtr * atr
                        : Math.Min(p.CandleLow, lo) - StopBufferAtr * atr;
                    EnterTrade(true, stop, bars, idx, strongConfirm ? "confirmed breakout (strong confirm, edge stop)" : "confirmed breakout");
                    return;
                }
                if (!p.IsLong && cl < _orLow && cl < op)
                {
                    bool strongConfirm = rng >= StrongAtrMult * refRng;
                    double stop = strongConfirm
                        ? _orLow + EdgeStopInsetAtr * atr
                        : Math.Max(p.CandleHigh, hi) + StopBufferAtr * atr;
                    EnterTrade(false, stop, bars, idx, strongConfirm ? "confirmed breakout (strong confirm, edge stop)" : "confirmed breakout");
                    return;
                }
                // confirmation failed -> the bar is still evaluated as a fresh candidate below
            }

            if (rng <= 0) return;
            double prevCl = bars.ClosePrices[idx - 1];

            // ---- 2) fresh range breakout: crossing close + momentum anatomy ----
            if (AllowLongs && prevCl <= _orHigh && cl > _orHigh)
            {
                double outFrac = (hi - _orHigh) / rng;
                double wickFrac = (hi - Math.Max(op, cl)) / rng;
                if (rng >= BreakoutAtrMult * refRng && wickFrac <= MaxHesitationWickFrac)
                {
                    if (outFrac > MinOutsideFrac)
                    {
                        bool veryStrong = rng >= StrongAtrMult * refRng && outFrac >= StrongOutsideFrac;
                        double stop = veryStrong ? _orHigh - EdgeStopInsetAtr * atr : lo - StopBufferAtr * atr;
                        EnterTrade(true, stop, bars, idx, veryStrong ? "breakout (very strong, edge stop)" : "breakout");
                        return;
                    }
                    _pending = new OfmPending { IsLong = true, CandleLow = lo, CandleHigh = hi };
                    if (ShowDiagnostics) Print("Breakout candle strong but only {0:P0} outside - waiting for a confirming candle", outFrac);
                    return;
                }
            }
            if (AllowShorts && prevCl >= _orLow && cl < _orLow)
            {
                double outFrac = (_orLow - lo) / rng;
                double wickFrac = (Math.Min(op, cl) - lo) / rng;
                if (rng >= BreakoutAtrMult * refRng && wickFrac <= MaxHesitationWickFrac)
                {
                    if (outFrac > MinOutsideFrac)
                    {
                        bool veryStrong = rng >= StrongAtrMult * refRng && outFrac >= StrongOutsideFrac;
                        double stop = veryStrong ? _orLow + EdgeStopInsetAtr * atr : hi + StopBufferAtr * atr;
                        EnterTrade(false, stop, bars, idx, veryStrong ? "breakout (very strong, edge stop)" : "breakout");
                        return;
                    }
                    _pending = new OfmPending { IsLong = false, CandleLow = lo, CandleHigh = hi };
                    if (ShowDiagnostics) Print("Breakout candle strong but only {0:P0} outside - waiting for a confirming candle", outFrac);
                    return;
                }
            }

            // ---- 3) pullback re-entry (momentum only, side must have triggered before) ----
            if (AllowLongs && _longTriggered)
            {
                bool pullback = _reLoLong <= _orHigh + RetestProximityAtr * atr
                    || (_postHiLong > _orHigh && _postHiLong - _reLoLong >= ReentryRetraceFrac * (_postHiLong - _orHigh));
                bool momentum = rng >= BreakoutAtrMult * atr
                    && (hi - Math.Max(op, cl)) / rng <= MaxHesitationWickFrac
                    && cl > op && cl > _orHigh;
                if (pullback && momentum)
                {
                    EnterTrade(true, lo - StopBufferAtr * atr, bars, idx, "pullback re-entry");
                    return;
                }
            }
            if (AllowShorts && _shortTriggered)
            {
                bool pullback = _reHiShort >= _orLow - RetestProximityAtr * atr
                    || (_postLoShort < _orLow && _reHiShort - _postLoShort >= ReentryRetraceFrac * (_orLow - _postLoShort));
                bool momentum = rng >= BreakoutAtrMult * atr
                    && (Math.Min(op, cl) - lo) / rng <= MaxHesitationWickFrac
                    && cl < op && cl < _orLow;
                if (pullback && momentum)
                    EnterTrade(false, hi + StopBufferAtr * atr, bars, idx, "pullback re-entry");
            }
        }

        private bool EntryGuardsPass()
        {
            if (_attemptsToday >= MaxOrbAttempts && !_runnerToday) return Halt("3-attempt rule: no runner - not an ORB day");
            if (_dailyPnl <= -Account.Balance * MaxDailyLossPercent / 100.0) return Halt("daily-loss backstop");
            if (MaxSpreadPips > 0 && Symbol.Spread / Symbol.PipSize > MaxSpreadPips) return false;
            return true;
        }

        private bool Halt(string reason)
        {
            if (!_haltPrintedToday)
            {
                Print("Entries halted for the day: {0}", reason);
                _haltPrintedToday = true;
            }
            return false;
        }

        // =====================================================================
        // Bias: A+ needs all 3 checks aligned with the trade direction
        // =====================================================================

        private bool EvaluateBias(bool isLong, double price, out string detail)
        {
            int dIdx = _barsD1.Count - 2;                 // last CLOSED daily bar
            int sIdx = dIdx - SlopeLookbackDays;
            if (dIdx < 1 || sIdx < 0)
            {
                detail = "daily history insufficient";
                return false;
            }
            double ema = _emaD.Result[dIdx], sma = _smaD.Result[dIdx];
            double emaPrev = _emaD.Result[sIdx], smaPrev = _smaD.Result[sIdx];
            if (double.IsNaN(ema) || double.IsNaN(sma) || double.IsNaN(emaPrev) || double.IsNaN(smaPrev))
            {
                detail = "daily MAs not ready";
                return false;
            }

            bool stackOk = isLong ? price > ema && ema > sma : price < ema && ema < sma;
            bool slopeOk = isLong ? ema > emaPrev && sma > smaPrev : ema < emaPrev && sma < smaPrev;

            bool overnightOk = false;
            if (!double.IsNaN(_overnightMove) && !double.IsNaN(_overnightRef) && _overnightRef > 0)
            {
                double minMove = _overnightRef * OvernightMinMovePct / 100.0;
                overnightOk = isLong ? _overnightMove >= minMove : _overnightMove <= -minMove;
            }

            detail = string.Format("stack {0} | slope {1} | overnight {2} (move {3})",
                stackOk ? "OK" : "no", slopeOk ? "OK" : "no", overnightOk ? "OK" : "no",
                double.IsNaN(_overnightMove) ? "n/a" : _overnightMove.ToString("F2"));
            return stackOk && slopeOk && overnightOk;
        }

        // =====================================================================
        // Order execution & sizing (A+ vs B)
        // =====================================================================

        private void EnterTrade(bool isLong, double stopPrice, Bars bars, int idx, string why)
        {
            double atr = _atrM1.Result[idx];
            double signalClose = bars.ClosePrices[idx];
            double entryRef = isLong ? Symbol.Ask : Symbol.Bid;

            // fast-tape guard: the live quote must still be where the signal was.
            // A news tick can put the market an ATR through the level the setup
            // was computed on (observed: fill $1.40 through a gold signal close).
            if (Math.Abs(entryRef - signalClose) > MaxEntrySlippageAtr * atr)
            {
                Print("Entry skipped ({0}): live {1} is {2:F2} from signal close {3} (max {4:F2}) - fast tape",
                    why, entryRef, Math.Abs(entryRef - signalClose), signalClose, MaxEntrySlippageAtr * atr);
                return;
            }

            // structural stop distance floored by ATR, LIVE SPREAD and ABSOLUTE pips.
            // ATR alone is self-referential on quiet mornings: 0.15xATR let $0.06 gold
            // stops through, which sized into ~200x-leverage positions whose SL the
            // server then rounded away (integer pips) - the -78% run.
            double spread = Symbol.Ask - Symbol.Bid;
            double minDist = Math.Max(MinStopAtr * atr,
                             Math.Max(MinStopSpreadMult * spread, MinStopPips * Symbol.PipSize));
            double signedDist = isLong ? entryRef - stopPrice : stopPrice - entryRef;
            if (signedDist < minDist)
            {
                if (ShowDiagnostics)
                    Print("Entry skipped ({0}): stop {1:F2} from live price, floor {2:F2} (atr/spread/pips)",
                        why, signedDist, minDist);
                return;
            }

            // cost-vs-risk: a scalp whose spread eats a quarter of its stop has no edge left
            if (MaxSpreadRiskFrac > 0 && spread > MaxSpreadRiskFrac * signedDist)
            {
                if (ShowDiagnostics)
                    Print("Entry skipped ({0}): spread {1:F2} > {2:P0} of stop distance {3:F2}",
                        why, spread, MaxSpreadRiskFrac, signedDist);
                return;
            }

            string biasDetail;
            bool aligned = EvaluateBias(isLong, signalClose, out biasDetail);
            double riskPct = aligned ? RiskPercentAPlus : RiskPercentB;
            double riskAmt = Account.Balance * riskPct / 100.0;

            double stopPips = signedDist / Symbol.PipSize;
            double perUnit = stopPips * Symbol.PipValue;
            if (perUnit <= 0) return;
            double volume = Symbol.NormalizeVolumeInUnits(riskAmt / perUnit, RoundingMode.Down);

            // notional cap: fixed-fractional sizing explodes as stops shrink; no single
            // trade may lever past MaxNotionalMult x balance (assumes USD-quoted symbol
            // on a USD account - approximate by design, it is a blowup guard not sizing)
            double maxVol = Symbol.NormalizeVolumeInUnits(
                MaxNotionalMult * Account.Balance / entryRef, RoundingMode.Down);
            if (volume > maxVol)
            {
                if (ShowDiagnostics)
                    Print("Volume capped by notional guard: {0} -> {1} ({2}x balance)", volume, maxVol, MaxNotionalMult);
                volume = maxVol;
            }
            if (volume < Symbol.VolumeInUnitsMin)
            {
                Print("Entry volume too small (risk {0:F2}, stop {1:F1}p) - skipped.", riskAmt, stopPips);
                return;
            }
            volume = Math.Min(volume, Symbol.VolumeInUnitsMax);

            // the server rounds stopLossPips to INTEGERS (0.4 -> 0 = SL silently dropped),
            // so the order carries a CEILINGed pip stop - never dropped, never tighter -
            // and the exact structural stop is set by PRICE right after the fill.
            var result = ExecuteMarketOrder(isLong ? TradeType.Buy : TradeType.Sell, SymbolName, volume,
                TradeLabel, Math.Ceiling(stopPips), null);
            if (!result.IsSuccessful || result.Position == null)
            {
                Print("Entry order failed ({0}): {1}", why, result.Error);
                return;
            }
            var pos = result.Position;

            var exact = pos.ModifyStopLossPrice(stopPrice);
            if (pos.StopLoss == null)
            {
                // cannot protect the position -> do not hold it for a single tick
                Print("ABORT: position #{0} filled without a stop loss - closing immediately", pos.Id);
                ClosePosition(pos);
                _attemptsToday++;
                if (isLong) _longTriggered = true; else _shortTriggered = true;
                return;
            }
            if (!exact.IsSuccessful && ShowDiagnostics)
                Print("Exact stop modify failed ({0}) - keeping server stop {1}", exact.Error, pos.StopLoss);

            double actualStop = pos.StopLoss.Value;
            double riskPrice = Math.Abs(pos.EntryPrice - actualStop);
            double riskActual = volume * (riskPrice / Symbol.PipSize) * Symbol.PipValue;

            _trade = new OfmTrade
            {
                IsLong = isLong,
                PositionId = pos.Id,
                Entry = pos.EntryPrice,
                InitialStop = actualStop,
                RiskPrice = riskPrice,
                RiskAmount = riskActual,
                IsAPlus = aligned,
                EntryTimeUtc = Server.Time,
                Stage = OfmStage.M1,
                Extreme = isLong ? bars.HighPrices[idx] : bars.LowPrices[idx],
                StageExtreme = isLong ? bars.HighPrices[idx] : bars.LowPrices[idx],
                PbCount = 0,
                PbExtreme = isLong ? double.MaxValue : double.MinValue,
                // the first impulse is measured from the signal candle's base; the
                // give-back line stays disarmed until price impulses past its extreme
                LegStart = isLong ? bars.LowPrices[idx] : bars.HighPrices[idx],
                ImpulseGateRef = isLong ? bars.HighPrices[idx] : bars.LowPrices[idx],
                ImpulseOn = false
            };
            _attemptsToday++;
            if (isLong) { _longTriggered = true; _reLoLong = double.MaxValue; }
            else { _shortTriggered = true; _reHiShort = double.MinValue; }

            Print("ENTER {0} ({1}) attempt {2}{3} | {4} @ {5} stop {6} ({7:F1}p, risk {8:F2}) vol {9} | bias: {10}",
                isLong ? "LONG" : "SHORT", why, _attemptsToday,
                _runnerToday ? " (runner day, uncapped)" : string.Format("/{0}", MaxOrbAttempts),
                aligned ? "A+" : "B", pos.EntryPrice, actualStop, riskPrice / Symbol.PipSize, riskActual, volume, biasDetail);
        }

        // =====================================================================
        // Trade management: scalp -> runner -> swing
        // =====================================================================

        private Position FindTradePosition()
        {
            if (_trade == null) return null;
            foreach (var pos in Positions.FindAll(TradeLabel, SymbolName))
                if (pos.Id == _trade.PositionId) return pos;
            return null;
        }

        // A live position without a stop loss is an emergency, never a valid state:
        // restore the initial stop or flatten. (Root cause of the -78% run: a naked
        // position whose "trail" then installed a stop 39R away from entry.)
        private bool EnsureProtected(Position pos)
        {
            if (pos.StopLoss != null) return true;
            Print("EMERGENCY: position #{0} has no stop loss - restoring {1}", pos.Id, _trade.InitialStop);
            var fix = pos.ModifyStopLossPrice(_trade.InitialStop);
            if (fix.IsSuccessful && pos.StopLoss != null) return true;
            Print("EMERGENCY: cannot protect position #{0} ({1}) - closing now", pos.Id, fix.Error);
            ClosePosition(pos);
            return false;
        }

        private void ManageTrade(Bars bars, int idx)
        {
            var pos = FindTradePosition();
            if (pos == null) { _trade = null; return; }   // safety: close event not yet seen
            if (!EnsureProtected(pos)) return;

            var t = _trade;
            double hi = bars.HighPrices[idx], lo = bars.LowPrices[idx];
            t.Extreme = t.IsLong ? Math.Max(t.Extreme, hi) : Math.Min(t.Extreme, lo);

            // "up ~1.5R" is floating profit: latch off the best price since entry,
            // not the close, so a runner that wicked to 1.5R still counts as one
            double bestR = (t.IsLong ? t.Extreme - t.Entry : t.Entry - t.Extreme) / t.RiskPrice;
            if (!t.Runner && bestR >= EaseUpR)
            {
                t.Runner = true;
                _runnerToday = true;
                if (t.Stage == OfmStage.M1) ShiftStage(t, OfmStage.M5, bestR);
            }
            if (UseStage3 && t.Stage == OfmStage.M5 && bestR >= Stage3R)
                ShiftStage(t, OfmStage.M15, bestR);

            if (t.Stage == OfmStage.M1)
                TrailStep(pos, bars, idx, _atrM1.Result[idx]);
        }

        private void ShiftStage(OfmTrade t, OfmStage stage, double openR)
        {
            t.Stage = stage;
            t.StageExtreme = t.Extreme;   // carry the best price into the new TF
            t.PbCount = 0;
            t.PbExtreme = t.IsLong ? double.MaxValue : double.MinValue;
            // LegStart (a price level) stays valid across timeframes
            if (ShowDiagnostics)
                Print("EASE UP: +{0:F1}R - management shifted to {1}", openR, stage == OfmStage.M5 ? "5m" : "15m");
        }

        private void OnM5Closed(BarClosedEventArgs e)
        {
            DrainClosedBars(e.Bars, TimeSpan.FromMinutes(5), ref _lastM5Done, ProcessM5Bar);
        }

        private void OnM15Closed(BarClosedEventArgs e)
        {
            DrainClosedBars(e.Bars, TimeSpan.FromMinutes(15), ref _lastM15Done, ProcessM15Bar);
        }

        private void ProcessM5Bar(Bars bars, int idx)
        {
            if (_trade == null || _trade.Stage != OfmStage.M5) return;
            if (idx < AtrPeriod + 1) return;
            if (bars.OpenTimes[idx] < _trade.EntryTimeUtc) return;   // skip bars that straddle the entry
            var pos = FindTradePosition();
            if (pos == null) { _trade = null; return; }
            TrailStep(pos, bars, idx, _atrM5.Result[idx]);
        }

        private void ProcessM15Bar(Bars bars, int idx)
        {
            if (_trade == null || _trade.Stage != OfmStage.M15) return;
            if (idx < AtrPeriod + 1) return;
            if (bars.OpenTimes[idx] < _trade.EntryTimeUtc) return;
            var pos = FindTradePosition();
            if (pos == null) { _trade = null; return; }
            TrailStep(pos, bars, idx, _atrM15.Result[idx]);
        }

        // One trail step on the management timeframe. The stop sits under the most recent
        // MEANINGFUL structure ("trail under retracements, 4-bar consolidations, or roughly
        // a 40%/60% retrace of impulses") and only ever ratchets:
        //   1. a retracement that just completed (new extreme) moves the stop under its
        //      low - only if it was deep enough to be structure (>= MinPullbackAtr x ATR);
        //      micro-dips no longer jam the stop against the extreme;
        //   2. an N-bar consolidation that fails to extend -> stop under the stall
        //      (stated verbatim in the framework, so no depth gate);
        //   3. the give-back line (extreme - 40%/60% of the current impulse) arms only
        //      once price has impulsed MinImpulseAtr x ATR past the signal candle -
        //      before that the trade keeps its birth stop, "under the breakout candle".
        private void TrailStep(Position pos, Bars bars, int idx, double atr)
        {
            if (double.IsNaN(atr) || atr <= 0) return;
            if (!EnsureProtected(pos)) return;
            var t = _trade;
            double hi = bars.HighPrices[idx], lo = bars.LowPrices[idx];

            if (t.IsLong)
            {
                double best = pos.StopLoss ?? t.InitialStop;
                if (hi > t.StageExtreme)
                {
                    if (t.PbCount >= 1 && t.PbExtreme < double.MaxValue
                        && t.StageExtreme - t.PbExtreme >= MinPullbackAtr * atr)
                    {
                        best = Math.Max(best, t.PbExtreme - TrailBufferAtr * atr);   // completed retracement
                        t.LegStart = t.PbExtreme;                                    // new impulse anchor
                    }
                    t.StageExtreme = hi;
                    t.PbCount = 0;
                    t.PbExtreme = double.MaxValue;
                }
                else
                {
                    t.PbCount++;
                    t.PbExtreme = Math.Min(t.PbExtreme, lo);
                    if (t.PbCount >= ConsolidationTrailBars)
                        best = Math.Max(best, t.PbExtreme - TrailBufferAtr * atr);   // consolidation stall
                }
                if (!t.ImpulseOn && t.StageExtreme - t.ImpulseGateRef >= MinImpulseAtr * atr)
                    t.ImpulseOn = true;
                if (t.ImpulseOn)
                {
                    double leg = t.StageExtreme - t.LegStart;
                    if (leg > 0)
                    {
                        double frac = leg >= BigImpulseAtrMult * atr ? BigImpulseRetraceFrac : SmallImpulseRetraceFrac;
                        best = Math.Max(best, t.StageExtreme - frac * leg);          // impulse give-back line
                    }
                }
                best = Math.Min(best, Symbol.Bid - 2 * Symbol.TickSize);
                double cur = pos.StopLoss ?? double.MinValue;
                if (best > cur + Symbol.TickSize * 0.5)
                {
                    var r = pos.ModifyStopLossPrice(best);
                    if (!r.IsSuccessful) Print("Trail modify failed: {0}", r.Error);
                }
            }
            else
            {
                double best = pos.StopLoss ?? t.InitialStop;
                if (lo < t.StageExtreme)
                {
                    if (t.PbCount >= 1 && t.PbExtreme > double.MinValue
                        && t.PbExtreme - t.StageExtreme >= MinPullbackAtr * atr)
                    {
                        best = Math.Min(best, t.PbExtreme + TrailBufferAtr * atr);
                        t.LegStart = t.PbExtreme;
                    }
                    t.StageExtreme = lo;
                    t.PbCount = 0;
                    t.PbExtreme = double.MinValue;
                }
                else
                {
                    t.PbCount++;
                    t.PbExtreme = Math.Max(t.PbExtreme, hi);
                    if (t.PbCount >= ConsolidationTrailBars)
                        best = Math.Min(best, t.PbExtreme + TrailBufferAtr * atr);
                }
                if (!t.ImpulseOn && t.ImpulseGateRef - t.StageExtreme >= MinImpulseAtr * atr)
                    t.ImpulseOn = true;
                if (t.ImpulseOn)
                {
                    double leg = t.LegStart - t.StageExtreme;
                    if (leg > 0)
                    {
                        double frac = leg >= BigImpulseAtrMult * atr ? BigImpulseRetraceFrac : SmallImpulseRetraceFrac;
                        best = Math.Min(best, t.StageExtreme + frac * leg);
                    }
                }
                best = Math.Max(best, Symbol.Ask + 2 * Symbol.TickSize);
                double cur = pos.StopLoss ?? double.MaxValue;
                if (best < cur - Symbol.TickSize * 0.5)
                {
                    var r = pos.ModifyStopLossPrice(best);
                    if (!r.IsSuccessful) Print("Trail modify failed: {0}", r.Error);
                }
            }
        }

        // =====================================================================
        // Timed flat / swing hold
        // =====================================================================

        private void HandleFlatTime(Bars bars, int idx)
        {
            _pending = null;
            if (_trade == null) return;
            if (_trade.SwingHold) return;                 // decision already made today

            var pos = FindTradePosition();
            if (pos == null) { _trade = null; return; }

            double cl = bars.ClosePrices[idx];
            double openR = (_trade.IsLong ? cl - _trade.Entry : _trade.Entry - cl) / _trade.RiskPrice;
            if (AllowSwingHold && _trade.IsAPlus && openR >= EaseUpR)
            {
                _trade.SwingHold = true;
                Print("SWING HOLD engaged: A+ runner at +{0:F1}R held past the close (trail continues on {1})",
                    openR, _trade.Stage == OfmStage.M15 ? "15m" : "5m");
                return;
            }
            var r = ClosePosition(pos);
            if (!r.IsSuccessful) Print("Timed flat failed: {0}", r.Error);
        }

        // =====================================================================
        // Bookkeeping
        // =====================================================================

        private void OnPositionClosed(PositionClosedEventArgs args)
        {
            var pos = args.Position;
            if (pos.Label != TradeLabel || pos.SymbolName != SymbolName) return;
            _dailyPnl += pos.NetProfit;

            if (_trade != null && pos.Id == _trade.PositionId)
            {
                double realizedR = _trade.RiskAmount > 0 ? pos.NetProfit / _trade.RiskAmount : 0;
                if (ShowDiagnostics)
                    Print("CLOSED #{0} {1} net {2:F2} ({3:F2}R) reason {4} | attempts {5} runner-day {6} | day PnL {7:F2}",
                        pos.Id, _trade.IsLong ? "LONG" : "SHORT", pos.NetProfit, realizedR, args.Reason,
                        _attemptsToday, _runnerToday, _dailyPnl);

                if (_trade.IsLong) _reLoLong = double.MaxValue;   // fresh pullback tracking from this exit
                else _reHiShort = double.MinValue;
                _trade = null;

                if (_attemptsToday >= MaxOrbAttempts && !_runnerToday)
                    Print("{0} ORB attempts, no runner - the opening imbalance didn't pay. Standing down for the day.", _attemptsToday);
            }
        }
    }
}
