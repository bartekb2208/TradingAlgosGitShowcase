using System;
using System.Collections.Generic;
using System.Linq;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    // =====================================================================================
    // Order-Flow + Jump-Diffusion cBot  —  CVD (tick-rule) variant  —  BACKTESTABLE
    // Designed for an index / commodity CFD (US500, NAS100, XAUUSD, ...) on M5.
    // -------------------------------------------------------------------------------------
    // TWO complementary signal layers:
    //
    //   1) JUMP-DIFFUSION layer (Merton 1976). Per-bar log returns are modelled as a
    //      continuous diffusion (vol sigma) plus a Poisson jump process (intensity lambda,
    //      jump size ~ N(muJ, sigmaJ)). Jumps are detected ONLINE with the Lee-Mykland (2008)
    //      statistic: a return is a jump when |r_i| / sigma_local exceeds a Gumbel-derived
    //      critical value, where sigma_local is a jump-ROBUST bipower-variation volatility.
    //      Merton parameters (sigma, lambda, muJ, sigmaJ) are re-estimated on a rolling window
    //      and drive (a) the jump gate and (b) volatility-scaled stops/targets.
    //
    //   2) ORDER-FLOW layer (Cumulative Volume Delta, tick-rule). Every tick is classified as
    //      buy/sell by the uptick/downtick rule (Lee-Ready style) using the bid price; the
    //      signed count accumulates into a per-bar delta and a running CVD. This is a PROXY:
    //      retail/CFD feeds expose tick activity (TickVolume), not true exchange-traded
    //      bid/ask volume. It is, however, fully reproducible in backtest (use tick-data
    //      accuracy) and live.
    //
    // COMBINATION. On the close of an M5 bar we ask: did a jump just occur (or is one
    // imminent)? If so, is the order flow aligned?
    //   * Continuation mode : trade IN the jump direction when CVD delta agrees.
    //   * Reversal/fade mode : trade AGAINST the jump when CVD diverges (exhaustion).
    //
    // RISK. Fixed-fractional sizing; stop = max(diffusion-vol stop, jump-vol stop); R:R target;
    // spread filter; daily-loss cap; consecutive-loss halt; max trades/day; max concurrent.
    //
    // NOTE on types: every helper type in this file is prefixed `Cvd...` so this bot stays
    // self-contained and never collides with the DOM-variant file if both compile together.
    // =====================================================================================
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class OrderFlowJumpCvdBot : Robot
    {
        #region Parameters - Jump-Diffusion

        [Parameter("Estimation Window (bars)", Group = "Jump-Diffusion", DefaultValue = 288, MinValue = 60, MaxValue = 2000)]
        public int EstimationWindow { get; set; }   // 288 M5 bars ~= 1 trading day

        [Parameter("Lee-Mykland Local Window (bars)", Group = "Jump-Diffusion", DefaultValue = 60, MinValue = 16, MaxValue = 300)]
        public int LocalWindow { get; set; }        // K for bipower local volatility

        [Parameter("Jump Confidence (%)", Group = "Jump-Diffusion", DefaultValue = 99.0, MinValue = 90.0, MaxValue = 99.99)]
        public double JumpConfidence { get; set; }  // alpha for the Lee-Mykland critical value

        [Parameter("Min Jump Sigma (fallback)", Group = "Jump-Diffusion", DefaultValue = 4.0, MinValue = 2.0, MaxValue = 10.0)]
        public double MinJumpSigma { get; set; }    // floor on |r|/sigma_local regardless of critical value

        #endregion

        #region Parameters - Order Flow (CVD)

        [Parameter("CVD Lookback (bars)", Group = "Order Flow", DefaultValue = 12, MinValue = 2, MaxValue = 100)]
        public int CvdLookback { get; set; }        // window over which delta direction is judged

        [Parameter("Min Aligned Delta (ticks)", Group = "Order Flow", DefaultValue = 0, MinValue = 0, MaxValue = 100000)]
        public int MinAlignedDelta { get; set; }    // require at least this |delta| to act (0 = sign only)

        [Parameter("Use Flow Ratio (delta/ticks)", Group = "Order Flow", DefaultValue = true)]
        public bool UseFlowRatio { get; set; }      // normalise delta by total ticks -> [-1,+1]

        [Parameter("Min |Flow Ratio|", Group = "Order Flow", DefaultValue = 0.25, MinValue = 0.0, MaxValue = 1.0)]
        public double MinFlowRatio { get; set; }    // require this much directional conviction

        #endregion

        #region Parameters - Strategy

        [Parameter("Trade Mode", Group = "Strategy", DefaultValue = CvdTradeMode.Continuation)]
        public CvdTradeMode Mode { get; set; }

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

        // US cash session ~ 13:30-20:00 UTC. Entries only inside [start, end).
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
        public double StopJumpSigmaMult { get; set; }   // stop also respects expected jump size

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

        [Parameter("Trade Label", Group = "Meta", DefaultValue = "OF_JD_CVD")]
        public string TradeLabel { get; set; }

        [Parameter("Show Diagnostics", Group = "Meta", DefaultValue = true)]
        public bool ShowDiagnostics { get; set; }

        #endregion

        // --- State ---
        private AverageTrueRange _atr;
        private ExponentialMovingAverage _regimeMa;
        private Bars _regimeBars;

        // Order-flow (tick) state
        private double _prevBid = double.NaN;
        private int _lastTickSign;          // +1 buy, -1 sell, 0 none (carried)
        private long _currentBarDelta;      // signed tick count for the forming bar
        private long _currentBarTicks;      // total classified ticks for the forming bar
        private readonly List<long> _barDeltas = new List<long>();   // closed-bar deltas (oldest..newest)
        private readonly List<long> _barTicks = new List<long>();    // closed-bar total tick counts

        // Jump-diffusion state: rolling log returns of bar closes
        private readonly List<double> _logReturns = new List<double>();
        private double _prevClose = double.NaN;
        private CvdJumpEstimate _model = CvdJumpEstimate.Empty;

        // Per-position bookkeeping (keyed by Position.Id)
        private readonly Dictionary<int, double> _initRiskPrice = new Dictionary<int, double>();  // entry stop distance (price)
        private readonly Dictionary<int, int> _entryBarIndex = new Dictionary<int, int>();        // Bars.Count at entry

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

            _atr = Indicators.AverageTrueRange(Bars, AtrPeriod, MovingAverageType.Exponential);
            _regimeBars = MarketData.GetBars(RegimeTimeFrame);
            _regimeMa = Indicators.ExponentialMovingAverage(_regimeBars.ClosePrices, RegimeMaPeriod);

            Symbol.Tick += OnSymbolTick;
            Bars.BarClosed += OnBarClosed;
            Positions.Closed += OnPositionClosed;

            _currentDay = Server.Time.Date;
            SeedFromHistory();

            Print("OrderFlow+JumpDiffusion (CVD) started on {0} {1}. PipSize={2} PipValue={3} VolMin={4} VolStep={5}",
                SymbolName, Bars.TimeFrame, Symbol.PipSize, Symbol.PipValue,
                Symbol.VolumeInUnitsMin, Symbol.VolumeInUnitsStep);
            Print("Initial model: {0}", _model);
        }

        protected override void OnStop()
        {
            Symbol.Tick -= OnSymbolTick;
            Bars.BarClosed -= OnBarClosed;
            Positions.Closed -= OnPositionClosed;
        }

        // Pre-fill the return history from already-loaded bars so the model is warm on bar 1.
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
            _model = CvdJumpEstimate.Estimate(_logReturns, LocalWindow, JumpConfidence, MinJumpSigma);
        }

        // =====================================================================
        // Order flow — tick classification (uptick/downtick rule)
        // =====================================================================

        private void OnSymbolTick(SymbolTickEventArgs e)
        {
            double bid = e.Bid;
            if (!double.IsNaN(_prevBid))
            {
                int sign;
                if (bid > _prevBid) sign = +1;
                else if (bid < _prevBid) sign = -1;
                else sign = _lastTickSign;   // zero-tick: carry previous classification

                if (sign != 0)
                {
                    _currentBarDelta += sign;
                    _currentBarTicks++;
                    _lastTickSign = sign;
                }
            }
            _prevBid = bid;

            if (UseBreakeven)
                ManageBreakeven();
        }

        // =====================================================================
        // Core: runs on every closed M5 bar
        // =====================================================================

        private void OnBarClosed(BarClosedEventArgs e)
        {
            var bars = e.Bars;
            int idx = bars.Count - 2;   // index of the bar that just closed
            if (idx < 1) { RollBarState(); return; }

            // --- 1. New day housekeeping ---
            var barDay = bars.OpenTimes[idx].Date;
            if (barDay != _currentDay)
            {
                _currentDay = barDay;
                _dailyRealizedPnl = 0;
                _dailyTradeCount = 0;
            }

            // --- 2. Update jump-diffusion model with the closed bar's return ---
            double close = bars.ClosePrices[idx];
            double ret = double.NaN;
            if (!double.IsNaN(_prevClose) && _prevClose > 0 && close > 0)
            {
                ret = Math.Log(close / _prevClose);
                PushReturn(ret);
            }
            _prevClose = close;
            _model = CvdJumpEstimate.Estimate(_logReturns, LocalWindow, JumpConfidence, MinJumpSigma);

            // --- 3. Commit the closed bar's order-flow delta + tick count ---
            long barDelta = _currentBarDelta;
            PushDelta(barDelta, _currentBarTicks);

            // --- 4. Manage open positions (time stop) ---
            if (UseTimeStop)
                ManageTimeStop();

            // --- 5. Diagnostics + signal ---
            if (ShowDiagnostics && !double.IsNaN(ret))
                DrawDiagnostics(bars, idx, ret);

            if (!double.IsNaN(ret))
                TryTrade(ret, barDelta);

            RollBarState();
        }

        private void RollBarState()
        {
            _currentBarDelta = 0;   // start accumulating the next bar fresh
            _currentBarTicks = 0;
        }

        // =====================================================================
        // Signal + execution
        // =====================================================================

        private void TryTrade(double lastReturn, long lastBarDelta)
        {
            if (!_model.IsReady) return;
            if (!TradingGuardsPass()) return;
            if (UseSessionFilter && !InSession(Server.Time)) return;

            // Jump detection on the just-closed bar.
            bool isJump = _model.IsJump(lastReturn);
            if (!isJump) return;

            int jumpDir = Math.Sign(lastReturn);          // +1 up-jump, -1 down-jump
            if (jumpDir == 0) return;

            // Order-flow direction over the lookback window — raw delta or normalised ratio.
            long flow = RecentDelta(CvdLookback);
            int flowDir = FlowDirection(flow);

            int tradeDir;
            if (Mode == CvdTradeMode.Continuation)
            {
                // Trade WITH the jump; flow must agree (or agreement not required).
                if (RequireFlowAgreement && flowDir != jumpDir) return;
                tradeDir = jumpDir;
            }
            else // Reversal / fade
            {
                // Fade an exhausted jump: enter opposite the jump when flow diverges from it.
                if (RequireFlowAgreement && flowDir == jumpDir) return;  // flow still supports jump -> no fade
                tradeDir = -jumpDir;
            }

            // Regime filter: only trade in the higher-timeframe trend direction.
            if (UseRegimeFilter)
            {
                int regime = RegimeDirection();
                if (regime != 0 && tradeDir != regime) return;
            }

            if (tradeDir > 0 && !AllowLongs) return;
            if (tradeDir < 0 && !AllowShorts) return;

            Execute(tradeDir, lastReturn, flow);
        }

        // Direction of order flow, gated by either the normalised ratio or the raw-delta floor.
        private int FlowDirection(long flow)
        {
            if (UseFlowRatio)
            {
                long ticks = RecentTicks(CvdLookback);
                if (ticks <= 0) return 0;
                double ratio = (double)flow / ticks;       // in [-1, +1]
                return Math.Abs(ratio) >= MinFlowRatio ? Math.Sign(ratio) : 0;
            }
            return Math.Abs(flow) >= MinAlignedDelta ? Math.Sign(flow) : 0;
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

        private bool InSession(DateTime utc)
        {
            int h = utc.Hour;
            if (SessionStartHour == SessionEndHour) return true;          // 24h
            if (SessionStartHour < SessionEndHour)
                return h >= SessionStartHour && h < SessionEndHour;        // same-day window
            return h >= SessionStartHour || h < SessionEndHour;           // wraps midnight
        }

        private void Execute(int dir, double lastReturn, long flow)
        {
            var tradeType = dir > 0 ? TradeType.Buy : TradeType.Sell;

            // Stop = max of an ATR-based and a jump-volatility-based distance (in price).
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
                _initRiskPrice[id] = stopPips * Symbol.PipSize;   // entry risk in price units
                _entryBarIndex[id] = Bars.Count;                 // for the time stop
                Print("{0} {1} @ {2:F2} | jumpRet={3:P3} flow={4} lambda={5:F3} sigmaJ={6:P3} SL={7:F1}p TP={8:F1}p vol={9}",
                    Mode, tradeType, result.Position.EntryPrice, lastReturn, flow,
                    _model.Lambda, _model.SigmaJump, stopPips, tpPips, volume);
            }
            else
            {
                Print("Order rejected: {0}", result.Error);
            }
        }

        // =====================================================================
        // Chart diagnostics
        // =====================================================================

        private void DrawDiagnostics(Bars bars, int idx, double ret)
        {
            // Live readout, top-left.
            long flow = RecentDelta(CvdLookback);
            string txt = string.Format(
                "OF+JD (CVD)\nsigma {0:P2}/bar\nlambda {1:F3}/bar\nmuJ {2:P2}  sigmaJ {3:P2}\ncrit {4:F2}  |last| {5:F2}\nCVD({6}) {7}",
                _model.SigmaDiffusion, _model.Lambda, _model.MuJump, _model.SigmaJump,
                _model.CriticalValue, _model.SigmaLocal > 0 ? Math.Abs(ret) / _model.SigmaLocal : 0,
                CvdLookback, flow);
            Chart.DrawStaticText("of_jd_cvd_stats", txt, VerticalAlignment.Top, HorizontalAlignment.Left, Color.White);

            // Jump markers on the closed bar.
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
            double pipValuePerUnit = Symbol.PipValue;   // account-currency value of 1 pip for 1 unit
            if (pipValuePerUnit <= 0 || stopPips <= 0) return 0;

            double rawUnits = riskCash / (stopPips * pipValuePerUnit);
            double normalized = Symbol.NormalizeVolumeInUnits(rawUnits, RoundingMode.Down);
            if (normalized < Symbol.VolumeInUnitsMin) return 0;   // can't risk this little -> skip
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

        // Move stop to entry once price has travelled BreakevenTriggerR * initial-risk in our favour.
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
                    if (p.StopLoss.HasValue && p.StopLoss.Value >= entry) continue;   // already at/above BE
                    if (Symbol.Bid - entry >= BreakevenTriggerR * risk)
                        ModifyPosition(p, entry, p.TakeProfit);
                }
                else
                {
                    if (p.StopLoss.HasValue && p.StopLoss.Value <= entry) continue;   // already at/below BE
                    if (entry - Symbol.Ask >= BreakevenTriggerR * risk)
                        ModifyPosition(p, entry, p.TakeProfit);
                }
            }
        }

        // Close positions that have been open longer than MaxBarsInTrade bars.
        private void ManageTimeStop()
        {
            foreach (var p in Positions.ToArray())   // copy: we may close inside the loop
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
        // Rolling-buffer helpers
        // =====================================================================

        private void PushReturn(double r)
        {
            _logReturns.Add(r);
            int max = EstimationWindow + 1;
            if (_logReturns.Count > max)
                _logReturns.RemoveRange(0, _logReturns.Count - max);
        }

        private void PushDelta(long d, long ticks)
        {
            _barDeltas.Add(d);
            _barTicks.Add(ticks);
            int max = Math.Max(CvdLookback, 256);
            if (_barDeltas.Count > max)
                _barDeltas.RemoveRange(0, _barDeltas.Count - max);
            if (_barTicks.Count > max)
                _barTicks.RemoveRange(0, _barTicks.Count - max);
        }

        private long RecentDelta(int lookback)
        {
            long sum = 0;
            int start = Math.Max(0, _barDeltas.Count - lookback);
            for (int i = start; i < _barDeltas.Count; i++) sum += _barDeltas[i];
            return sum;
        }

        private long RecentTicks(int lookback)
        {
            long sum = 0;
            int start = Math.Max(0, _barTicks.Count - lookback);
            for (int i = start; i < _barTicks.Count; i++) sum += _barTicks[i];
            return sum;
        }
    }

    public enum CvdTradeMode
    {
        Continuation,
        Reversal
    }

    // =========================================================================
    // Merton jump-diffusion estimate + Lee-Mykland (2008) online jump test.
    //   * sigmaDiffusion : jump-robust diffusion volatility, sqrt(bipower variation / n)
    //   * lambda         : detected jumps per bar (intensity)
    //   * muJump,sigmaJump: mean / std of detected jump returns (log)
    //   * critical value : Gumbel-based threshold on |r| / sigma_local
    // =========================================================================
    public struct CvdJumpEstimate
    {
        public bool IsReady;
        public double SigmaDiffusion;   // per-bar diffusion vol (log)
        public double Lambda;           // jumps per bar
        public double MuJump;           // mean log jump size
        public double SigmaJump;        // std of log jump size
        public double CriticalValue;    // Lee-Mykland threshold for |r|/sigma_local
        public double SigmaLocal;       // most recent local bipower volatility

        public static readonly CvdJumpEstimate Empty = new CvdJumpEstimate { IsReady = false };

        public override string ToString()
        {
            if (!IsReady) return "model[not ready]";
            return string.Format("model[sigma={0:P3} lambda={1:F3}/bar muJ={2:P3} sigmaJ={3:P3} crit={4:F2}]",
                SigmaDiffusion, Lambda, MuJump, SigmaJump, CriticalValue);
        }

        // Bipower local volatility around the END of the series (most recent LocalWindow returns),
        // used as the denominator for the most-recent return's jump test.
        public static CvdJumpEstimate Estimate(List<double> returns, int localWindow, double confidencePct, double minSigma)
        {
            var est = Empty;
            int n = returns.Count;
            if (n < localWindow + 2) return est;

            // --- Global diffusion vol via bipower variation over the whole window ---
            double bpvSum = 0;
            int bpvCount = 0;
            for (int i = 1; i < n; i++)
            {
                bpvSum += Math.Abs(returns[i]) * Math.Abs(returns[i - 1]);
                bpvCount++;
            }
            const double mu1 = 0.7978845608028654;          // E|Z| = sqrt(2/pi)
            double bpv = bpvCount > 0 ? (1.0 / (mu1 * mu1)) * (bpvSum / bpvCount) : 0; // per-bar integrated var
            est.SigmaDiffusion = Math.Sqrt(Math.Max(bpv, 0));

            // --- Local bipower volatility for the most recent return ---
            est.SigmaLocal = LocalBipowerSigma(returns, n - 1, localWindow);

            // --- Lee-Mykland critical value (Gumbel) ---
            est.CriticalValue = LeeMyklandCritical(n, confidencePct, minSigma);

            // --- Classify jumps across the window to estimate Merton params ---
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
                est.SigmaJump = est.SigmaDiffusion;   // fallback so stops stay sane
            }

            est.IsReady = est.SigmaLocal > 0;
            return est;
        }

        // sigma_local = sqrt( (1/(K-2)) * sum_{j=i-K+2..i-1} |r_{j-1}|*|r_j| / mu1^2 )
        // window ends just BEFORE i so the tested return r_i does not contaminate its own scale.
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
            const double mu1sq = 0.6366197723675814; // (sqrt(2/pi))^2 = 2/pi
            return Math.Sqrt(s / cnt / mu1sq);
        }

        // Lee & Mykland (2008) rejection: reject H0(no jump) when
        //   (max_i L_i - C_n) / S_n > beta*,  beta* = -ln(-ln(1-alpha))
        // Applied per-return this gives an effective threshold C_n + S_n*beta* on L_i.
        private static double LeeMyklandCritical(int n, double confidencePct, double minSigma)
        {
            double c = 0.7978845608028654;                 // sqrt(2/pi)
            double sqrt2ln = Math.Sqrt(2.0 * Math.Log(n));
            double Cn = sqrt2ln / c - (Math.Log(Math.PI) + Math.Log(Math.Log(n))) / (2.0 * c * sqrt2ln);
            double Sn = 1.0 / (c * sqrt2ln);
            double alpha = 1.0 - confidencePct / 100.0;
            double betaStar = -Math.Log(-Math.Log(1.0 - alpha));
            double crit = Cn + Sn * betaStar;
            return Math.Max(crit, minSigma);
        }

        // Is the most recent return a jump, using the current local scale?
        public bool IsJump(double lastReturn)
        {
            if (!IsReady || SigmaLocal <= 0) return false;
            return Math.Abs(lastReturn) / SigmaLocal > CriticalValue;
        }

        // Expected absolute jump move converted to a PRICE distance at the given reference price.
        // Uses sqrt(muJ^2 + sigmaJ^2) as the representative jump magnitude (log) -> price.
        public double ExpectedJumpMovePrice(double refPrice)
        {
            if (!IsReady || refPrice <= 0) return 0;
            double mag = Math.Sqrt(MuJump * MuJump + SigmaJump * SigmaJump);
            return refPrice * mag;   // small-move approx: price * |log-return|
        }
    }
}
