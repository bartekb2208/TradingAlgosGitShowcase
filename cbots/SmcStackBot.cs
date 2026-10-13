using System;
using System.Collections.Generic;
using System.Linq;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    // SMC "full stack" cBot — the classic Smart Money Concepts pipeline, end to end:
    // ---------------------------------------------------------------------------------
    //   1. HTF (H4) market structure for BIAS      — swing-based BOS/CHoCH tracker.
    //   2. Setup TF (M15) liquidity SWEEP          — wick through a prior swing, close back inside.
    //   3. DISPLACEMENT for confirmation           — >=N strong-bodied ATR-multiple candles with bias.
    //   4. FVG / ORDER BLOCK for the entry zone    — 3-bar gap and/or last opposite candle
    //                                                before displacement; optional overlap confluence.
    //   5. LTF execution (chart TF, M1–M5)         — retrace into zone + confirmation candle,
    //                                                or a resting limit order at the zone.
    //
    // Trade management: stop below/above the sweep extreme (+buffer), fixed-R:R target,
    // breakeven at +X R, optional time stop. Risk: fixed-fractional sizing, spread filter,
    // daily-loss cap, consecutive-loss halt, max trades/day, max concurrent positions,
    // NY-anchored killzone filter (self-contained DST handling).
    //
    // Run the bot on an M1–M5 chart; the chart timeframe IS the execution LTF.
    // Backtestable (bar-close logic; use tick data for LTF entry fidelity).
    // Neutral defaults — walk-forward test and tune per instrument before live capital.
    // ---------------------------------------------------------------------------------
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class SmcStackBot : Robot
    {
        #region Parameters - Timeframes

        [Parameter("Bias Timeframe (HTF)", Group = "Timeframes", DefaultValue = "Hour4")]
        public TimeFrame BiasTimeFrame { get; set; }

        [Parameter("Setup Timeframe", Group = "Timeframes", DefaultValue = "Minute15")]
        public TimeFrame SetupTimeFrame { get; set; }

        #endregion

        #region Parameters - HTF Structure (bias)

        [Parameter("HTF Swing Length", Group = "Structure", DefaultValue = 5, MinValue = 2, MaxValue = 20)]
        public int HtfSwingLength { get; set; }

        [Parameter("HTF Structure Lookback (bars)", Group = "Structure", DefaultValue = 200, MinValue = 50, MaxValue = 1000)]
        public int HtfStructureLookback { get; set; }

        [Parameter("Allow Neutral-Bias Trades", Group = "Structure", DefaultValue = false)]
        public bool AllowNeutralBias { get; set; }

        #endregion

        #region Parameters - Setup (sweep / displacement / zone)

        [Parameter("Sweep Lookback (setup bars)", Group = "Setup", DefaultValue = 30, MinValue = 10, MaxValue = 200)]
        public int SweepLookback { get; set; }

        [Parameter("Swing Length (setup bars)", Group = "Setup", DefaultValue = 5, MinValue = 3, MaxValue = 20)]
        public int SwingLength { get; set; }

        [Parameter("Displacement ATR Multiplier", Group = "Setup", DefaultValue = 1.2, MinValue = 0.3, MaxValue = 5.0)]
        public double DisplacementAtrMult { get; set; }

        [Parameter("Min Displacement Candles", Group = "Setup", DefaultValue = 2, MinValue = 1, MaxValue = 5)]
        public int MinDisplacementCandles { get; set; }

        [Parameter("ATR Period (setup TF)", Group = "Setup", DefaultValue = 14, MinValue = 5, MaxValue = 50)]
        public int AtrPeriod { get; set; }

        [Parameter("Entry Zone", Group = "Setup", DefaultValue = SmcZoneMode.FvgOrOrderBlock)]
        public SmcZoneMode ZoneMode { get; set; }

        [Parameter("Order Block = Body Only", Group = "Setup", DefaultValue = true)]
        public bool ObBodyOnly { get; set; }

        [Parameter("Zone Validity (setup bars)", Group = "Setup", DefaultValue = 20, MinValue = 3, MaxValue = 100)]
        public int ZoneValidBars { get; set; }

        #endregion

        #region Parameters - Execution (LTF)

        [Parameter("Entry Mode", Group = "Execution", DefaultValue = SmcEntryMode.LtfConfirmation)]
        public SmcEntryMode EntryMode { get; set; }

        [Parameter("Require 50% Zone Retrace", Group = "Execution", DefaultValue = true)]
        public bool RequireZoneMidTouch { get; set; }

        [Parameter("Limit Order At Zone Mid (else edge)", Group = "Execution", DefaultValue = true)]
        public bool LimitAtZoneMid { get; set; }

        #endregion

        #region Parameters - Killzones (NY local time; DST handled automatically)

        [Parameter("Use Killzone Filter", Group = "Killzones", DefaultValue = true)]
        public bool UseKillzoneFilter { get; set; }

        [Parameter("London KZ (NY 02:00-05:00)", Group = "Killzones", DefaultValue = true)]
        public bool TradeLondonKz { get; set; }

        [Parameter("NY AM KZ (NY 07:00-10:00)", Group = "Killzones", DefaultValue = true)]
        public bool TradeNyAmKz { get; set; }

        [Parameter("NY PM KZ (NY 13:30-16:00)", Group = "Killzones", DefaultValue = false)]
        public bool TradeNyPmKz { get; set; }

        #endregion

        #region Parameters - Risk Management

        [Parameter("Risk Per Trade (%)", Group = "Risk", DefaultValue = 0.5, MinValue = 0.05, MaxValue = 2.0)]
        public double RiskPercent { get; set; }

        [Parameter("Stop Buffer (pips)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.0, MaxValue = 50.0)]
        public double StopBufferPips { get; set; }

        [Parameter("Take Profit (R multiple)", Group = "Risk", DefaultValue = 2.0, MinValue = 0.5, MaxValue = 10.0)]
        public double TakeProfitRR { get; set; }

        [Parameter("Breakeven Trigger (R, 0 = off)", Group = "Risk", DefaultValue = 1.0, MinValue = 0.0, MaxValue = 5.0)]
        public double BreakevenTriggerR { get; set; }

        [Parameter("Max Bars In Trade (chart TF, 0 = off)", Group = "Risk", DefaultValue = 0, MinValue = 0, MaxValue = 2000)]
        public int MaxBarsInTrade { get; set; }

        [Parameter("Max Spread (pips)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.0, MaxValue = 50.0)]
        public double MaxSpreadPips { get; set; }

        [Parameter("Max Daily Loss (% of balance)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.5, MaxValue = 20.0)]
        public double MaxDailyLossPercent { get; set; }

        [Parameter("Max Consecutive Losses", Group = "Risk", DefaultValue = 3, MinValue = 1, MaxValue = 20)]
        public int MaxConsecutiveLosses { get; set; }

        [Parameter("Max Trades Per Day", Group = "Risk", DefaultValue = 5, MinValue = 1, MaxValue = 50)]
        public int MaxTradesPerDay { get; set; }

        #endregion

        #region Parameters - Meta

        [Parameter("Trade Label", Group = "Meta", DefaultValue = "SMC_STACK")]
        public string TradeLabel { get; set; }

        [Parameter("Allow Longs", Group = "Meta", DefaultValue = true)]
        public bool AllowLongs { get; set; }

        [Parameter("Allow Shorts", Group = "Meta", DefaultValue = true)]
        public bool AllowShorts { get; set; }

        [Parameter("Max Concurrent Positions", Group = "Meta", DefaultValue = 1, MinValue = 1, MaxValue = 5)]
        public int MaxPositions { get; set; }

        #endregion

        // --- State ---
        private Bars _htfBars, _setupBars;
        private AverageTrueRange _setupAtr;

        private SmcBias _bias = SmcBias.Neutral;
        private SmcZone _activeZone;

        private readonly Dictionary<long, double> _initialRiskById = new Dictionary<long, double>();
        private readonly Dictionary<long, int> _entryBarById = new Dictionary<long, int>();
        private readonly HashSet<long> _breakevenDone = new HashSet<long>();

        private DateTime _currentDay = DateTime.MinValue;
        private double _dailyRealizedPnl;
        private int _dailyTradeCount;
        private int _consecutiveLosses;

        // =====================================================================
        // Lifecycle
        // =====================================================================

        protected override void OnStart()
        {
            _htfBars = MarketData.GetBars(BiasTimeFrame);
            _setupBars = MarketData.GetBars(SetupTimeFrame);
            _setupAtr = Indicators.AverageTrueRange(_setupBars, AtrPeriod, MovingAverageType.Simple);

            _htfBars.BarClosed += OnHtfBarClosed;
            _setupBars.BarClosed += OnSetupBarClosed;
            Positions.Opened += OnPositionOpened;
            Positions.Closed += OnPositionClosed;

            _currentDay = Server.Time.Date;
            _bias = ComputeHtfBias();

            Print("SMC stack bot started. {0} bias TF={1} setup TF={2} chart TF={3} | initial bias={4}",
                SymbolName, BiasTimeFrame, SetupTimeFrame, TimeFrame, _bias);
            Print("PipSize={0} PipValue={1} VolMin={2} VolStep={3}",
                Symbol.PipSize, Symbol.PipValue, Symbol.VolumeInUnitsMin, Symbol.VolumeInUnitsStep);
            WarnIfChartTfTooHigh();
        }

        protected override void OnStop()
        {
            _htfBars.BarClosed -= OnHtfBarClosed;
            _setupBars.BarClosed -= OnSetupBarClosed;
            Positions.Opened -= OnPositionOpened;
            Positions.Closed -= OnPositionClosed;
        }

        private void WarnIfChartTfTooHigh()
        {
            if (Bars.Count < 3 || _setupBars.Count < 3) return;
            var chartSpan = Bars.OpenTimes[Bars.Count - 1] - Bars.OpenTimes[Bars.Count - 2];
            var setupSpan = _setupBars.OpenTimes[_setupBars.Count - 1] - _setupBars.OpenTimes[_setupBars.Count - 2];
            if (chartSpan >= setupSpan)
                Print("WARNING: chart TF >= setup TF. Run this bot on an M1-M5 chart; the chart is the execution LTF.");
        }

        // =====================================================================
        // DST helpers (self-contained; killzones anchor to NY local time)
        // =====================================================================

        private static DateTime NthSundayOfMonth(int year, int month, int n)
        {
            var first = new DateTime(year, month, 1);
            int fwd = ((int)DayOfWeek.Sunday - (int)first.DayOfWeek + 7) % 7;
            return first.AddDays(fwd + 7 * (n - 1));
        }

        private static bool IsUsEdt(DateTime utc)
        {
            int y = utc.Year;
            var start = NthSundayOfMonth(y, 3, 2).AddHours(7);
            var end = NthSundayOfMonth(y, 11, 1).AddHours(6);
            return utc >= start && utc < end;
        }

        private static DateTime UtcToNyLocal(DateTime utc)
        {
            return utc.AddHours(IsUsEdt(utc) ? -4 : -5);
        }

        // =====================================================================
        // 1. HTF market structure -> bias (BOS / CHoCH tracker)
        // =====================================================================

        private void OnHtfBarClosed(BarClosedEventArgs args)
        {
            var newBias = ComputeHtfBias();
            if (newBias == _bias) return;

            Print("[HTF] structure shift: {0} -> {1} ({2})",
                _bias, newBias, _bias == SmcBias.Neutral ? "BOS" : "CHoCH");
            _bias = newBias;

            if (_activeZone != null && !ZoneMatchesBias(_activeZone))
            {
                Print("[HTF] active zone conflicts with new bias, dropping it.");
                DropZone();
            }
        }

        private bool ZoneMatchesBias(SmcZone zone)
        {
            if (_bias == SmcBias.Neutral) return AllowNeutralBias;
            return zone.IsBullish == (_bias == SmcBias.Bullish);
        }

        // Walks the HTF window once, confirming fractal swings as they form and flipping
        // bias whenever a close breaks the most recent unbroken swing level.
        private SmcBias ComputeHtfBias()
        {
            int lastClosed = _htfBars.Count - 2;
            if (lastClosed < 2 * HtfSwingLength + 2) return SmcBias.Neutral;
            int start = Math.Max(2 * HtfSwingLength, lastClosed - HtfStructureLookback);

            double lastSwingHigh = double.NaN, lastSwingLow = double.NaN;
            var bias = SmcBias.Neutral;

            for (int i = start; i <= lastClosed; i++)
            {
                int s = i - HtfSwingLength; // swing candidate confirmed once HtfSwingLength bars follow it
                if (s >= HtfSwingLength)
                {
                    if (IsHtfSwingHigh(s)) lastSwingHigh = _htfBars.HighPrices[s];
                    if (IsHtfSwingLow(s)) lastSwingLow = _htfBars.LowPrices[s];
                }

                double close = _htfBars.ClosePrices[i];
                if (!double.IsNaN(lastSwingHigh) && close > lastSwingHigh)
                {
                    bias = SmcBias.Bullish;
                    lastSwingHigh = double.NaN; // consumed; wait for the next confirmed swing
                }
                else if (!double.IsNaN(lastSwingLow) && close < lastSwingLow)
                {
                    bias = SmcBias.Bearish;
                    lastSwingLow = double.NaN;
                }
            }
            return bias;
        }

        private bool IsHtfSwingHigh(int s)
        {
            double h = _htfBars.HighPrices[s];
            for (int k = 1; k <= HtfSwingLength; k++)
            {
                if (h <= _htfBars.HighPrices[s - k]) return false;
                if (h < _htfBars.HighPrices[s + k]) return false;
            }
            return true;
        }

        private bool IsHtfSwingLow(int s)
        {
            double l = _htfBars.LowPrices[s];
            for (int k = 1; k <= HtfSwingLength; k++)
            {
                if (l >= _htfBars.LowPrices[s - k]) return false;
                if (l > _htfBars.LowPrices[s + k]) return false;
            }
            return true;
        }

        // =====================================================================
        // 2-4. Setup TF: sweep -> displacement -> FVG / OB zone
        // =====================================================================

        private void OnSetupBarClosed(BarClosedEventArgs args)
        {
            UpdateZoneLifecycle();
            ScanSetup();

            if (_activeZone != null && EntryMode == SmcEntryMode.LimitAtZone && !_activeZone.OrderPlaced)
                TryPlaceLimitAtZone();
        }

        private void UpdateZoneLifecycle()
        {
            if (_activeZone == null) return;
            int lastClosed = _setupBars.Count - 2;
            if (lastClosed < 0) return;

            if (lastClosed - _activeZone.CreatedSetupIndex > ZoneValidBars)
            {
                Print("[zone] expired after {0} setup bars.", ZoneValidBars);
                DropZone();
                return;
            }

            double close = _setupBars.ClosePrices[lastClosed];
            if (_activeZone.IsBullish && close < _activeZone.Low)
            {
                Print("[zone] invalidated: setup close {0:F5} below zone low {1:F5}.", close, _activeZone.Low);
                DropZone();
            }
            else if (!_activeZone.IsBullish && close > _activeZone.High)
            {
                Print("[zone] invalidated: setup close {0:F5} above zone high {1:F5}.", close, _activeZone.High);
                DropZone();
            }
        }

        private void DropZone()
        {
            _activeZone = null;
            CancelLabelledPendingOrders();
        }

        private void ScanSetup()
        {
            if (_setupBars.Count < SweepLookback + SwingLength + 5) return;

            bool longOk = AllowLongs && (_bias == SmcBias.Bullish || (_bias == SmcBias.Neutral && AllowNeutralBias));
            bool shortOk = AllowShorts && (_bias == SmcBias.Bearish || (_bias == SmcBias.Neutral && AllowNeutralBias));
            int lastClosed = _setupBars.Count - 2;

            if (longOk)
            {
                var zone = TryFindZone(lastClosed, isLong: true);
                if (zone != null) { ActivateZone(zone); return; }
            }
            if (shortOk)
            {
                var zone = TryFindZone(lastClosed, isLong: false);
                if (zone != null) ActivateZone(zone);
            }
        }

        private void ActivateZone(SmcZone zone)
        {
            // Rescans find the same setup on every setup-bar close while the sweep is in
            // lookback; keep the live zone (and its Touched/OrderPlaced state) if unchanged.
            if (_activeZone != null
                && _activeZone.IsBullish == zone.IsBullish
                && Math.Abs(_activeZone.Low - zone.Low) < Symbol.TickSize
                && Math.Abs(_activeZone.High - zone.High) < Symbol.TickSize)
                return;

            bool replacing = _activeZone != null;
            if (replacing) CancelLabelledPendingOrders();
            _activeZone = zone;
            Print("[setup {0}] {1} zone [{2:F5}-{3:F5}] sweep={4:F5}{5}",
                zone.IsBullish ? "LONG" : "SHORT", zone.Source, zone.Low, zone.High, zone.SweepLevel,
                replacing ? " (replaced previous zone)" : "");
        }

        private SmcZone TryFindZone(int lastBar, bool isLong)
        {
            // 2. Liquidity sweep: a candle pierces the prior SwingLength-bar extreme, closes back inside.
            int sweepScanStart = Math.Max(SwingLength + 2, lastBar - SweepLookback);
            int sweepBarIdx = -1;

            for (int i = lastBar; i >= sweepScanStart; i--)
            {
                int priorFrom = Math.Max(0, i - SwingLength - 1);
                int priorTo = i - 1;
                if (priorTo < priorFrom) continue;

                if (isLong)
                {
                    double priorLow = double.MaxValue;
                    for (int j = priorFrom; j <= priorTo; j++)
                        if (_setupBars.LowPrices[j] < priorLow) priorLow = _setupBars.LowPrices[j];

                    if (_setupBars.LowPrices[i] < priorLow && _setupBars.ClosePrices[i] > priorLow)
                    {
                        sweepBarIdx = i;
                        break;
                    }
                }
                else
                {
                    double priorHigh = double.MinValue;
                    for (int j = priorFrom; j <= priorTo; j++)
                        if (_setupBars.HighPrices[j] > priorHigh) priorHigh = _setupBars.HighPrices[j];

                    if (_setupBars.HighPrices[i] > priorHigh && _setupBars.ClosePrices[i] < priorHigh)
                    {
                        sweepBarIdx = i;
                        break;
                    }
                }
            }
            if (sweepBarIdx < 0) return null;

            double sweepLevel = isLong
                ? _setupBars.LowPrices[sweepBarIdx]
                : _setupBars.HighPrices[sweepBarIdx];

            // 3. Displacement: enough strong-bodied candles after the sweep, in trade direction.
            int dispStart = sweepBarIdx + 1;
            int dispEnd = lastBar;
            if (dispEnd < dispStart + 1) return null;

            double atr = _setupAtr.Result[sweepBarIdx];
            if (double.IsNaN(atr) || atr <= 0) return null;
            double minBody = atr * DisplacementAtrMult;

            int strongCount = 0;
            for (int i = dispStart; i <= dispEnd; i++)
            {
                double body = Math.Abs(_setupBars.ClosePrices[i] - _setupBars.OpenPrices[i]);
                bool bullish = _setupBars.ClosePrices[i] > _setupBars.OpenPrices[i];
                if (body >= minBody && ((isLong && bullish) || (!isLong && !bullish)))
                    strongCount++;
            }
            if (strongCount < MinDisplacementCandles) return null;

            // 4a. FVG: first 3-bar gap inside the displacement window.
            double fvgLow = double.NaN, fvgHigh = double.NaN;
            int fvgCreated = -1;
            for (int i = dispStart + 1; i <= dispEnd; i++)
            {
                int i2 = i - 2;
                if (i2 < 0) continue;
                if (isLong && _setupBars.LowPrices[i] > _setupBars.HighPrices[i2])
                {
                    fvgLow = _setupBars.HighPrices[i2];
                    fvgHigh = _setupBars.LowPrices[i];
                    fvgCreated = i;
                    break;
                }
                if (!isLong && _setupBars.HighPrices[i] < _setupBars.LowPrices[i2])
                {
                    fvgLow = _setupBars.HighPrices[i];
                    fvgHigh = _setupBars.LowPrices[i2];
                    fvgCreated = i;
                    break;
                }
            }
            bool hasFvg = fvgCreated >= 0;

            // 4b. Order block: last opposite-direction candle at or before the displacement leg.
            double obLow = double.NaN, obHigh = double.NaN;
            int obIdx = -1;
            for (int i = dispStart; i >= Math.Max(0, sweepBarIdx - 3); i--)
            {
                bool bearishCandle = _setupBars.ClosePrices[i] < _setupBars.OpenPrices[i];
                if ((isLong && bearishCandle) || (!isLong && !bearishCandle))
                {
                    obIdx = i;
                    break;
                }
            }
            bool hasOb = obIdx >= 0;
            if (hasOb)
            {
                if (ObBodyOnly)
                {
                    obLow = Math.Min(_setupBars.OpenPrices[obIdx], _setupBars.ClosePrices[obIdx]);
                    obHigh = Math.Max(_setupBars.OpenPrices[obIdx], _setupBars.ClosePrices[obIdx]);
                }
                else
                {
                    obLow = _setupBars.LowPrices[obIdx];
                    obHigh = _setupBars.HighPrices[obIdx];
                }
            }

            // Combine per ZoneMode. Overlap = intersection of FVG and OB when both exist.
            double zLow, zHigh;
            string source;
            bool overlap = hasFvg && hasOb && Math.Min(fvgHigh, obHigh) > Math.Max(fvgLow, obLow);

            switch (ZoneMode)
            {
                case SmcZoneMode.FvgOnly:
                    if (!hasFvg) return null;
                    zLow = fvgLow; zHigh = fvgHigh; source = "FVG";
                    break;
                case SmcZoneMode.OrderBlockOnly:
                    if (!hasOb) return null;
                    zLow = obLow; zHigh = obHigh; source = "OB";
                    break;
                case SmcZoneMode.OverlapOnly:
                    if (!overlap) return null;
                    zLow = Math.Max(fvgLow, obLow); zHigh = Math.Min(fvgHigh, obHigh); source = "FVG+OB overlap";
                    break;
                default: // FvgOrOrderBlock: prefer overlap, then FVG, then OB
                    if (overlap) { zLow = Math.Max(fvgLow, obLow); zHigh = Math.Min(fvgHigh, obHigh); source = "FVG+OB overlap"; }
                    else if (hasFvg) { zLow = fvgLow; zHigh = fvgHigh; source = "FVG"; }
                    else if (hasOb) { zLow = obLow; zHigh = obHigh; source = "OB"; }
                    else return null;
                    break;
            }
            if (zHigh - zLow <= 0) return null;

            // Discard if a later setup close has already pushed through the zone's far side.
            int createdIdx = hasFvg ? fvgCreated : dispEnd;
            for (int j = createdIdx + 1; j <= lastBar; j++)
            {
                if (isLong && _setupBars.ClosePrices[j] < zLow) return null;
                if (!isLong && _setupBars.ClosePrices[j] > zHigh) return null;
            }

            return new SmcZone
            {
                Low = zLow,
                High = zHigh,
                IsBullish = isLong,
                CreatedSetupIndex = createdIdx,
                SweepLevel = sweepLevel,
                Source = source
            };
        }

        // =====================================================================
        // 5. LTF execution (chart TF)
        // =====================================================================

        protected override void OnBar()
        {
            ManageOpenPositions();

            if (_activeZone == null || EntryMode != SmcEntryMode.LtfConfirmation) return;
            if (!ZoneMatchesBias(_activeZone)) { DropZone(); return; }
            if (Positions.FindAll(TradeLabel, SymbolName).Length >= MaxPositions) return;
            if (!CanTradeNow()) return;

            int lastLtf = Bars.Count - 2;
            if (lastLtf < 1) return;

            double open = Bars.OpenPrices[lastLtf];
            double close = Bars.ClosePrices[lastLtf];
            double high = Bars.HighPrices[lastLtf];
            double low = Bars.LowPrices[lastLtf];
            double touchLevel = RequireZoneMidTouch
                ? (_activeZone.High + _activeZone.Low) / 2.0
                : (_activeZone.IsBullish ? _activeZone.High : _activeZone.Low);

            if (_activeZone.IsBullish)
            {
                if (low <= touchLevel) _activeZone.Touched = true;
                if (close < _activeZone.Low) { DropZone(); return; }
                if (_activeZone.Touched && close > open && close >= _activeZone.Low)
                    EnterMarket(TradeType.Buy);
            }
            else
            {
                if (high >= touchLevel) _activeZone.Touched = true;
                if (close > _activeZone.High) { DropZone(); return; }
                if (_activeZone.Touched && close < open && close <= _activeZone.High)
                    EnterMarket(TradeType.Sell);
            }
        }

        private void EnterMarket(TradeType type)
        {
            bool isLong = type == TradeType.Buy;
            double entry = isLong ? Symbol.Ask : Symbol.Bid;
            double stop = StopLevelFor(isLong);

            double slDist = isLong ? entry - stop : stop - entry;
            if (slDist <= 0) { DropZone(); return; }

            double slPips = slDist / Symbol.PipSize;
            double tpPips = slPips * TakeProfitRR;

            double volume = CalculatePositionSize(slPips);
            if (volume < Symbol.VolumeInUnitsMin)
            {
                Print("Volume {0} below min {1}, skipping.", volume, Symbol.VolumeInUnitsMin);
                DropZone();
                return;
            }

            var result = ExecuteMarketOrder(type, SymbolName, volume, TradeLabel, slPips, tpPips);
            if (result.IsSuccessful)
                Print("{0} filled vol={1} SL={2:F1}p TP={3:F1}p ({4:F1}R) zone={5}",
                    type, volume, slPips, tpPips, TakeProfitRR, _activeZone.Source);
            else
                Print("Order failed: {0}", result.Error);

            DropZone();
        }

        private void TryPlaceLimitAtZone()
        {
            if (Positions.FindAll(TradeLabel, SymbolName).Length >= MaxPositions) return;
            if (!CanTradeNow()) return;

            var zone = _activeZone;
            bool isLong = zone.IsBullish;
            double entry = LimitAtZoneMid ? (zone.High + zone.Low) / 2.0 : (isLong ? zone.High : zone.Low);

            // A limit must rest on the passive side of the market; otherwise wait for confirmation mode logic.
            if (isLong && Symbol.Bid <= entry) { Print("[limit] price already at/below zone, skipping limit placement."); return; }
            if (!isLong && Symbol.Ask >= entry) { Print("[limit] price already at/above zone, skipping limit placement."); return; }

            double stop = StopLevelFor(isLong);
            double slDist = isLong ? entry - stop : stop - entry;
            if (slDist <= 0) return;

            double slPips = slDist / Symbol.PipSize;
            double tpPips = slPips * TakeProfitRR;

            double volume = CalculatePositionSize(slPips);
            if (volume < Symbol.VolumeInUnitsMin)
            {
                Print("[limit] volume {0} below min {1}, skipping.", volume, Symbol.VolumeInUnitsMin);
                return;
            }

            var expiry = Server.Time + RemainingZoneLifetime();
            var result = PlaceLimitOrder(isLong ? TradeType.Buy : TradeType.Sell, SymbolName, volume, entry,
                TradeLabel, slPips, tpPips, ProtectionType.Relative, expiry);

            if (result.IsSuccessful)
            {
                zone.OrderPlaced = true;
                Print("[limit] {0} @ {1:F5} SL={2:F1}p TP={3:F1}p expires {4:yyyy-MM-dd HH:mm} UTC",
                    isLong ? "BUY" : "SELL", entry, slPips, tpPips, expiry);
            }
            else
                Print("[limit] order failed: {0}", result.Error);
        }

        private double StopLevelFor(bool isLong)
        {
            // Stop beyond both the sweep extreme and the zone's far edge, plus buffer.
            double anchor = isLong
                ? Math.Min(_activeZone.SweepLevel, _activeZone.Low)
                : Math.Max(_activeZone.SweepLevel, _activeZone.High);
            return isLong
                ? anchor - StopBufferPips * Symbol.PipSize
                : anchor + StopBufferPips * Symbol.PipSize;
        }

        private TimeSpan RemainingZoneLifetime()
        {
            var setupSpan = _setupBars.Count >= 2
                ? _setupBars.OpenTimes[_setupBars.Count - 1] - _setupBars.OpenTimes[_setupBars.Count - 2]
                : TimeSpan.FromMinutes(15);
            int lastClosed = _setupBars.Count - 2;
            int barsLeft = Math.Max(1, ZoneValidBars - (lastClosed - _activeZone.CreatedSetupIndex));
            var life = TimeSpan.FromTicks(setupSpan.Ticks * barsLeft);
            return life < TimeSpan.FromMinutes(10) ? TimeSpan.FromMinutes(10) : life;
        }

        private void CancelLabelledPendingOrders()
        {
            foreach (var po in PendingOrders.Where(o => o.Label == TradeLabel && o.SymbolName == SymbolName).ToArray())
                CancelPendingOrder(po);
        }

        // =====================================================================
        // Trade management: breakeven + time stop
        // =====================================================================

        private void ManageOpenPositions()
        {
            foreach (var p in Positions.FindAll(TradeLabel, SymbolName))
            {
                if (MaxBarsInTrade > 0 && _entryBarById.TryGetValue(p.Id, out int entryBar)
                    && Bars.Count - entryBar >= MaxBarsInTrade)
                {
                    Print("Time stop: closing {0} after {1} chart bars.", p.TradeType, MaxBarsInTrade);
                    ClosePosition(p);
                    continue;
                }

                if (BreakevenTriggerR <= 0 || _breakevenDone.Contains(p.Id)) continue;
                if (!_initialRiskById.TryGetValue(p.Id, out double risk) || risk <= 0) continue;

                bool isLong = p.TradeType == TradeType.Buy;
                double current = isLong ? Symbol.Bid : Symbol.Ask;
                double profitDist = isLong ? current - p.EntryPrice : p.EntryPrice - current;

                if (profitDist >= BreakevenTriggerR * risk)
                {
                    var result = p.ModifyStopLossPrice(p.EntryPrice);
                    if (result.IsSuccessful)
                    {
                        _breakevenDone.Add(p.Id);
                        Print("Breakeven: SL moved to entry on {0} after +{1:F1}R.", p.TradeType, BreakevenTriggerR);
                    }
                }
            }
        }

        // =====================================================================
        // Gating (killzone / spread / risk caps)
        // =====================================================================

        private bool CanTradeNow()
        {
            ResetDailyStatsIfNeeded();

            if (UseKillzoneFilter && !IsInActiveKillzone(Server.Time)) return false;

            double spreadPips = Symbol.Spread / Symbol.PipSize;
            if (spreadPips > MaxSpreadPips) return false;

            double maxLoss = -Math.Abs(Account.Balance * (MaxDailyLossPercent / 100.0));
            if (_dailyRealizedPnl <= maxLoss)
            {
                Print("Daily loss cap hit ({0:F2}). Halted for the day.", _dailyRealizedPnl);
                return false;
            }

            if (_dailyTradeCount >= MaxTradesPerDay) return false;
            if (_consecutiveLosses >= MaxConsecutiveLosses)
            {
                Print("Consecutive loss cap reached ({0}). Halted.", _consecutiveLosses);
                return false;
            }
            return true;
        }

        private bool IsInActiveKillzone(DateTime utc)
        {
            var ny = UtcToNyLocal(utc);
            double h = ny.Hour + ny.Minute / 60.0;

            if (TradeLondonKz && h >= 2.0 && h < 5.0) return true;
            if (TradeNyAmKz && h >= 7.0 && h < 10.0) return true;
            if (TradeNyPmKz && h >= 13.5 && h < 16.0) return true;
            return false;
        }

        private void ResetDailyStatsIfNeeded()
        {
            var today = Server.Time.Date;
            if (today != _currentDay)
            {
                _currentDay = today;
                _dailyRealizedPnl = 0;
                _dailyTradeCount = 0;
                // _consecutiveLosses persists across days; cleared on first win.
            }
        }

        // =====================================================================
        // Sizing + position bookkeeping
        // =====================================================================

        private double CalculatePositionSize(double slPips)
        {
            // Fixed-fractional sizing: units = (balance * risk%) / (SL_pips * pip_value_per_unit)
            double riskAmount = Account.Balance * (RiskPercent / 100.0);
            double pipValue = Symbol.PipValue;
            if (pipValue <= 0 || slPips <= 0) return 0;

            double raw = riskAmount / (slPips * pipValue);
            if (raw > Symbol.VolumeInUnitsMax) raw = Symbol.VolumeInUnitsMax;
            return Symbol.NormalizeVolumeInUnits(raw, RoundingMode.Down);
        }

        private void OnPositionOpened(PositionOpenedEventArgs args)
        {
            var p = args.Position;
            if (p.Label != TradeLabel || p.SymbolName != SymbolName) return;

            ResetDailyStatsIfNeeded();
            _dailyTradeCount++;
            _entryBarById[p.Id] = Bars.Count;
            if (p.StopLoss.HasValue)
                _initialRiskById[p.Id] = Math.Abs(p.EntryPrice - p.StopLoss.Value);

            // A pending fill consumes the zone the same way a market entry does.
            if (_activeZone != null && _activeZone.OrderPlaced)
                _activeZone = null;
        }

        private void OnPositionClosed(PositionClosedEventArgs args)
        {
            var p = args.Position;
            if (p.Label != TradeLabel || p.SymbolName != SymbolName) return;

            ResetDailyStatsIfNeeded();
            _dailyRealizedPnl += p.NetProfit;

            if (p.NetProfit < 0) _consecutiveLosses++;
            else if (p.NetProfit > 0) _consecutiveLosses = 0;

            _initialRiskById.Remove(p.Id);
            _entryBarById.Remove(p.Id);
            _breakevenDone.Remove(p.Id);

            Print("Closed {0}: PnL={1:F2} dayPnL={2:F2} consLosses={3}",
                p.TradeType, p.NetProfit, _dailyRealizedPnl, _consecutiveLosses);
        }
    }

    // =========================================================================
    // Supporting types (Smc-prefixed to avoid clashes when bots share an assembly)
    // =========================================================================

    public enum SmcBias { Neutral, Bullish, Bearish }

    public enum SmcZoneMode { FvgOnly, OrderBlockOnly, FvgOrOrderBlock, OverlapOnly }

    public enum SmcEntryMode { LtfConfirmation, LimitAtZone }

    public class SmcZone
    {
        public double High;
        public double Low;
        public bool IsBullish;
        public int CreatedSetupIndex;
        public double SweepLevel;
        public string Source;
        public bool Touched;
        public bool OrderPlaced;
    }
}
