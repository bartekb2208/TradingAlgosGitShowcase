using System;
using System.Collections.Generic;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    // =====================================================================================
    // Trend Volatility-Breakout cBot  —  Donchian breakout + ATR risk scaling  —  BACKTESTABLE
    // Designed for liquid FX majors / index CFDs on H4 (also works on H1/D1 — retune periods).
    // -------------------------------------------------------------------------------------
    // RATIONALE. Time-series momentum is the most extensively documented systematic effect
    // in the academic literature (Moskowitz, Ooi & Pedersen 2012 "Time Series Momentum";
    // Hurst, Ooi & Pedersen 2017 "A Century of Evidence on Trend-Following Investing").
    // The classic retail-tradable implementation is the Donchian channel breakout (Turtle
    // rules): enter when price closes beyond the N-bar extreme, size positions inversely to
    // volatility, cut losers fast with an ATR stop, and let winners run with a trailing exit.
    //
    // SIGNAL.  On each closed bar:
    //   * LONG  when Close > highest High of the previous `EntryPeriod` bars.
    //   * SHORT when Close < lowest  Low  of the previous `EntryPeriod` bars.
    //   (The channel EXCLUDES the just-closed bar, so the breakout bar itself triggers.)
    //
    // FILTERS.
    //   * Regime    — higher-TF EMA (default Daily/100): longs only above it, shorts below.
    //   * Trendiness— ADX floor (default 20) rejects breakouts inside chop, where Donchian
    //                 systems bleed via whipsaws.
    //   * Session   — optional UTC entry window (default OFF: trends are 24h phenomena).
    //
    // EXITS (first one hit wins).
    //   * Initial stop      — `StopAtrMult`·ATR from entry (volatility-scaled).
    //   * Chandelier trail  — stop ratchets to (extreme since entry) ∓ `TrailAtrMult`·ATR,
    //                         updated on bar close only, never loosened.
    //   * Channel exit      — close beyond the opposite `ExitPeriod` Donchian band (Turtle
    //                         "System 2" exit), catching trend reversals the trail misses.
    //   * Breakeven         — stop to entry after `BreakevenTriggerR`·initial-risk in profit.
    //   * Optional fixed TP — `RiskReward` > 0 sets a target; default 0 = none, because
    //                         capping winners destroys the fat right tail trend systems need.
    //
    // RISK. Fixed-fractional sizing against the ATR stop (so position size is automatically
    // volatility-targeted); spread filter; daily-loss cap; consecutive-loss halt; max
    // trades/day; max concurrent positions.
    //
    // HONESTY NOTE. No strategy is guaranteed profitable; this is the highest-evidence edge
    // implementable on retail data, shipped with conservative defaults. Trend following is
    // a low-win-rate (~35-45%), positive-skew style: expect losing streaks and flat months,
    // with profits concentrated in a few large trends. Walk-forward test per instrument
    // (H4, several years, real spreads + commission) before risking live capital.
    //
    // NOTE on types: helper types are prefixed `Tvb...` so this bot stays self-contained
    // and never collides with the other single-file bots if compiled into one assembly.
    // =====================================================================================
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class TrendVolBreakoutBot : Robot
    {
        #region Parameters - Signal

        [Parameter("Entry Channel (bars)", Group = "Signal", DefaultValue = 55, MinValue = 10, MaxValue = 400)]
        public int EntryPeriod { get; set; }        // Donchian breakout lookback (Turtle System 2 = 55)

        [Parameter("Exit Channel (bars)", Group = "Signal", DefaultValue = 20, MinValue = 5, MaxValue = 200)]
        public int ExitPeriod { get; set; }         // opposite-band exit lookback (Turtle = 20)

        [Parameter("ATR Period", Group = "Signal", DefaultValue = 20, MinValue = 5, MaxValue = 100)]
        public int AtrPeriod { get; set; }

        [Parameter("Allow Longs", Group = "Signal", DefaultValue = true)]
        public bool AllowLongs { get; set; }

        [Parameter("Allow Shorts", Group = "Signal", DefaultValue = true)]
        public bool AllowShorts { get; set; }

        #endregion

        #region Parameters - Regime Filter

        [Parameter("Use Regime Filter", Group = "Regime", DefaultValue = true)]
        public bool UseRegimeFilter { get; set; }

        [Parameter("Regime Timeframe", Group = "Regime", DefaultValue = "Daily")]
        public TimeFrame RegimeTimeFrame { get; set; }

        [Parameter("Regime EMA Period", Group = "Regime", DefaultValue = 100, MinValue = 10, MaxValue = 400)]
        public int RegimeMaPeriod { get; set; }

        [Parameter("Use ADX Filter", Group = "Regime", DefaultValue = true)]
        public bool UseAdxFilter { get; set; }

        [Parameter("ADX Period", Group = "Regime", DefaultValue = 14, MinValue = 5, MaxValue = 50)]
        public int AdxPeriod { get; set; }

        [Parameter("Min ADX", Group = "Regime", DefaultValue = 20.0, MinValue = 5.0, MaxValue = 50.0)]
        public double MinAdx { get; set; }          // reject breakouts in chop

        // Equity indices drift upward; corrections dip under a flat EMA and V-recover, so
        // "price below EMA" alone produces losing shorts in bull markets. Requiring the EMA
        // itself to be FALLING keeps the 2022-style bear-market shorts and drops the rest.
        [Parameter("Shorts Need Falling Regime EMA", Group = "Regime", DefaultValue = true)]
        public bool ShortsNeedFallingMa { get; set; }

        [Parameter("Regime Slope Lookback (bars)", Group = "Regime", DefaultValue = 5, MinValue = 1, MaxValue = 50)]
        public int RegimeSlopeBars { get; set; }

        #endregion

        #region Parameters - Session Filter (UTC)

        [Parameter("Use Session Filter", Group = "Session", DefaultValue = false)]
        public bool UseSessionFilter { get; set; }  // default off: trends run around the clock

        [Parameter("Session Start Hour (UTC)", Group = "Session", DefaultValue = 7, MinValue = 0, MaxValue = 23)]
        public int SessionStartHour { get; set; }

        [Parameter("Session End Hour (UTC)", Group = "Session", DefaultValue = 20, MinValue = 0, MaxValue = 24)]
        public int SessionEndHour { get; set; }

        #endregion

        #region Parameters - Exits

        [Parameter("Use Channel Exit", Group = "Exits", DefaultValue = true)]
        public bool UseChannelExit { get; set; }

        [Parameter("Trail ATR Multiplier", Group = "Exits", DefaultValue = 3.0, MinValue = 0.5, MaxValue = 10.0)]
        public double TrailAtrMult { get; set; }    // chandelier distance from extreme-since-entry

        [Parameter("Use Breakeven Stop", Group = "Exits", DefaultValue = true)]
        public bool UseBreakeven { get; set; }

        [Parameter("Breakeven Trigger (R)", Group = "Exits", DefaultValue = 1.0, MinValue = 0.1, MaxValue = 5.0)]
        public double BreakevenTriggerR { get; set; }

        [Parameter("Use Time Stop", Group = "Exits", DefaultValue = false)]
        public bool UseTimeStop { get; set; }       // default off: time-capping truncates trends

        [Parameter("Max Bars In Trade", Group = "Exits", DefaultValue = 120, MinValue = 5, MaxValue = 2000)]
        public int MaxBarsInTrade { get; set; }

        #endregion

        #region Parameters - Risk

        [Parameter("Risk Per Trade (%)", Group = "Risk", DefaultValue = 0.5, MinValue = 0.05, MaxValue = 3.0)]
        public double RiskPercent { get; set; }

        [Parameter("Stop ATR Multiplier", Group = "Risk", DefaultValue = 2.5, MinValue = 0.5, MaxValue = 8.0)]
        public double StopAtrMult { get; set; }

        [Parameter("Risk:Reward (0 = no TP)", Group = "Risk", DefaultValue = 0.0, MinValue = 0.0, MaxValue = 10.0)]
        public double RiskReward { get; set; }

        [Parameter("Max Spread (pips)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.0, MaxValue = 100.0)]
        public double MaxSpreadPips { get; set; }

        [Parameter("Max Daily Loss (% of balance)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.5, MaxValue = 25.0)]
        public double MaxDailyLossPercent { get; set; }

        [Parameter("Max Consecutive Losses", Group = "Risk", DefaultValue = 4, MinValue = 1, MaxValue = 20)]
        public int MaxConsecutiveLosses { get; set; }   // halts entries for the rest of the day

        [Parameter("Max Trades Per Day", Group = "Risk", DefaultValue = 3, MinValue = 1, MaxValue = 50)]
        public int MaxTradesPerDay { get; set; }

        [Parameter("Max Concurrent Positions", Group = "Risk", DefaultValue = 1, MinValue = 1, MaxValue = 5)]
        public int MaxPositions { get; set; }

        #endregion

        #region Parameters - Meta

        [Parameter("Trade Label", Group = "Meta", DefaultValue = "TVB")]
        public string TradeLabel { get; set; }

        [Parameter("Show Diagnostics", Group = "Meta", DefaultValue = true)]
        public bool ShowDiagnostics { get; set; }

        #endregion

        // --- Per-position bookkeeping (keyed by Position.Id) ---
        private sealed class TvbPositionState
        {
            public double InitialRiskPrice;     // entry stop distance in price units
            public double ExtremeSinceEntry;    // highest high (long) / lowest low (short)
            public int EntryBarIndex;           // Bars.Count at entry
            public bool BreakevenDone;
        }

        // --- State ---
        private AverageTrueRange _atr;
        private DirectionalMovementSystem _dms;
        private ExponentialMovingAverage _regimeMa;
        private Bars _regimeBars;

        private readonly Dictionary<int, TvbPositionState> _posState = new Dictionary<int, TvbPositionState>();

        // Risk / session state
        private DateTime _currentDay = DateTime.MinValue;
        private double _dailyRealizedPnl;
        private int _dailyTradeCount;
        private int _consecutiveLosses;
        private bool _haltPrintedToday;

        // =====================================================================
        // Lifecycle
        // =====================================================================

        protected override void OnStart()
        {
            if (ExitPeriod >= EntryPeriod)
                Print("Warning: Exit channel ({0}) >= entry channel ({1}); exits may fire immediately after entry.",
                    ExitPeriod, EntryPeriod);

            _atr = Indicators.AverageTrueRange(Bars, AtrPeriod, MovingAverageType.Exponential);
            _dms = Indicators.DirectionalMovementSystem(Bars, AdxPeriod);
            _regimeBars = MarketData.GetBars(RegimeTimeFrame);
            _regimeMa = Indicators.ExponentialMovingAverage(_regimeBars.ClosePrices, RegimeMaPeriod);

            Bars.BarClosed += OnBarClosed;
            Positions.Closed += OnPositionClosed;

            _currentDay = Server.Time.Date;

            Print("TrendVolBreakout started on {0} {1}. Entry={2} Exit={3} ATR={4} PipSize={5} VolMin={6} VolStep={7}",
                SymbolName, Bars.TimeFrame, EntryPeriod, ExitPeriod, AtrPeriod,
                Symbol.PipSize, Symbol.VolumeInUnitsMin, Symbol.VolumeInUnitsStep);
        }

        protected override void OnStop()
        {
            Bars.BarClosed -= OnBarClosed;
            Positions.Closed -= OnPositionClosed;
        }

        // Breakeven is checked tick-by-tick so a fast intrabar move can still lock in entry.
        protected override void OnTick()
        {
            if (UseBreakeven)
                ManageBreakeven();
        }

        // =====================================================================
        // Core: runs on every closed bar
        // =====================================================================

        private void OnBarClosed(BarClosedEventArgs e)
        {
            var bars = e.Bars;
            int idx = bars.Count - 2;   // index of the bar that just closed
            if (idx < Math.Max(EntryPeriod, ExitPeriod) + 1) return;

            // --- 1. New day housekeeping ---
            var barDay = bars.OpenTimes[idx].Date;
            if (barDay != _currentDay)
            {
                _currentDay = barDay;
                _dailyRealizedPnl = 0;
                _dailyTradeCount = 0;
                _consecutiveLosses = 0;     // loss-streak halt is a per-day circuit breaker
                _haltPrintedToday = false;
            }

            // --- 2. Manage open positions (trail / channel exit / time stop) ---
            ManageOpenPositions(bars, idx);

            // --- 3. Look for a new breakout entry ---
            TrySignal(bars, idx);
        }

        // =====================================================================
        // Signal + execution
        // =====================================================================

        private void TrySignal(Bars bars, int idx)
        {
            if (!TradingGuardsPass()) return;
            if (UseSessionFilter && !InSession(Server.Time)) return;

            // Donchian channel over the EntryPeriod bars BEFORE the just-closed bar.
            double entryHigh = Highest(bars.HighPrices, idx - EntryPeriod, EntryPeriod);
            double entryLow = Lowest(bars.LowPrices, idx - EntryPeriod, EntryPeriod);
            double close = bars.ClosePrices[idx];

            int dir = 0;
            if (close > entryHigh) dir = +1;
            else if (close < entryLow) dir = -1;
            if (dir == 0) return;

            if (dir > 0 && !AllowLongs) return;
            if (dir < 0 && !AllowShorts) return;

            // Trendiness: ADX floor rejects breakouts inside chop.
            if (UseAdxFilter)
            {
                double adx = _dms.ADX.LastValue;
                if (double.IsNaN(adx) || adx < MinAdx)
                {
                    if (ShowDiagnostics)
                        Print("Breakout {0} rejected: ADX {1:F1} < {2:F1}", dir > 0 ? "LONG" : "SHORT", adx, MinAdx);
                    return;
                }
            }

            // Regime: only trade in the higher-timeframe trend direction.
            if (UseRegimeFilter)
            {
                int regime = RegimeDirection();
                if (regime != 0 && dir != regime)
                {
                    if (ShowDiagnostics)
                        Print("Breakout {0} rejected: against {1} regime", dir > 0 ? "LONG" : "SHORT", RegimeTimeFrame);
                    return;
                }

                if (dir < 0 && ShortsNeedFallingMa && !RegimeMaFalling())
                {
                    if (ShowDiagnostics)
                        Print("Breakout SHORT rejected: regime EMA not falling");
                    return;
                }
            }

            // One position per direction: never stack same-direction entries.
            foreach (var p in Positions.FindAll(TradeLabel, SymbolName))
            {
                int pDir = p.TradeType == TradeType.Buy ? +1 : -1;
                if (pDir == dir) return;
            }

            Execute(dir, close, entryHigh, entryLow);
        }

        private void Execute(int dir, double close, double entryHigh, double entryLow)
        {
            var tradeType = dir > 0 ? TradeType.Buy : TradeType.Sell;

            double atr = _atr.Result.LastValue;
            if (double.IsNaN(atr) || atr <= 0) return;

            double stopPrice = StopAtrMult * atr;
            double stopPips = stopPrice / Symbol.PipSize;
            if (stopPips <= 0) return;

            double? tpPips = RiskReward > 0 ? stopPips * RiskReward : (double?)null;

            double volume = CalculateVolume(stopPips);
            if (volume <= 0)
            {
                Print("Volume too small for risk {0}% with stop {1:F1} pips — skipping.", RiskPercent, stopPips);
                return;
            }

            var result = ExecuteMarketOrder(tradeType, SymbolName, volume, TradeLabel, stopPips, tpPips);
            if (!result.IsSuccessful || result.Position == null)
            {
                Print("Order failed: {0}", result.Error);
                return;
            }

            var pos = result.Position;
            _posState[pos.Id] = new TvbPositionState
            {
                InitialRiskPrice = stopPrice,
                ExtremeSinceEntry = pos.EntryPrice,
                EntryBarIndex = Bars.Count,
                BreakevenDone = false
            };
            _dailyTradeCount++;

            if (ShowDiagnostics)
                Print("ENTER {0} {1} @ {2} | channel [{3} / {4}] close {5} | ATR {6} stop {7:F1}p vol {8}",
                    tradeType, SymbolName, pos.EntryPrice, entryLow, entryHigh, close, atr, stopPips, volume);
        }

        // Fixed-fractional: risk RiskPercent of balance against the stop distance.
        private double CalculateVolume(double stopPips)
        {
            double riskAmount = Account.Balance * RiskPercent / 100.0;
            double perUnitRisk = stopPips * Symbol.PipValue;   // account-currency risk per 1 unit
            if (perUnitRisk <= 0) return 0;

            double raw = riskAmount / perUnitRisk;
            double volume = Symbol.NormalizeVolumeInUnits(raw, RoundingMode.Down);
            if (volume < Symbol.VolumeInUnitsMin) return 0;
            return Math.Min(volume, Symbol.VolumeInUnitsMax);
        }

        // =====================================================================
        // Position management
        // =====================================================================

        private void ManageOpenPositions(Bars bars, int idx)
        {
            double atr = _atr.Result.LastValue;
            double barHigh = bars.HighPrices[idx];
            double barLow = bars.LowPrices[idx];
            double close = bars.ClosePrices[idx];

            // Opposite Donchian band over the ExitPeriod bars before the just-closed bar.
            double exitLow = Lowest(bars.LowPrices, idx - ExitPeriod, ExitPeriod);
            double exitHigh = Highest(bars.HighPrices, idx - ExitPeriod, ExitPeriod);

            foreach (var pos in Positions.FindAll(TradeLabel, SymbolName))
            {
                var st = GetOrRebuildState(pos);
                bool isLong = pos.TradeType == TradeType.Buy;

                // Time stop.
                if (UseTimeStop && Bars.Count - st.EntryBarIndex >= MaxBarsInTrade)
                {
                    if (ShowDiagnostics) Print("TIME EXIT {0} after {1} bars", pos.Id, MaxBarsInTrade);
                    ClosePosition(pos);
                    continue;
                }

                // Channel exit: close beyond the opposite band ends the trade.
                if (UseChannelExit && ((isLong && close < exitLow) || (!isLong && close > exitHigh)))
                {
                    if (ShowDiagnostics) Print("CHANNEL EXIT {0} @ {1}", pos.Id, close);
                    ClosePosition(pos);
                    continue;
                }

                // Chandelier trail: ratchet stop behind the extreme since entry. Never loosen.
                st.ExtremeSinceEntry = isLong
                    ? Math.Max(st.ExtremeSinceEntry, barHigh)
                    : Math.Min(st.ExtremeSinceEntry, barLow);

                if (double.IsNaN(atr) || atr <= 0) continue;
                double trailStop = isLong
                    ? st.ExtremeSinceEntry - TrailAtrMult * atr
                    : st.ExtremeSinceEntry + TrailAtrMult * atr;
                trailStop = Math.Round(trailStop, Symbol.Digits);

                bool improves = pos.StopLoss == null ||
                    (isLong ? trailStop > pos.StopLoss.Value : trailStop < pos.StopLoss.Value);
                bool valid = isLong ? trailStop < Symbol.Bid : trailStop > Symbol.Ask;
                if (improves && valid)
                    ModifyPosition(pos, trailStop, pos.TakeProfit, ProtectionType.Absolute);
            }
        }

        private void ManageBreakeven()
        {
            foreach (var pos in Positions.FindAll(TradeLabel, SymbolName))
            {
                var st = GetOrRebuildState(pos);
                if (st.BreakevenDone || st.InitialRiskPrice <= 0) continue;

                bool isLong = pos.TradeType == TradeType.Buy;
                double progress = isLong ? Symbol.Bid - pos.EntryPrice : pos.EntryPrice - Symbol.Ask;
                if (progress < BreakevenTriggerR * st.InitialRiskPrice) continue;

                double be = Math.Round(pos.EntryPrice, Symbol.Digits);
                bool improves = pos.StopLoss == null ||
                    (isLong ? be > pos.StopLoss.Value : be < pos.StopLoss.Value);
                if (improves)
                {
                    var r = ModifyPosition(pos, be, pos.TakeProfit, ProtectionType.Absolute);
                    if (r.IsSuccessful && ShowDiagnostics)
                        Print("BREAKEVEN set for {0} @ {1}", pos.Id, be);
                }
                st.BreakevenDone = true;   // try once; trail takes over from here
            }
        }

        // Rebuild state for positions we lost track of (e.g. bot restart on a live chart).
        private TvbPositionState GetOrRebuildState(Position pos)
        {
            TvbPositionState st;
            if (_posState.TryGetValue(pos.Id, out st)) return st;

            double risk = pos.StopLoss.HasValue
                ? Math.Abs(pos.EntryPrice - pos.StopLoss.Value)
                : StopAtrMult * _atr.Result.LastValue;
            st = new TvbPositionState
            {
                InitialRiskPrice = risk,
                ExtremeSinceEntry = pos.EntryPrice,
                EntryBarIndex = Bars.Count,
                BreakevenDone = false
            };
            _posState[pos.Id] = st;
            return st;
        }

        private void OnPositionClosed(PositionClosedEventArgs args)
        {
            var pos = args.Position;
            if (pos.Label != TradeLabel || pos.SymbolName != SymbolName) return;

            _dailyRealizedPnl += pos.NetProfit;
            if (pos.NetProfit < 0) _consecutiveLosses++;
            else if (pos.NetProfit > 0) _consecutiveLosses = 0;
            _posState.Remove(pos.Id);

            if (ShowDiagnostics)
                Print("CLOSED {0} net {1:F2} | day PnL {2:F2} | streak {3} losses",
                    pos.Id, pos.NetProfit, _dailyRealizedPnl, _consecutiveLosses);
        }

        // =====================================================================
        // Guards / helpers
        // =====================================================================

        private bool TradingGuardsPass()
        {
            if (Positions.FindAll(TradeLabel, SymbolName).Length >= MaxPositions) return false;
            if (_dailyTradeCount >= MaxTradesPerDay) return Halt("max trades/day reached");
            if (_consecutiveLosses >= MaxConsecutiveLosses) return Halt("consecutive-loss halt");

            double dailyCap = Account.Balance * MaxDailyLossPercent / 100.0;
            if (_dailyRealizedPnl <= -dailyCap) return Halt("daily-loss cap hit");

            double spreadPips = Symbol.Spread / Symbol.PipSize;
            if (MaxSpreadPips > 0 && spreadPips > MaxSpreadPips) return false;

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

        // +1 bullish (price above regime EMA), -1 bearish, 0 unknown/flat.
        private int RegimeDirection()
        {
            if (_regimeMa == null || _regimeBars.ClosePrices.Count == 0) return 0;
            double ma = _regimeMa.Result.LastValue;
            if (double.IsNaN(ma) || ma <= 0) return 0;
            double price = _regimeBars.ClosePrices.LastValue;
            if (price > ma) return +1;
            if (price < ma) return -1;
            return 0;
        }

        // True when the regime EMA is lower than it was RegimeSlopeBars regime-TF bars ago.
        private bool RegimeMaFalling()
        {
            var series = _regimeMa.Result;
            if (series.Count <= RegimeSlopeBars) return false;
            double now = series.LastValue;
            double then = series.Last(RegimeSlopeBars);
            if (double.IsNaN(now) || double.IsNaN(then)) return false;
            return now < then;
        }

        private bool InSession(DateTime utc)
        {
            int h = utc.Hour;
            if (SessionStartHour == SessionEndHour) return true;          // 24h
            if (SessionStartHour < SessionEndHour)
                return h >= SessionStartHour && h < SessionEndHour;        // same-day window
            return h >= SessionStartHour || h < SessionEndHour;           // wraps midnight
        }

        private static double Highest(DataSeries s, int startIdx, int count)
        {
            double h = double.MinValue;
            for (int i = startIdx; i < startIdx + count; i++)
                h = Math.Max(h, s[i]);
            return h;
        }

        private static double Lowest(DataSeries s, int startIdx, int count)
        {
            double l = double.MaxValue;
            for (int i = startIdx; i < startIdx + count; i++)
                l = Math.Min(l, s[i]);
            return l;
        }
    }
}
