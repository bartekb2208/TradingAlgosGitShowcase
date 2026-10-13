using System;
using System.Collections.Generic;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    // =====================================================================================
    // Band-Fade Mean-Reversion cBot  —  two-sided z-score fade on range-bound crosses
    // Designed for AUDNZD H1 (EURGBP / EURCHF as cross-market tests).      —  BACKTESTABLE
    // -------------------------------------------------------------------------------------
    // ORIGIN. Reconstruction exercise: reverse-engineered profile of one strategy line in
    // a QuantAnalyzer-style multi-strategy portfolio (per-line ≈ 40 trades/yr, ~52% win,
    // avg win ≈ 1.4× avg loss, ~0.15% risk/trade, regime-indifferent near-linear equity,
    // soft patch only in strong one-way markets like 2022). That fingerprint is short-hold
    // swing MEAN REVERSION, not trend-following.
    //
    // HYPOTHESIS. FX mean reversion is weak in the majors at short horizons (weekly returns
    // show cross-sectional MOMENTUM — Raza et al. 2014, JIMF), but is exploitable on
    // closely-linked CROSSES whose economies share shocks: AUD/NZD (twin commodity
    // exporters, correlated terms-of-trade, RBA/RBNZ co-movement) and EUR/GBP. The cross
    // nets out the common USD factor, leaving a slow-moving relative-value spread that
    // ranges far more than it trends (practitioner literature: Quantpedia "How to Build
    // Mean Reversion Strategies in Currencies", Beluská & Vojtko 2024; Robot Wealth
    // cointegration studies). Academic anchors: Lehmann 1990 / Jegadeesh 1990 short-term
    // reversal; Lo & MacKinlay 1990 contrarian profits; Serban 2010 (JBF) — FX strategies
    // combining mean reversion with a momentum overlay outperform their equity analogues.
    // Reversion premium interpretation: fading a 2σ+ stretch supplies liquidity /
    // absorbs inventory imbalance after crowded short-horizon flows.
    //
    // MECHANICS (all decisions on closed chart-TF bars → OHLC backtest OK; tick data only
    // sharpens hard-stop fidelity):
    //   * STRETCH  — z = (close − SMA(BandPeriod)) / StdDev(BandPeriod); |z| ≥ EntryZ arms
    //                a fade in the opposite direction.
    //   * EXHAUST  — optional RSI(2) confluence (Connors): oversold ≤ 10 for longs,
    //                overbought ≥ 90 for shorts. Filters "stretch that is still running".
    //   * REGIME VETO — the mirror image of the trend bots' chop filter: NO entries while
    //                the higher-TF ADX says the cross is trending (last CLOSED regime bar,
    //                no peeking). MR blows up when a range breaks into trend; standing
    //                down in trends is why the reconstructed lines went flat-not-red in
    //                2022-style one-way markets.
    //   * EXITS    — (1) mean touch: close crosses back through the band mid → take it
    //                (this is the ~52%-win pocket; win size ≈ entry stretch ≈ 2σ);
    //                (2) hard stop: StopAtrMult·ATR — the range-break disaster stop;
    //                (3) time stop: MR alpha decays fast (O-U half-life); stale trades are
    //                dead capital and adverse selection. Time-stopped exits are partial
    //                wins/losses — they are what pulls avg loss BELOW the hard stop and
    //                pushes the payoff ratio toward the target 1.4.
    //   * No breakeven, no trail, no partials: reversion trades either work back to the
    //     mean or they don't; trailing a reversion is a category error.
    //
    // NEWS. Deliberately NO news filter: the UJ campaign measured news avoidance costing
    // a third to half of a swing edge, and MR entries are risk-capped by the hard stop.
    // Rollover blackout kept (spread blowout would trip entries on stale quotes).
    //
    // PRE-REGISTERED CAMPAIGN PROTOCOL (write results to memory, no forking paths):
    //   * IS window Apr 2018 – Dec 2022, OOS Jan 2023 – now, untouched until frozen.
    //   * Envelope targets (from the deciphered line, at 0.15% risk): 30–60 trades/yr,
    //     win 48–58%, avg win / avg loss 1.1–1.6, PF 1.2–1.6, profitable ≥ 6 of 8 years,
    //     max balance DD ≤ 2.5%, strong-trend years flat-not-red.
    //   * Falsifier: IS PF < 1.05 at neutral defaults → the family is dead on this pair;
    //     abandon, do NOT rescue with a parameter sweep (Overnight-Drift lesson).
    //   * Cross-market: frozen config must stay PF > 1.0 on EURGBP unseen.
    //
    // HONESTY NOTE. Research scaffold, not a validated edge. Known failure mode: regime
    // break — a structural repricing of the cross (policy divergence, e.g. RBNZ/RBA split
    // or Brexit-style shock on EURGBP) turns every fade into a knife-catch; the ADX veto
    // and hard stop bound each hit to 1R but a fast break can eat several R before the
    // veto engages. Size for a losing streak of 8–10.
    //
    // NOTE on types: helpers are prefixed `Mrb...` so this bot stays self-contained and
    // never collides with the other single-file bots if compiled into one assembly.
    // =====================================================================================
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class MeanRevBandFadeBot : Robot
    {
        #region Parameters - Signal

        [Parameter("Band Period (SMA/StdDev)", Group = "Signal", DefaultValue = 20, MinValue = 5, MaxValue = 200)]
        public int BandPeriod { get; set; }

        [Parameter("Entry Z-Score", Group = "Signal", DefaultValue = 2.0, MinValue = 0.5, MaxValue = 5.0)]
        public double EntryZ { get; set; }

        [Parameter("Use RSI(2) Confluence", Group = "Signal", DefaultValue = true)]
        public bool UseRsiConfluence { get; set; }

        [Parameter("RSI Period", Group = "Signal", DefaultValue = 2, MinValue = 2, MaxValue = 14)]
        public int RsiPeriod { get; set; }

        [Parameter("RSI Oversold (long)", Group = "Signal", DefaultValue = 10, MinValue = 1, MaxValue = 40)]
        public int RsiOversold { get; set; }

        [Parameter("RSI Overbought (short)", Group = "Signal", DefaultValue = 90, MinValue = 60, MaxValue = 99)]
        public int RsiOverbought { get; set; }

        [Parameter("Allow Longs", Group = "Signal", DefaultValue = true)]
        public bool AllowLong { get; set; }

        [Parameter("Allow Shorts", Group = "Signal", DefaultValue = true)]
        public bool AllowShort { get; set; }

        [Parameter("ATR Period", Group = "Signal", DefaultValue = 14, MinValue = 5, MaxValue = 100)]
        public int AtrPeriod { get; set; }

        #endregion

        #region Parameters - Regime veto

        [Parameter("Use Regime Veto", Group = "Regime", DefaultValue = true)]
        public bool UseRegimeVeto { get; set; }

        [Parameter("Regime Timeframe", Group = "Regime", DefaultValue = "Hour4")]
        public TimeFrame RegimeTimeframe { get; set; }

        [Parameter("ADX Period", Group = "Regime", DefaultValue = 14, MinValue = 5, MaxValue = 50)]
        public int AdxPeriod { get; set; }

        [Parameter("Max ADX (no-trend gate)", Group = "Regime", DefaultValue = 25, MinValue = 10, MaxValue = 50)]
        public double MaxAdx { get; set; }

        #endregion

        #region Parameters - Exits

        [Parameter("Stop ATR Multiplier", Group = "Exits", DefaultValue = 2.5, MinValue = 0.5, MaxValue = 8.0)]
        public double StopAtrMult { get; set; }

        [Parameter("Exit At Mean Touch", Group = "Exits", DefaultValue = true)]
        public bool MeanExit { get; set; }

        [Parameter("Time Stop (bars, 0=off)", Group = "Exits", DefaultValue = 48, MinValue = 0, MaxValue = 500)]
        public int TimeStopBars { get; set; }

        #endregion

        #region Parameters - Session (UTC)

        [Parameter("Use Rollover Blackout", Group = "Session", DefaultValue = true)]
        public bool UseRolloverBlackout { get; set; }

        [Parameter("Rollover Start (UTC h:mm)", Group = "Session", DefaultValue = "21:00")]
        public string RolloverStart { get; set; }

        [Parameter("Rollover End (UTC h:mm)", Group = "Session", DefaultValue = "22:15")]
        public string RolloverEnd { get; set; }

        #endregion

        #region Parameters - Risk

        [Parameter("Risk Per Trade (%)", Group = "Risk", DefaultValue = 0.15, MinValue = 0.02, MaxValue = 2.0)]
        public double RiskPercent { get; set; }

        [Parameter("Max Spread (pips)", Group = "Risk", DefaultValue = 2.5, MinValue = 0.0, MaxValue = 20.0)]
        public double MaxSpreadPips { get; set; }

        [Parameter("Max Daily Loss (% of balance)", Group = "Risk", DefaultValue = 2.0, MinValue = 0.5, MaxValue = 25.0)]
        public double MaxDailyLossPercent { get; set; }

        [Parameter("Max Consecutive Losses", Group = "Risk", DefaultValue = 6, MinValue = 1, MaxValue = 20)]
        public int MaxConsecutiveLosses { get; set; }

        [Parameter("Max Trades Per Day", Group = "Risk", DefaultValue = 3, MinValue = 1, MaxValue = 20)]
        public int MaxTradesPerDay { get; set; }

        [Parameter("Max Concurrent Positions", Group = "Risk", DefaultValue = 1, MinValue = 1, MaxValue = 4)]
        public int MaxPositions { get; set; }

        #endregion

        #region Parameters - Meta

        [Parameter("Trade Label", Group = "Meta", DefaultValue = "MRB")]
        public string TradeLabel { get; set; }

        [Parameter("Show Diagnostics", Group = "Meta", DefaultValue = true)]
        public bool ShowDiagnostics { get; set; }

        #endregion

        // --- State ---
        private SimpleMovingAverage _sma;
        private StandardDeviation _std;
        private RelativeStrengthIndex _rsi;
        private AverageTrueRange _atr;
        private DirectionalMovementSystem _dms;   // on regime-TF bars
        private Bars _regimeBars;

        private TimeSpan _rollStart, _rollEnd;
        private readonly Dictionary<int, int> _entryBarByPos = new Dictionary<int, int>();

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
            if (UseRsiConfluence && RsiOversold >= RsiOverbought)
            {
                Print("Config error: RSI oversold {0} must be below overbought {1}. Stopping.", RsiOversold, RsiOverbought);
                Stop();
                return;
            }

            _sma = Indicators.SimpleMovingAverage(Bars.ClosePrices, BandPeriod);
            _std = Indicators.StandardDeviation(Bars.ClosePrices, BandPeriod, MovingAverageType.Simple);
            _rsi = Indicators.RelativeStrengthIndex(Bars.ClosePrices, RsiPeriod);
            _atr = Indicators.AverageTrueRange(Bars, AtrPeriod, MovingAverageType.Exponential);

            _regimeBars = MarketData.GetBars(RegimeTimeframe);
            _dms = Indicators.DirectionalMovementSystem(_regimeBars, AdxPeriod);

            _rollStart = MrbParseTime(RolloverStart, new TimeSpan(21, 0, 0));
            _rollEnd = MrbParseTime(RolloverEnd, new TimeSpan(22, 15, 0));

            Bars.BarClosed += OnBarClosed;
            Positions.Closed += OnPositionClosed;

            _currentDay = Server.Time.Date;

            Print("MeanRevBandFade started on {0} {1}. Band={2} z={3} RSI({4}) {5}/{6} veto=ADX({7})<{8}@{9} stop={10}xATR timeStop={11} risk={12}%",
                SymbolName, Bars.TimeFrame, BandPeriod, EntryZ, RsiPeriod,
                UseRsiConfluence ? RsiOversold.ToString() : "off", UseRsiConfluence ? RsiOverbought.ToString() : "off",
                AdxPeriod, MaxAdx, RegimeTimeframe, StopAtrMult, TimeStopBars, RiskPercent);
        }

        protected override void OnStop()
        {
            Bars.BarClosed -= OnBarClosed;
            Positions.Closed -= OnPositionClosed;
        }

        // =====================================================================
        // Core: closed-bar logic
        // =====================================================================

        private void OnBarClosed(BarClosedEventArgs e)
        {
            var bars = e.Bars;
            int idx = bars.Count - 2;
            if (idx < BandPeriod + 1 || idx < AtrPeriod + 1) return;

            var barDay = bars.OpenTimes[idx].Date;
            if (barDay != _currentDay)
            {
                _currentDay = barDay;
                _dailyRealizedPnl = 0;
                _dailyTradeCount = 0;
                _consecutiveLosses = 0;
                _haltPrintedToday = false;
            }

            ManageExits(bars, idx);
            TrySignal(bars, idx);
        }

        private void TrySignal(Bars bars, int idx)
        {
            double close = bars.ClosePrices[idx];
            double mid = _sma.Result[idx];
            double sd = _std.Result[idx];
            if (double.IsNaN(mid) || double.IsNaN(sd) || sd <= 0) return;

            double z = (close - mid) / sd;
            double rsi = _rsi.Result[idx];

            bool longSig = AllowLong && z <= -EntryZ && (!UseRsiConfluence || rsi <= RsiOversold);
            bool shortSig = AllowShort && z >= EntryZ && (!UseRsiConfluence || rsi >= RsiOverbought);
            if (!longSig && !shortSig) return;

            if (UseRegimeVeto && RegimeIsTrending(bars.OpenTimes[idx])) return;
            if (!TradingGuardsPass()) return;
            if (UseRolloverBlackout && InRollover(Server.Time)) return;

            Execute(longSig ? TradeType.Buy : TradeType.Sell, bars, idx, z);
        }

        private bool RegimeIsTrending(DateTime chartBarOpenUtc)
        {
            int rIdx = _regimeBars.OpenTimes.GetIndexByTime(chartBarOpenUtc);
            // rIdx is the (possibly still-forming) regime bar; judge on the last CLOSED one
            // so OHLC backtests cannot peek inside an unfinished higher-TF bar.
            if (rIdx < 1) return true;
            double adx = _dms.ADX[rIdx - 1];
            return double.IsNaN(adx) || adx >= MaxAdx;
        }

        private void Execute(TradeType side, Bars bars, int idx, double z)
        {
            double atr = _atr.Result[idx];
            if (double.IsNaN(atr) || atr <= 0) return;

            double stopPips = StopAtrMult * atr / Symbol.PipSize;
            if (stopPips <= 0) return;

            double volume = CalculateVolume(stopPips);
            if (volume <= 0)
            {
                Print("Volume too small for risk {0}% with stop {1:F1} pips — skipping.", RiskPercent, stopPips);
                return;
            }

            var result = ExecuteMarketOrder(side, SymbolName, volume, TradeLabel, stopPips, null);
            if (!result.IsSuccessful || result.Position == null)
            {
                Print("Order failed: {0}", result.Error);
                return;
            }

            _entryBarByPos[result.Position.Id] = idx;
            _dailyTradeCount++;

            if (ShowDiagnostics)
                Print("FADE {0} {1} @ {2} | z {3:F2} rsi {4:F1} | ATR {5:F5} stop {6:F1}p vol {7}",
                    side, SymbolName, result.Position.EntryPrice, z, _rsi.Result[idx], atr, stopPips, volume);
        }

        private double CalculateVolume(double stopPips)
        {
            double riskAmount = Account.Balance * RiskPercent / 100.0;
            double perUnitRisk = stopPips * Symbol.PipValue;
            if (perUnitRisk <= 0) return 0;

            double raw = riskAmount / perUnitRisk;
            double volume = Symbol.NormalizeVolumeInUnits(raw, RoundingMode.Down);
            if (volume < Symbol.VolumeInUnitsMin) return 0;
            return Math.Min(volume, Symbol.VolumeInUnitsMax);
        }

        // =====================================================================
        // Exits (bar close): mean touch, time stop. Hard stop lives on the order.
        // =====================================================================

        private void ManageExits(Bars bars, int idx)
        {
            double close = bars.ClosePrices[idx];
            double mid = _sma.Result[idx];
            if (double.IsNaN(mid)) return;

            foreach (var pos in Positions.FindAll(TradeLabel, SymbolName))
            {
                int entryIdx;
                if (!_entryBarByPos.TryGetValue(pos.Id, out entryIdx))
                {
                    // restart recovery: locate the entry bar from the position's timestamp
                    entryIdx = bars.OpenTimes.GetIndexByTime(pos.EntryTime);
                    _entryBarByPos[pos.Id] = entryIdx;
                }

                string reason = null;
                if (MeanExit &&
                    ((pos.TradeType == TradeType.Buy && close >= mid) ||
                     (pos.TradeType == TradeType.Sell && close <= mid)))
                    reason = "mean touch";
                else if (TimeStopBars > 0 && idx - entryIdx >= TimeStopBars)
                    reason = "time stop";

                if (reason == null) continue;
                var r = ClosePosition(pos);
                if (r.IsSuccessful && ShowDiagnostics)
                    Print("EXIT {0} ({1}) after {2} bars, net {3:F2}", pos.Id, reason, idx - entryIdx, pos.NetProfit);
            }
        }

        private void OnPositionClosed(PositionClosedEventArgs args)
        {
            var pos = args.Position;
            if (pos.Label != TradeLabel || pos.SymbolName != SymbolName) return;

            _dailyRealizedPnl += pos.NetProfit;
            if (pos.NetProfit < 0) _consecutiveLosses++;
            else if (pos.NetProfit > 0) _consecutiveLosses = 0;

            bool stillOpen = false;
            foreach (var p in Positions.FindAll(TradeLabel, SymbolName))
                if (p.Id == pos.Id) { stillOpen = true; break; }
            if (!stillOpen) _entryBarByPos.Remove(pos.Id);

            if (ShowDiagnostics)
                Print("CLOSED {0} net {1:F2} | day PnL {2:F2} | streak {3} losses",
                    pos.Id, pos.NetProfit, _dailyRealizedPnl, _consecutiveLosses);
        }

        // =====================================================================
        // Guards / time windows
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

        private bool InRollover(DateTime utc)
        {
            var t = utc.TimeOfDay;
            if (_rollStart <= _rollEnd) return t >= _rollStart && t < _rollEnd;
            return t >= _rollStart || t < _rollEnd;   // wraps midnight
        }

        private static TimeSpan MrbParseTime(string s, TimeSpan fallback)
        {
            TimeSpan t;
            return TimeSpan.TryParse(s.Trim(), out t) ? t : fallback;
        }
    }
}
