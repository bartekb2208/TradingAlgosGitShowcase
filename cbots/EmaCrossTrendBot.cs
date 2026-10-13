using System;
using System.Collections.Generic;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    // =====================================================================================
    // EMA-Cross Trend cBot  —  long-only dual-EMA crossover for crypto CFDs  —  BACKTESTABLE
    // Designed for BTC/USD on H4/D1; cross-market test the SAME settings on ETH/USD and
    // SOL/USD (the goal is behaviour that repeats, not a perfect per-symbol setting).
    // -------------------------------------------------------------------------------------
    // HYPOTHESIS. Bitcoin has an upside bias driven by (1) limited supply — fixed issuance,
    // so demand shocks resolve through price; (2) speculative adoption — risk-on capital
    // flows in aggressively; (3) momentum behaviour — once a trend starts it persists longer
    // than expected. Therefore a LONG-ONLY trend-following system may capture part of those
    // upside moves while standing aside in weak regimes.
    //
    // EVIDENCE. Time-series momentum in crypto is documented, not just folklore: Liu &
    // Tsyvinski ("Risks and Returns of Cryptocurrency", RFS 2021) find current market
    // returns positively predict returns 1–8 weeks ahead for BTC/ETH; Liu, Tsyvinski & Wu
    // (2022) find a significant momentum factor in the crypto cross-section. The general
    // trend-following base rate is Moskowitz, Ooi & Pedersen (2012). None of this
    // guarantees the effect persists out of sample.
    //
    // ENTRY (rule-based only — no emotions, no prediction). On each CLOSED chart bar:
    //   * LONG when the fast EMA crosses ABOVE the slow EMA (fast reacts to recent price,
    //     slow represents the broader trend → cross = bullish momentum may be starting).
    //   * No short trades, ever. Flat is the bearish position.
    //   * Crossover only — being above the slow EMA is not an entry; the transition is.
    //
    // EXITS — "the exit defines the behaviour of the system". Three options, each
    // independently toggleable so backtests can isolate their effect:
    //   1. Opposite EMA cross — fast closes back below slow (momentum weakening).
    //   2. ATR initial stop  — `StopAtrMult`·ATR below entry. Volatility-scaled: wide when
    //      vol is high, tight when low. Always on: it also anchors position sizing.
    //   3. ATR trailing stop — chandelier from the highest high since entry, ratchets up
    //      on bar close, never loosens. Stays in strong trends, cuts exposure on reversal.
    //
    // RISK. Fixed-fractional sizing against the ATR stop; spread filter (as % of ATR —
    // crypto CFD pip conventions differ per broker, ATR-relative is symbol-agnostic);
    // daily-loss cap; consecutive-loss halt; max trades/day; max concurrent positions.
    //
    // MARKET NOTES. Crypto trades ~24/7 so there is deliberately no session filter; "daily"
    // risk counters reset at UTC midnight. Backtest with real spreads + commission; OHLC
    // bar mode is fine (all logic is bar-close), tick mode only sharpens stop fill fidelity.
    //
    // HONESTY NOTE. This is a research scaffold, not a validated edge. Long-only trend
    // systems on crypto live or die by a handful of large trends and endure long flat/chop
    // stretches where the cross whipsaws. Walk-forward test per instrument, then run the
    // unchanged config across ETH/SOL: if the behaviour does not repeat, the "edge" was fit.
    //
    // NOTE on types: helper types are prefixed `Ect...` so this bot stays self-contained
    // and never collides with the other single-file bots if compiled into one assembly.
    // =====================================================================================
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class EmaCrossTrendBot : Robot
    {
        #region Parameters - Signal

        [Parameter("Fast EMA Period", Group = "Signal", DefaultValue = 20, MinValue = 2, MaxValue = 200)]
        public int FastEmaPeriod { get; set; }      // reacts quicker to recent price

        [Parameter("Slow EMA Period", Group = "Signal", DefaultValue = 50, MinValue = 5, MaxValue = 500)]
        public int SlowEmaPeriod { get; set; }      // represents the broader trend

        [Parameter("ATR Period", Group = "Signal", DefaultValue = 14, MinValue = 5, MaxValue = 100)]
        public int AtrPeriod { get; set; }

        #endregion

        #region Parameters - Exits

        [Parameter("Exit On Opposite Cross", Group = "Exits", DefaultValue = true)]
        public bool UseCrossExit { get; set; }      // option 1: fast closes back below slow

        [Parameter("Stop ATR Multiplier", Group = "Exits", DefaultValue = 3.0, MinValue = 0.5, MaxValue = 10.0)]
        public double StopAtrMult { get; set; }     // option 2: initial volatility stop (always set)

        [Parameter("Use ATR Trailing Stop", Group = "Exits", DefaultValue = true)]
        public bool UseAtrTrail { get; set; }       // option 3: chandelier trail

        [Parameter("Trail ATR Multiplier", Group = "Exits", DefaultValue = 4.0, MinValue = 0.5, MaxValue = 15.0)]
        public double TrailAtrMult { get; set; }    // wider than the stop: let good trades breathe

        #endregion

        #region Parameters - Risk

        [Parameter("Risk Per Trade (%)", Group = "Risk", DefaultValue = 0.5, MinValue = 0.05, MaxValue = 3.0)]
        public double RiskPercent { get; set; }

        [Parameter("Max Spread (% of ATR)", Group = "Risk", DefaultValue = 10.0, MinValue = 0.0, MaxValue = 100.0)]
        public double MaxSpreadAtrPercent { get; set; }     // 0 = filter off

        [Parameter("Max Daily Loss (% of balance)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.5, MaxValue = 25.0)]
        public double MaxDailyLossPercent { get; set; }

        [Parameter("Max Consecutive Losses", Group = "Risk", DefaultValue = 5, MinValue = 1, MaxValue = 20)]
        public int MaxConsecutiveLosses { get; set; }       // halts entries for the rest of the UTC day

        [Parameter("Max Trades Per Day", Group = "Risk", DefaultValue = 2, MinValue = 1, MaxValue = 20)]
        public int MaxTradesPerDay { get; set; }            // crosses are rare; >2/day means chop

        [Parameter("Max Concurrent Positions", Group = "Risk", DefaultValue = 1, MinValue = 1, MaxValue = 3)]
        public int MaxPositions { get; set; }

        #endregion

        #region Parameters - Meta

        [Parameter("Trade Label", Group = "Meta", DefaultValue = "ECT")]
        public string TradeLabel { get; set; }

        [Parameter("Show Diagnostics", Group = "Meta", DefaultValue = true)]
        public bool ShowDiagnostics { get; set; }

        #endregion

        // --- Per-position bookkeeping (keyed by Position.Id) ---
        private sealed class EctPositionState
        {
            public double HighestSinceEntry;    // chandelier anchor
        }

        // --- State ---
        private ExponentialMovingAverage _emaFast;
        private ExponentialMovingAverage _emaSlow;
        private AverageTrueRange _atr;

        private readonly Dictionary<int, EctPositionState> _posState = new Dictionary<int, EctPositionState>();

        // Risk / day state (UTC day — crypto has no exchange close)
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
            if (FastEmaPeriod >= SlowEmaPeriod)
            {
                Print("Config error: fast EMA ({0}) must be shorter than slow EMA ({1}). Stopping.",
                    FastEmaPeriod, SlowEmaPeriod);
                Stop();
                return;
            }

            _emaFast = Indicators.ExponentialMovingAverage(Bars.ClosePrices, FastEmaPeriod);
            _emaSlow = Indicators.ExponentialMovingAverage(Bars.ClosePrices, SlowEmaPeriod);
            _atr = Indicators.AverageTrueRange(Bars, AtrPeriod, MovingAverageType.Exponential);

            Bars.BarClosed += OnBarClosed;
            Positions.Closed += OnPositionClosed;

            _currentDay = Server.Time.Date;

            Print("EmaCrossTrend started on {0} {1}. Fast={2} Slow={3} ATR={4} StopMult={5} Trail={6} CrossExit={7}",
                SymbolName, Bars.TimeFrame, FastEmaPeriod, SlowEmaPeriod, AtrPeriod,
                StopAtrMult, UseAtrTrail ? TrailAtrMult.ToString("F1") : "off", UseCrossExit);
        }

        protected override void OnStop()
        {
            Bars.BarClosed -= OnBarClosed;
            Positions.Closed -= OnPositionClosed;
        }

        // =====================================================================
        // Core: runs on every closed bar
        // =====================================================================

        private void OnBarClosed(BarClosedEventArgs e)
        {
            var bars = e.Bars;
            int idx = bars.Count - 2;   // index of the bar that just closed
            if (idx < SlowEmaPeriod + 1) return;

            // --- 1. New UTC day housekeeping ---
            var barDay = bars.OpenTimes[idx].Date;
            if (barDay != _currentDay)
            {
                _currentDay = barDay;
                _dailyRealizedPnl = 0;
                _dailyTradeCount = 0;
                _consecutiveLosses = 0;     // loss-streak halt is a per-day circuit breaker
                _haltPrintedToday = false;
            }

            // Cross state on the just-closed bar vs the bar before it.
            double fastNow = _emaFast.Result[idx];
            double slowNow = _emaSlow.Result[idx];
            double fastPrev = _emaFast.Result[idx - 1];
            double slowPrev = _emaSlow.Result[idx - 1];
            if (double.IsNaN(fastNow) || double.IsNaN(slowNow) ||
                double.IsNaN(fastPrev) || double.IsNaN(slowPrev))
                return;

            bool crossUp = fastPrev <= slowPrev && fastNow > slowNow;
            bool crossDown = fastPrev >= slowPrev && fastNow < slowNow;

            // --- 2. Manage open positions first (exit before entry on the same bar) ---
            ManageOpenPositions(bars, idx, crossDown);

            // --- 3. Entry: fast EMA crossed above slow EMA on this closed bar ---
            if (crossUp)
                TryEnterLong(bars, idx, fastNow, slowNow);
        }

        // =====================================================================
        // Entry
        // =====================================================================

        private void TryEnterLong(Bars bars, int idx, double fast, double slow)
        {
            if (!TradingGuardsPass()) return;

            double atr = _atr.Result[idx];
            if (double.IsNaN(atr) || atr <= 0) return;

            double stopPrice = StopAtrMult * atr;
            double stopPips = stopPrice / Symbol.PipSize;
            if (stopPips <= 0) return;

            double volume = CalculateVolume(stopPips);
            if (volume <= 0)
            {
                Print("Volume too small for risk {0}% with stop {1:F1} pips — skipping.", RiskPercent, stopPips);
                return;
            }

            var result = ExecuteMarketOrder(TradeType.Buy, SymbolName, volume, TradeLabel, stopPips, null);
            if (!result.IsSuccessful || result.Position == null)
            {
                Print("Order failed: {0}", result.Error);
                return;
            }

            var pos = result.Position;
            _posState[pos.Id] = new EctPositionState { HighestSinceEntry = pos.EntryPrice };
            _dailyTradeCount++;

            if (ShowDiagnostics)
                Print("ENTER LONG {0} @ {1} | EMA cross fast {2:F2} > slow {3:F2} | close {4} ATR {5:F2} stop {6:F1}p vol {7}",
                    SymbolName, pos.EntryPrice, fast, slow, bars.ClosePrices[idx], atr, stopPips, volume);
        }

        // Fixed-fractional: risk RiskPercent of balance against the ATR stop distance.
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
        // Position management (exits)
        // =====================================================================

        private void ManageOpenPositions(Bars bars, int idx, bool crossDown)
        {
            double atr = _atr.Result[idx];
            double barHigh = bars.HighPrices[idx];

            foreach (var pos in Positions.FindAll(TradeLabel, SymbolName))
            {
                // Exit option 1: opposite EMA cross — upside momentum may be weakening.
                if (UseCrossExit && crossDown)
                {
                    if (ShowDiagnostics) Print("CROSS EXIT {0} @ {1}", pos.Id, bars.ClosePrices[idx]);
                    ClosePosition(pos);
                    continue;
                }

                // Exit option 3: chandelier trail from the highest high since entry.
                if (!UseAtrTrail) continue;

                var st = GetOrRebuildState(pos);
                st.HighestSinceEntry = Math.Max(st.HighestSinceEntry, barHigh);

                if (double.IsNaN(atr) || atr <= 0) continue;
                double trailStop = Math.Round(st.HighestSinceEntry - TrailAtrMult * atr, Symbol.Digits);

                // Ratchet only: never loosen, and never place at/through the current bid.
                bool improves = pos.StopLoss == null || trailStop > pos.StopLoss.Value;
                bool valid = trailStop < Symbol.Bid;
                if (improves && valid)
                    ModifyPosition(pos, trailStop, pos.TakeProfit, ProtectionType.Absolute);
            }
        }

        // Rebuild state for positions we lost track of (e.g. bot restart on a live chart).
        private EctPositionState GetOrRebuildState(Position pos)
        {
            EctPositionState st;
            if (_posState.TryGetValue(pos.Id, out st)) return st;

            st = new EctPositionState { HighestSinceEntry = pos.EntryPrice };
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
        // Guards
        // =====================================================================

        private bool TradingGuardsPass()
        {
            if (Positions.FindAll(TradeLabel, SymbolName).Length >= MaxPositions) return false;
            if (_dailyTradeCount >= MaxTradesPerDay) return Halt("max trades/day reached");
            if (_consecutiveLosses >= MaxConsecutiveLosses) return Halt("consecutive-loss halt");

            double dailyCap = Account.Balance * MaxDailyLossPercent / 100.0;
            if (_dailyRealizedPnl <= -dailyCap) return Halt("daily-loss cap hit");

            // Spread filter relative to ATR: crypto CFD pip conventions vary per broker,
            // so an absolute pip threshold does not transfer across BTC/ETH/SOL.
            if (MaxSpreadAtrPercent > 0)
            {
                double atr = _atr.Result.LastValue;
                if (!double.IsNaN(atr) && atr > 0 && Symbol.Spread > atr * MaxSpreadAtrPercent / 100.0)
                {
                    if (ShowDiagnostics)
                        Print("Entry skipped: spread {0} > {1}% of ATR {2:F2}", Symbol.Spread, MaxSpreadAtrPercent, atr);
                    return false;
                }
            }

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
    }
}
