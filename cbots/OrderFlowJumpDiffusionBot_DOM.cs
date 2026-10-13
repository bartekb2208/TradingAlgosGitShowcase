using System;
using System.Collections.Generic;
using System.Linq;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    // =====================================================================================
    // Order-Flow + Jump-Diffusion cBot  —  DOM (depth-of-market imbalance) variant  —  LIVE ONLY
    // Designed for an index / commodity CFD (US500, NAS100, XAUUSD, ...) on M5.
    // -------------------------------------------------------------------------------------
    // IMPORTANT: MarketData.GetMarketDepth(...) returns NO data in the cTrader backtester.
    // This bot therefore CANNOT be backtested meaningfully — run it on a DEMO account in
    // real time / forward test. For a backtestable equivalent use the CVD-variant file.
    //
    // Same two-layer design as the CVD variant, but the ORDER-FLOW layer is replaced:
    //
    //   JUMP-DIFFUSION layer (identical): Merton model on M5 log returns; online jumps via
    //   the Lee-Mykland (2008) statistic using jump-robust bipower local volatility; rolling
    //   re-estimation of sigma, lambda, muJ, sigmaJ.
    //
    //   ORDER-FLOW layer (DOM imbalance): on each depth update we sum the top-N bid volumes
    //   and top-N ask volumes from the live order book and form a normalised imbalance
    //   I = (bidVol - askVol) / (bidVol + askVol) in [-1, +1]. I is smoothed (EMA) to reduce
    //   book flicker. I > 0 => resting buy pressure (bids stacked), I < 0 => sell pressure.
    //
    // COMBINATION on each closed M5 bar:
    //   * Continuation : trade IN the jump direction when imbalance agrees in sign.
    //   * Reversal     : fade the jump when imbalance opposes it (book leaning the other way).
    //
    // RISK identical to the CVD variant. Helper types are prefixed `Dom...` to stay collision
    // free if both bot files compile into the same assembly.
    // =====================================================================================
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class OrderFlowJumpDomBot : Robot
    {
        #region Parameters - Jump-Diffusion

        [Parameter("Estimation Window (bars)", Group = "Jump-Diffusion", DefaultValue = 288, MinValue = 60, MaxValue = 2000)]
        public int EstimationWindow { get; set; }

        [Parameter("Lee-Mykland Local Window (bars)", Group = "Jump-Diffusion", DefaultValue = 60, MinValue = 16, MaxValue = 300)]
        public int LocalWindow { get; set; }

        [Parameter("Jump Confidence (%)", Group = "Jump-Diffusion", DefaultValue = 99.0, MinValue = 90.0, MaxValue = 99.99)]
        public double JumpConfidence { get; set; }

        [Parameter("Min Jump Sigma (fallback)", Group = "Jump-Diffusion", DefaultValue = 4.0, MinValue = 2.0, MaxValue = 10.0)]
        public double MinJumpSigma { get; set; }

        #endregion

        #region Parameters - Order Flow (DOM)

        [Parameter("DOM Levels", Group = "Order Flow", DefaultValue = 5, MinValue = 1, MaxValue = 50)]
        public int DomLevels { get; set; }              // top-N price levels each side

        [Parameter("Imbalance Smoothing (0-0.99)", Group = "Order Flow", DefaultValue = 0.7, MinValue = 0.0, MaxValue = 0.99)]
        public double ImbalanceSmoothing { get; set; }  // EMA weight on the previous imbalance

        [Parameter("Min |Imbalance| To Act", Group = "Order Flow", DefaultValue = 0.15, MinValue = 0.0, MaxValue = 1.0)]
        public double MinImbalance { get; set; }

        #endregion

        #region Parameters - Strategy

        [Parameter("Trade Mode", Group = "Strategy", DefaultValue = DomTradeMode.Continuation)]
        public DomTradeMode Mode { get; set; }

        [Parameter("Require Order-Flow Agreement", Group = "Strategy", DefaultValue = true)]
        public bool RequireFlowAgreement { get; set; }

        [Parameter("Allow Longs", Group = "Strategy", DefaultValue = true)]
        public bool AllowLongs { get; set; }

        [Parameter("Allow Shorts", Group = "Strategy", DefaultValue = true)]
        public bool AllowShorts { get; set; }

        #endregion

        #region Parameters - Session Filter (UTC)

        [Parameter("Use Session Filter", Group = "Session", DefaultValue = true)]
        public bool UseSessionFilter { get; set; }

        [Parameter("Session Start Hour (UTC)", Group = "Session", DefaultValue = 13, MinValue = 0, MaxValue = 23)]
        public int SessionStartHour { get; set; }

        [Parameter("Session End Hour (UTC)", Group = "Session", DefaultValue = 20, MinValue = 0, MaxValue = 24)]
        public int SessionEndHour { get; set; }

        #endregion

        #region Parameters - Regime Filter

        [Parameter("Use Regime Filter", Group = "Regime", DefaultValue = true)]
        public bool UseRegimeFilter { get; set; }

        [Parameter("Regime Timeframe", Group = "Regime", DefaultValue = "Hour1")]
        public TimeFrame RegimeTimeFrame { get; set; }

        [Parameter("Regime EMA Period", Group = "Regime", DefaultValue = 50, MinValue = 5, MaxValue = 400)]
        public int RegimeMaPeriod { get; set; }

        #endregion

        #region Parameters - Exits

        [Parameter("Use Breakeven Stop", Group = "Exits", DefaultValue = true)]
        public bool UseBreakeven { get; set; }

        [Parameter("Breakeven Trigger (R)", Group = "Exits", DefaultValue = 0.5, MinValue = 0.1, MaxValue = 3.0)]
        public double BreakevenTriggerR { get; set; }

        [Parameter("Use Time Stop", Group = "Exits", DefaultValue = true)]
        public bool UseTimeStop { get; set; }

        [Parameter("Max Bars In Trade", Group = "Exits", DefaultValue = 12, MinValue = 1, MaxValue = 500)]
        public int MaxBarsInTrade { get; set; }

        #endregion

        #region Parameters - Risk

        [Parameter("Risk Per Trade (%)", Group = "Risk", DefaultValue = 0.5, MinValue = 0.05, MaxValue = 3.0)]
        public double RiskPercent { get; set; }

        [Parameter("Stop ATR Multiplier", Group = "Risk", DefaultValue = 1.5, MinValue = 0.3, MaxValue = 6.0)]
        public double StopAtrMult { get; set; }

        [Parameter("Stop Jump-Sigma Multiplier", Group = "Risk", DefaultValue = 1.0, MinValue = 0.0, MaxValue = 6.0)]
        public double StopJumpSigmaMult { get; set; }

        [Parameter("Risk:Reward", Group = "Risk", DefaultValue = 2.0, MinValue = 0.5, MaxValue = 10.0)]
        public double RiskReward { get; set; }

        [Parameter("ATR Period", Group = "Risk", DefaultValue = 14, MinValue = 5, MaxValue = 50)]
        public int AtrPeriod { get; set; }

        [Parameter("Max Spread (pips)", Group = "Risk", DefaultValue = 2.0, MinValue = 0.0, MaxValue = 100.0)]
        public double MaxSpreadPips { get; set; }

        [Parameter("Max Daily Loss (% of balance)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.5, MaxValue = 25.0)]
        public double MaxDailyLossPercent { get; set; }

        [Parameter("Max Consecutive Losses", Group = "Risk", DefaultValue = 3, MinValue = 1, MaxValue = 20)]
        public int MaxConsecutiveLosses { get; set; }

        [Parameter("Max Trades Per Day", Group = "Risk", DefaultValue = 8, MinValue = 1, MaxValue = 100)]
        public int MaxTradesPerDay { get; set; }

        [Parameter("Max Concurrent Positions", Group = "Risk", DefaultValue = 1, MinValue = 1, MaxValue = 5)]
        public int MaxPositions { get; set; }

        #endregion

        #region Parameters - Meta

        [Parameter("Trade Label", Group = "Meta", DefaultValue = "OF_JD_DOM")]
        public string TradeLabel { get; set; }

        [Parameter("Show Diagnostics", Group = "Meta", DefaultValue = true)]
        public bool ShowDiagnostics { get; set; }

        #endregion

        // --- State ---
        private AverageTrueRange _atr;
        private ExponentialMovingAverage _regimeMa;
        private Bars _regimeBars;
        private MarketDepth _depth;
        private double _imbalance;          // smoothed imbalance in [-1, +1]
        private bool _haveImbalance;

        // Jump-diffusion state
        private readonly List<double> _logReturns = new List<double>();
        private double _prevClose = double.NaN;
        private DomJumpEstimate _model = DomJumpEstimate.Empty;

        // Per-position bookkeeping (keyed by Position.Id)
        private readonly Dictionary<int, double> _initRiskPrice = new Dictionary<int, double>();
        private readonly Dictionary<int, int> _entryBarIndex = new Dictionary<int, int>();

        // Risk / session state
        private DateTime _currentDay = DateTime.MinValue;
        private double _dailyRealizedPnl;
        private int _dailyTradeCount;
        private int _consecutiveLosses;

        // =====================================================================
        // Lifecycle
        // =====================================================================

        protected override void OnStart()
        {
            if (LocalWindow >= EstimationWindow)
            {
                Print("Local window must be smaller than estimation window. Aborting.");
                Stop();
                return;
            }
            if (IsBacktesting)
                Print("WARNING: DOM data is unavailable in the backtester. This bot will not generate order-flow signals here. Use the CVD variant to backtest.");

            _atr = Indicators.AverageTrueRange(Bars, AtrPeriod, MovingAverageType.Exponential);
            _regimeBars = MarketData.GetBars(RegimeTimeFrame);
            _regimeMa = Indicators.ExponentialMovingAverage(_regimeBars.ClosePrices, RegimeMaPeriod);

            _depth = MarketData.GetMarketDepth(SymbolName);
            _depth.Updated += OnDepthUpdated;

            Bars.BarClosed += OnBarClosed;
            Positions.Closed += OnPositionClosed;

            _currentDay = Server.Time.Date;
            SeedFromHistory();

            Print("OrderFlow+JumpDiffusion (DOM) started on {0} {1}. Levels={2}", SymbolName, Bars.TimeFrame, DomLevels);
            Print("Initial model: {0}", _model);
        }

        protected override void OnStop()
        {
            if (_depth != null) _depth.Updated -= OnDepthUpdated;
            Bars.BarClosed -= OnBarClosed;
            Positions.Closed -= OnPositionClosed;
        }

        protected override void OnTick()
        {
            if (UseBreakeven)
                ManageBreakeven();
        }

        private void SeedFromHistory()
        {
            int n = Bars.ClosePrices.Count;
            int start = Math.Max(1, n - EstimationWindow - 1);
            _prevClose = Bars.ClosePrices[start - 1];
            for (int i = start; i < n; i++)
            {
                double c = Bars.ClosePrices[i];
                if (c > 0 && _prevClose > 0)
                    PushReturn(Math.Log(c / _prevClose));
                _prevClose = c;
            }
            _model = DomJumpEstimate.Estimate(_logReturns, LocalWindow, JumpConfidence, MinJumpSigma);
        }

        // =====================================================================
        // Order flow — DOM imbalance
        // =====================================================================

        private void OnDepthUpdated()
        {
            double bidVol = SumTop(_depth.BidEntries, DomLevels);
            double askVol = SumTop(_depth.AskEntries, DomLevels);
            double total = bidVol + askVol;
            if (total <= 0) return;

            double raw = (bidVol - askVol) / total;   // +1 all bids, -1 all asks
            _imbalance = _haveImbalance
                ? ImbalanceSmoothing * _imbalance + (1.0 - ImbalanceSmoothing) * raw
                : raw;
            _haveImbalance = true;
        }

        private static double SumTop(IEnumerable<MarketDepthEntry> entries, int levels)
        {
            double s = 0;
            int i = 0;
            foreach (var e in entries)
            {
                if (i++ >= levels) break;
                s += e.Volume;
            }
            return s;
        }

        // =====================================================================
        // Core: runs on every closed M5 bar
        // =====================================================================

        private void OnBarClosed(BarClosedEventArgs e)
        {
            var bars = e.Bars;
            int idx = bars.Count - 2;
            if (idx < 1) return;

            var barDay = bars.OpenTimes[idx].Date;
            if (barDay != _currentDay)
            {
                _currentDay = barDay;
                _dailyRealizedPnl = 0;
                _dailyTradeCount = 0;
            }

            double close = bars.ClosePrices[idx];
            double ret = double.NaN;
            if (!double.IsNaN(_prevClose) && _prevClose > 0 && close > 0)
            {
                ret = Math.Log(close / _prevClose);
                PushReturn(ret);
            }
            _prevClose = close;
            _model = DomJumpEstimate.Estimate(_logReturns, LocalWindow, JumpConfidence, MinJumpSigma);

            if (UseTimeStop)
                ManageTimeStop();

            if (ShowDiagnostics && !double.IsNaN(ret))
                DrawDiagnostics(bars, idx, ret);

            if (!double.IsNaN(ret))
                TryTrade(ret);
        }

        // =====================================================================
        // Chart diagnostics
        // =====================================================================

        private void DrawDiagnostics(Bars bars, int idx, double ret)
        {
            string txt = string.Format(
                "OF+JD (DOM)\nsigma {0:P2}/bar\nlambda {1:F3}/bar\nmuJ {2:P2}  sigmaJ {3:P2}\ncrit {4:F2}  |last| {5:F2}\nimbalance {6:F3}",
                _model.SigmaDiffusion, _model.Lambda, _model.MuJump, _model.SigmaJump,
                _model.CriticalValue, _model.SigmaLocal > 0 ? Math.Abs(ret) / _model.SigmaLocal : 0,
                _imbalance);
            Chart.DrawStaticText("of_jd_dom_stats", txt, VerticalAlignment.Top, HorizontalAlignment.Left, Color.White);

            if (_model.IsJump(ret))
            {
                var time = bars.OpenTimes[idx];
                string name = "jump_" + time.Ticks;
                if (ret > 0)
                    Chart.DrawIcon(name, ChartIconType.UpArrow, time, bars.LowPrices[idx], Color.Lime);
                else
                    Chart.DrawIcon(name, ChartIconType.DownArrow, time, bars.HighPrices[idx], Color.Red);
            }
        }

        // =====================================================================
        // Signal + execution
        // =====================================================================

        private void TryTrade(double lastReturn)
        {
            if (!_model.IsReady) return;
            if (!_haveImbalance) return;          // no live book yet
            if (!TradingGuardsPass()) return;
            if (UseSessionFilter && !InSession(Server.Time)) return;

            if (!_model.IsJump(lastReturn)) return;
            int jumpDir = Math.Sign(lastReturn);
            if (jumpDir == 0) return;

            int flowDir = Math.Abs(_imbalance) >= MinImbalance ? Math.Sign(_imbalance) : 0;

            int tradeDir;
            if (Mode == DomTradeMode.Continuation)
            {
                if (RequireFlowAgreement && flowDir != jumpDir) return;
                tradeDir = jumpDir;
            }
            else
            {
                if (RequireFlowAgreement && flowDir == jumpDir) return;
                tradeDir = -jumpDir;
            }

            if (UseRegimeFilter)
            {
                int regime = RegimeDirection();
                if (regime != 0 && tradeDir != regime) return;
            }

            if (tradeDir > 0 && !AllowLongs) return;
            if (tradeDir < 0 && !AllowShorts) return;

            Execute(tradeDir, lastReturn);
        }

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

        private bool InSession(DateTime utc)
        {
            int h = utc.Hour;
            if (SessionStartHour == SessionEndHour) return true;
            if (SessionStartHour < SessionEndHour)
                return h >= SessionStartHour && h < SessionEndHour;
            return h >= SessionStartHour || h < SessionEndHour;
        }

        private void Execute(int dir, double lastReturn)
        {
            var tradeType = dir > 0 ? TradeType.Buy : TradeType.Sell;

            double atr = _atr.Result.LastValue;
            double jumpStopPrice = StopJumpSigmaMult * _model.ExpectedJumpMovePrice(Symbol.Bid);
            double stopPrice = Math.Max(StopAtrMult * atr, jumpStopPrice);
            if (stopPrice <= 0 || double.IsNaN(stopPrice)) return;

            double stopPips = stopPrice / Symbol.PipSize;
            double tpPips = stopPips * RiskReward;
            if (stopPips <= 0) return;

            double volume = CalculateVolume(stopPips);
            if (volume <= 0) return;

            var result = ExecuteMarketOrder(tradeType, SymbolName, volume, TradeLabel, stopPips, tpPips);
            if (result.IsSuccessful)
            {
                _dailyTradeCount++;
                int id = result.Position.Id;
                _initRiskPrice[id] = stopPips * Symbol.PipSize;
                _entryBarIndex[id] = Bars.Count;
                Print("{0} {1} @ {2:F2} | jumpRet={3:P3} imbalance={4:F3} lambda={5:F3} sigmaJ={6:P3} SL={7:F1}p TP={8:F1}p vol={9}",
                    Mode, tradeType, result.Position.EntryPrice, lastReturn, _imbalance,
                    _model.Lambda, _model.SigmaJump, stopPips, tpPips, volume);
            }
            else
            {
                Print("Order rejected: {0}", result.Error);
            }
        }

        // =====================================================================
        // Risk helpers
        // =====================================================================

        private bool TradingGuardsPass()
        {
            if (CountOwnPositions() >= MaxPositions) return false;
            if (_dailyTradeCount >= MaxTradesPerDay) return false;
            if (_consecutiveLosses >= MaxConsecutiveLosses) return false;

            double spreadPips = Symbol.Spread / Symbol.PipSize;
            if (spreadPips > MaxSpreadPips) return false;

            double maxLossCash = Account.Balance * MaxDailyLossPercent / 100.0;
            if (_dailyRealizedPnl <= -maxLossCash) return false;

            return true;
        }

        private double CalculateVolume(double stopPips)
        {
            double riskCash = Account.Balance * RiskPercent / 100.0;
            double pipValuePerUnit = Symbol.PipValue;
            if (pipValuePerUnit <= 0 || stopPips <= 0) return 0;

            double rawUnits = riskCash / (stopPips * pipValuePerUnit);
            double normalized = Symbol.NormalizeVolumeInUnits(rawUnits, RoundingMode.Down);
            if (normalized < Symbol.VolumeInUnitsMin) return 0;
            if (normalized > Symbol.VolumeInUnitsMax) normalized = Symbol.VolumeInUnitsMax;
            return normalized;
        }

        private int CountOwnPositions()
        {
            int c = 0;
            foreach (var p in Positions)
                if (p.SymbolName == SymbolName && p.Label == TradeLabel) c++;
            return c;
        }

        private void OnPositionClosed(PositionClosedEventArgs args)
        {
            var p = args.Position;
            if (p.SymbolName != SymbolName || p.Label != TradeLabel) return;

            _initRiskPrice.Remove(p.Id);
            _entryBarIndex.Remove(p.Id);

            double net = p.NetProfit;
            _dailyRealizedPnl += net;
            if (net < 0) _consecutiveLosses++;
            else if (net > 0) _consecutiveLosses = 0;
        }

        // =====================================================================
        // Open-position management
        // =====================================================================

        private void ManageBreakeven()
        {
            foreach (var p in Positions)
            {
                if (p.SymbolName != SymbolName || p.Label != TradeLabel) continue;
                double risk;
                if (!_initRiskPrice.TryGetValue(p.Id, out risk) || risk <= 0) continue;

                double entry = p.EntryPrice;
                if (p.TradeType == TradeType.Buy)
                {
                    if (p.StopLoss.HasValue && p.StopLoss.Value >= entry) continue;
                    if (Symbol.Bid - entry >= BreakevenTriggerR * risk)
                        ModifyPosition(p, entry, p.TakeProfit);
                }
                else
                {
                    if (p.StopLoss.HasValue && p.StopLoss.Value <= entry) continue;
                    if (entry - Symbol.Ask >= BreakevenTriggerR * risk)
                        ModifyPosition(p, entry, p.TakeProfit);
                }
            }
        }

        private void ManageTimeStop()
        {
            foreach (var p in Positions.ToArray())
            {
                if (p.SymbolName != SymbolName || p.Label != TradeLabel) continue;
                int entryBar;
                if (!_entryBarIndex.TryGetValue(p.Id, out entryBar)) continue;
                if (Bars.Count - entryBar >= MaxBarsInTrade)
                {
                    ClosePosition(p);
                    Print("Time stop: closed PID{0} after {1} bars", p.Id, Bars.Count - entryBar);
                }
            }
        }

        // =====================================================================
        // Rolling-buffer helper
        // =====================================================================

        private void PushReturn(double r)
        {
            _logReturns.Add(r);
            int max = EstimationWindow + 1;
            if (_logReturns.Count > max)
                _logReturns.RemoveRange(0, _logReturns.Count - max);
        }
    }

    public enum DomTradeMode
    {
        Continuation,
        Reversal
    }

    // =========================================================================
    // Merton jump-diffusion estimate + Lee-Mykland (2008) online jump test.
    // (Identical maths to the CVD variant; duplicated here so each bot file is
    //  self-contained per this repo's one-file-per-bot convention.)
    // =========================================================================
    public struct DomJumpEstimate
    {
        public bool IsReady;
        public double SigmaDiffusion;
        public double Lambda;
        public double MuJump;
        public double SigmaJump;
        public double CriticalValue;
        public double SigmaLocal;

        public static readonly DomJumpEstimate Empty = new DomJumpEstimate { IsReady = false };

        public override string ToString()
        {
            if (!IsReady) return "model[not ready]";
            return string.Format("model[sigma={0:P3} lambda={1:F3}/bar muJ={2:P3} sigmaJ={3:P3} crit={4:F2}]",
                SigmaDiffusion, Lambda, MuJump, SigmaJump, CriticalValue);
        }

        public static DomJumpEstimate Estimate(List<double> returns, int localWindow, double confidencePct, double minSigma)
        {
            var est = Empty;
            int n = returns.Count;
            if (n < localWindow + 2) return est;

            double bpvSum = 0;
            int bpvCount = 0;
            for (int i = 1; i < n; i++)
            {
                bpvSum += Math.Abs(returns[i]) * Math.Abs(returns[i - 1]);
                bpvCount++;
            }
            const double mu1 = 0.7978845608028654;
            double bpv = bpvCount > 0 ? (1.0 / (mu1 * mu1)) * (bpvSum / bpvCount) : 0;
            est.SigmaDiffusion = Math.Sqrt(Math.Max(bpv, 0));

            est.SigmaLocal = LocalBipowerSigma(returns, n - 1, localWindow);
            est.CriticalValue = LeeMyklandCritical(n, confidencePct, minSigma);

            var jumps = new List<double>();
            for (int i = localWindow; i < n; i++)
            {
                double sl = LocalBipowerSigma(returns, i, localWindow);
                if (sl <= 0) continue;
                double stat = Math.Abs(returns[i]) / sl;
                if (stat > est.CriticalValue) jumps.Add(returns[i]);
            }

            int tested = n - localWindow;
            est.Lambda = tested > 0 ? (double)jumps.Count / tested : 0;
            if (jumps.Count >= 2)
            {
                est.MuJump = jumps.Average();
                double var = jumps.Sum(x => (x - est.MuJump) * (x - est.MuJump)) / (jumps.Count - 1);
                est.SigmaJump = Math.Sqrt(Math.Max(var, 0));
            }
            else if (jumps.Count == 1)
            {
                est.MuJump = jumps[0];
                est.SigmaJump = Math.Abs(jumps[0]);
            }
            else
            {
                est.MuJump = 0;
                est.SigmaJump = est.SigmaDiffusion;
            }

            est.IsReady = est.SigmaLocal > 0;
            return est;
        }

        private static double LocalBipowerSigma(List<double> r, int i, int K)
        {
            int from = i - K + 2;
            if (from < 1) return 0;
            double s = 0;
            int cnt = 0;
            for (int j = from; j <= i - 1; j++)
            {
                s += Math.Abs(r[j - 1]) * Math.Abs(r[j]);
                cnt++;
            }
            if (cnt < 1) return 0;
            const double mu1sq = 0.6366197723675814;
            return Math.Sqrt(s / cnt / mu1sq);
        }

        private static double LeeMyklandCritical(int n, double confidencePct, double minSigma)
        {
            double c = 0.7978845608028654;
            double sqrt2ln = Math.Sqrt(2.0 * Math.Log(n));
            double Cn = sqrt2ln / c - (Math.Log(Math.PI) + Math.Log(Math.Log(n))) / (2.0 * c * sqrt2ln);
            double Sn = 1.0 / (c * sqrt2ln);
            double alpha = 1.0 - confidencePct / 100.0;
            double betaStar = -Math.Log(-Math.Log(1.0 - alpha));
            double crit = Cn + Sn * betaStar;
            return Math.Max(crit, minSigma);
        }

        public bool IsJump(double lastReturn)
        {
            if (!IsReady || SigmaLocal <= 0) return false;
            return Math.Abs(lastReturn) / SigmaLocal > CriticalValue;
        }

        public double ExpectedJumpMovePrice(double refPrice)
        {
            if (!IsReady || refPrice <= 0) return 0;
            double mag = Math.Sqrt(MuJump * MuJump + SigmaJump * SigmaJump);
            return refPrice * mag;
        }
    }
}
