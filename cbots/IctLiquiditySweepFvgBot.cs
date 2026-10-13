using System;
using System.Collections.Generic;
using System.Linq;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    // ICT Liquidity-Sweep / Displacement / FVG cBot — STRICTLY GBP/JPY on Pepperstone cTrader.
    // ---------------------------------------------------------------------------------
    // HTF:          PDH / PDL / PWH / PWL from Daily + Weekly bars. Optional H4 FVG confluence.
    // MTF (M15):    Sweep (raid of key liquidity) -> displacement (>=N ATR-strong candles) -> FVG.
    // LTF (chart):  Retrace >= 50% into MTF FVG + confirmation candle = entry.
    // TP:           Nearest prior-liquidity pool inside [MinRR, MaxRR] (PWH/L, PDH/L, session H/L).
    //               Falls back to FallbackRR if nothing qualifies, or skip if RequireLiquidityTarget.
    // Risk:         Fixed-fractional sizing, spread filter, daily loss cap, consecutive-loss halt,
    //               max trades/day, min R:R floor. Optional chandelier trailing stop (MTF ATR,
    //               tighten-only, activates after +N R; fixed TP stays in place).
    //
    // ---- Pepperstone / cTrader timezone notes ---------------------------------------
    // Pepperstone's *displayed* cTrader server time is GMT+2 (winter) / GMT+3 (summer) so
    // that daily candles close at New York 17:00 — the standard FX daily close used by ICT.
    // At the cAlgo API level, however, cTrader has migrated to UTC: when
    // [Robot(TimeZone = TimeZones.UTC)] is set, Server.Time AND Bars.OpenTimes are pure UTC,
    // and Daily bars are NY-anchored (start 17:00 NY) on every broker. So:
    //   * PDH / PDL from Daily bars are already NY-aligned (correct for ICT) — no manual fix needed.
    //   * Killzones are stored as NY local hours (ICT convention) and the bot converts
    //     UTC -> NY local on every check, applying US DST automatically.
    //   * UK BST / GMT is also computed locally for log labelling (UK is the user's home zone).
    //
    // ---- DST drift handling ---------------------------------------------------------
    // US enters DST on the 2nd Sunday of March and leaves on the 1st Sunday of November.
    // UK enters BST on the last Sunday of March and leaves on the last Sunday of October.
    // For ~3 weeks in March and ~1 week in Nov the UK and US are out-of-sync, so the
    // "UK XX:XX" hint in killzone parameter labels can drift by 1 hour during those windows.
    // The actual killzone behaviour is anchored to NY (correct for ICT) — only the UK
    // label drifts. Audit log on startup prints the current offsets so you can verify.
    // ---------------------------------------------------------------------------------
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class IctLiquiditySweepFvgBot : Robot
    {
        #region Parameters - Timeframes

        [Parameter("Higher Timeframe", Group = "Timeframes", DefaultValue = "Hour4")]
        public TimeFrame HighTimeFrame { get; set; }

        [Parameter("Mid Timeframe", Group = "Timeframes", DefaultValue = "Minute15")]
        public TimeFrame MidTimeFrame { get; set; }

        #endregion

        #region Parameters - Setup (defaults tuned for GBP/JPY M15)

        [Parameter("Sweep Lookback (MTF bars)", Group = "Setup", DefaultValue = 30, MinValue = 10, MaxValue = 200)]
        public int SweepLookback { get; set; }

        [Parameter("Swing Length (MTF bars)", Group = "Setup", DefaultValue = 5, MinValue = 3, MaxValue = 20)]
        public int SwingLength { get; set; }

        [Parameter("Displacement ATR Multiplier", Group = "Setup", DefaultValue = 1.2, MinValue = 0.3, MaxValue = 5.0)]
        public double DisplacementAtrMult { get; set; }

        [Parameter("Min Displacement Candles", Group = "Setup", DefaultValue = 2, MinValue = 1, MaxValue = 5)]
        public int MinDisplacementCandles { get; set; }

        [Parameter("ATR Period", Group = "Setup", DefaultValue = 14, MinValue = 5, MaxValue = 50)]
        public int AtrPeriod { get; set; }

        [Parameter("FVG Validity (MTF bars)", Group = "Setup", DefaultValue = 20, MinValue = 3, MaxValue = 100)]
        public int FvgValidBars { get; set; }

        #endregion

        #region Parameters - Confluence

        [Parameter("Require Key Liquidity Sweep", Group = "Confluence", DefaultValue = true)]
        public bool RequireKeyLiquiditySweep { get; set; }

        [Parameter("Require HTF FVG Confluence", Group = "Confluence", DefaultValue = false)]
        public bool RequireHtfFvg { get; set; }

        #endregion

        #region Parameters - Killzones (NY local time; DST handled automatically)

        [Parameter("Use Killzone Filter", Group = "Killzones", DefaultValue = true)]
        public bool UseKillzoneFilter { get; set; }

        [Parameter("Asian KZ (NY 20:00-00:00)", Group = "Killzones", DefaultValue = false)]
        public bool TradeAsianKz { get; set; }

        [Parameter("London KZ (NY 02:00-05:00 / UK 07:00-10:00)", Group = "Killzones", DefaultValue = true)]
        public bool TradeLondonKz { get; set; }

        [Parameter("NY AM KZ (NY 07:00-10:00 / UK 12:00-15:00)", Group = "Killzones", DefaultValue = true)]
        public bool TradeNyAmKz { get; set; }

        [Parameter("NY PM KZ (NY 13:30-16:00 / UK 18:30-21:00)", Group = "Killzones", DefaultValue = false)]
        public bool TradeNyPmKz { get; set; }

        [Parameter("London Close KZ (NY 10:00-12:00 / UK 15:00-17:00)", Group = "Killzones", DefaultValue = false)]
        public bool TradeLondonCloseKz { get; set; }

        #endregion

        #region Parameters - Risk Management (GBP/JPY tuned)

        [Parameter("Risk Per Trade (%)", Group = "Risk", DefaultValue = 0.5, MinValue = 0.05, MaxValue = 2.0)]
        public double RiskPercent { get; set; }

        [Parameter("Stop Buffer (pips)", Group = "Risk", DefaultValue = 8.0, MinValue = 0.0, MaxValue = 50.0)]
        public double StopBufferPips { get; set; }

        [Parameter("Min R:R", Group = "Risk", DefaultValue = 2.0, MinValue = 1.0, MaxValue = 10.0)]
        public double MinRR { get; set; }

        [Parameter("Max R:R", Group = "Risk", DefaultValue = 4.0, MinValue = 1.5, MaxValue = 20.0)]
        public double MaxRR { get; set; }

        [Parameter("Fallback R:R (no liquidity target in range)", Group = "Risk", DefaultValue = 2.5, MinValue = 1.0, MaxValue = 10.0)]
        public double FallbackRR { get; set; }

        [Parameter("Require Liquidity Target (reject if none)", Group = "Risk", DefaultValue = false)]
        public bool RequireLiquidityTarget { get; set; }

        [Parameter("TP Buffer Before Liquidity (pips)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.0, MaxValue = 30.0)]
        public double TpBufferPips { get; set; }

        // GBP/JPY on Pepperstone Razor: typical ~1.5-2.0 pips during London/NY, can spike to 4+ Asian.
        [Parameter("Max Spread (pips)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.0, MaxValue = 50.0)]
        public double MaxSpreadPips { get; set; }

        [Parameter("Max Daily Loss (% of balance)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.5, MaxValue = 20.0)]
        public double MaxDailyLossPercent { get; set; }

        [Parameter("Max Consecutive Losses", Group = "Risk", DefaultValue = 3, MinValue = 1, MaxValue = 20)]
        public int MaxConsecutiveLosses { get; set; }

        [Parameter("Max Trades Per Day", Group = "Risk", DefaultValue = 5, MinValue = 1, MaxValue = 50)]
        public int MaxTradesPerDay { get; set; }

        // Chandelier-style trail: after +ActivationR, SL follows price at TrailAtrMult * MTF ATR.
        // Tighten-only; the fixed TP stays in place, whichever is hit first ends the trade.
        [Parameter("Use Trailing Stop", Group = "Risk", DefaultValue = true)]
        public bool UseTrailingStop { get; set; }

        [Parameter("Trail Activation (R)", Group = "Risk", DefaultValue = 1.0, MinValue = 0.0, MaxValue = 5.0)]
        public double TrailActivationR { get; set; }

        [Parameter("Trail Distance (MTF ATR mult)", Group = "Risk", DefaultValue = 2.0, MinValue = 0.5, MaxValue = 10.0)]
        public double TrailAtrMult { get; set; }

        #endregion

        #region Parameters - Meta

        [Parameter("Trade Label", Group = "Meta", DefaultValue = "ICT_LS_FVG")]
        public string TradeLabel { get; set; }

        // Hard-restrict to GBP/JPY. Set false only if you knowingly want to test on another pair.
        [Parameter("Restrict To GBP/JPY", Group = "Meta", DefaultValue = true)]
        public bool RestrictToGbpJpy { get; set; }

        [Parameter("Allow Longs", Group = "Meta", DefaultValue = true)]
        public bool AllowLongs { get; set; }

        [Parameter("Allow Shorts", Group = "Meta", DefaultValue = true)]
        public bool AllowShorts { get; set; }

        [Parameter("Max Concurrent Positions", Group = "Meta", DefaultValue = 1, MinValue = 1, MaxValue = 5)]
        public int MaxPositions { get; set; }

        #endregion

        // --- State ---
        private Bars _htfBars, _mtfBars, _dailyBars, _weeklyBars, _m1Bars;
        private AverageTrueRange _mtfAtr;

        private FvgZone _activeFvg;
        private int _activeFvgMtfIndex;

        private double _pdh = double.NaN, _pdl = double.NaN;
        private double _pwh = double.NaN, _pwl = double.NaN;

        // Session windows defined in NY local time (ICT convention).
        private readonly SessionTracker _asian = new SessionTracker("Asian", 20.0, 5.0);
        private readonly SessionTracker _london = new SessionTracker("London", 3.0, 11.0);
        private readonly SessionTracker _ny = new SessionTracker("NewYork", 8.0, 16.0);

        private DateTime _currentDay = DateTime.MinValue;
        private double _dailyRealizedPnl;
        private int _dailyTradeCount;
        private int _consecutiveLosses;

        // Initial entry-to-SL distance per position, captured at open (SL moves as we trail).
        private readonly Dictionary<long, double> _initialRiskById = new Dictionary<long, double>();

        // =====================================================================
        // Lifecycle
        // =====================================================================

        protected override void OnStart()
        {
            if (MinRR > MaxRR)
            {
                Print("Min R:R > Max R:R, aborting. Fix parameters.");
                Stop();
                return;
            }

            // -- Hard symbol restriction: this bot is calibrated for GBP/JPY only. --
            if (RestrictToGbpJpy && !IsGbpJpySymbol(SymbolName))
            {
                Print("This bot is restricted to GBP/JPY. Current symbol: '{0}'. Aborting.", SymbolName);
                Print("Set 'Restrict To GBP/JPY' = false to override (defaults are NOT tuned for other pairs).");
                Stop();
                return;
            }

            _htfBars = MarketData.GetBars(HighTimeFrame);
            _mtfBars = MarketData.GetBars(MidTimeFrame);
            _dailyBars = MarketData.GetBars(TimeFrame.Daily);
            _weeklyBars = MarketData.GetBars(TimeFrame.Weekly);
            _m1Bars = MarketData.GetBars(TimeFrame.Minute);
            _mtfAtr = Indicators.AverageTrueRange(_mtfBars, AtrPeriod, MovingAverageType.Simple);

            _mtfBars.BarClosed += OnMtfBarClosed;
            _m1Bars.BarClosed += OnM1BarClosed;
            Positions.Opened += OnPositionOpened;
            Positions.Closed += OnPositionClosed;

            // Seed risk for positions that survived a bot restart (approximate if SL already moved).
            foreach (var p in Positions.FindAll(TradeLabel, SymbolName))
                if (p.StopLoss.HasValue)
                    _initialRiskById[p.Id] = Math.Abs(p.EntryPrice - p.StopLoss.Value);

            BackfillSessions();
            UpdatePdhPdl();
            UpdatePwhPwl();
            _currentDay = Server.Time.Date;

            LogDstAudit();

            Print("ICT bot started. {0} HTF={1} MTF={2} PDH={3:F3} PDL={4:F3} PWH={5:F3} PWL={6:F3}",
                SymbolName, HighTimeFrame, MidTimeFrame, _pdh, _pdl, _pwh, _pwl);
            Print("PipSize={0} PipValue={1} VolMin={2} VolMax={3} VolStep={4}",
                Symbol.PipSize, Symbol.PipValue, Symbol.VolumeInUnitsMin, Symbol.VolumeInUnitsMax, Symbol.VolumeInUnitsStep);
        }

        private static bool IsGbpJpySymbol(string symbolName)
        {
            if (string.IsNullOrEmpty(symbolName)) return false;
            var s = symbolName.ToUpperInvariant();
            // Tolerate broker suffixes like "GBPJPY.s", "GBPJPY-Pro", "GBP/JPY", etc.
            s = s.Replace("/", "").Replace("_", "").Replace("-", "");
            return s.StartsWith("GBPJPY");
        }

        private void LogDstAudit()
        {
            var utc = Server.Time;
            bool ukBst = IsUkBst(utc);
            bool usEdt = IsUsEdt(utc);
            int ukOffset = ukBst ? 1 : 0;
            int nyOffset = usEdt ? -4 : -5;
            Print("DST audit @ {0:yyyy-MM-dd HH:mm} UTC | UK={1} (UTC{2:+0;-0}) | NY={3} (UTC{4:+0;-0})",
                utc, ukBst ? "BST" : "GMT", ukOffset, usEdt ? "EDT" : "EST", nyOffset);
            // Warn during the 1-3 week mismatch windows (mid-March, early November)
            // when "UK XX:XX" labels in killzone parameter names are 1h off vs reality.
            if (ukBst != usEdt)
                Print("NOTE: UK and US DST are out of sync this week. Killzones still anchor to NY (correct), but UK-time hints in the parameter labels are shifted by 1 hour.");
        }

        protected override void OnStop()
        {
            _mtfBars.BarClosed -= OnMtfBarClosed;
            _m1Bars.BarClosed -= OnM1BarClosed;
            Positions.Opened -= OnPositionOpened;
            Positions.Closed -= OnPositionClosed;
        }

        // =====================================================================
        // DST helpers (self-contained, no TimeZoneInfo dependency)
        //   UK BST : last Sun Mar 01:00 UTC -> last Sun Oct 01:00 UTC
        //   US EDT : 2nd Sun Mar 07:00 UTC  -> 1st Sun Nov 06:00 UTC
        // =====================================================================

        private static DateTime LastSundayOfMonth(int year, int month)
        {
            var last = new DateTime(year, month, DateTime.DaysInMonth(year, month));
            int back = ((int)last.DayOfWeek - (int)DayOfWeek.Sunday + 7) % 7;
            return last.AddDays(-back);
        }

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

        private static bool IsUkBst(DateTime utc)
        {
            int y = utc.Year;
            var start = LastSundayOfMonth(y, 3).AddHours(1);
            var end = LastSundayOfMonth(y, 10).AddHours(1);
            return utc >= start && utc < end;
        }

        private static DateTime UtcToNyLocal(DateTime utc)
        {
            return utc.AddHours(IsUsEdt(utc) ? -4 : -5);
        }

        // =====================================================================
        // Session / HTF context
        // =====================================================================

        private void BackfillSessions()
        {
            int wanted = Math.Min(_m1Bars.Count, 3 * 24 * 60);
            int start = Math.Max(0, _m1Bars.Count - 1 - wanted);
            for (int i = start; i < _m1Bars.Count - 1; i++)
                FeedSessions(_m1Bars.OpenTimes[i], _m1Bars.HighPrices[i], _m1Bars.LowPrices[i]);
        }

        private void OnM1BarClosed(BarClosedEventArgs args)
        {
            int idx = _m1Bars.Count - 2;
            if (idx < 0) return;
            FeedSessions(_m1Bars.OpenTimes[idx], _m1Bars.HighPrices[idx], _m1Bars.LowPrices[idx]);
        }

        private void FeedSessions(DateTime utc, double high, double low)
        {
            var ny = UtcToNyLocal(utc);
            _asian.Update(ny, high, low);
            _london.Update(ny, high, low);
            _ny.Update(ny, high, low);
        }

        private void UpdatePdhPdl()
        {
            if (_dailyBars.Count < 2) return;
            int last = _dailyBars.Count - 2;
            _pdh = _dailyBars.HighPrices[last];
            _pdl = _dailyBars.LowPrices[last];
        }

        private void UpdatePwhPwl()
        {
            if (_weeklyBars.Count < 2) return;
            int last = _weeklyBars.Count - 2;
            _pwh = _weeklyBars.HighPrices[last];
            _pwl = _weeklyBars.LowPrices[last];
        }

        // =====================================================================
        // MTF setup detection
        // =====================================================================

        private void OnMtfBarClosed(BarClosedEventArgs args)
        {
            UpdatePdhPdl();
            UpdatePwhPwl();
            ScanMtfSetup();
        }

        private void ScanMtfSetup()
        {
            if (_mtfBars.Count < SweepLookback + SwingLength + 5) return;
            int lastClosed = _mtfBars.Count - 2;

            if (AllowLongs)
            {
                var longFvg = TryFindSetup(lastClosed, isLong: true);
                if (longFvg != null)
                {
                    _activeFvg = longFvg;
                    _activeFvgMtfIndex = lastClosed;
                    Print("[MTF LONG setup] FVG [{0:F3}-{1:F3}] sweep={2:F3}",
                        longFvg.Low, longFvg.High, longFvg.SweepLevel);
                    return;
                }
            }

            if (AllowShorts)
            {
                var shortFvg = TryFindSetup(lastClosed, isLong: false);
                if (shortFvg != null)
                {
                    _activeFvg = shortFvg;
                    _activeFvgMtfIndex = lastClosed;
                    Print("[MTF SHORT setup] FVG [{0:F3}-{1:F3}] sweep={2:F3}",
                        shortFvg.Low, shortFvg.High, shortFvg.SweepLevel);
                }
            }
        }

        private FvgZone TryFindSetup(int lastBar, bool isLong)
        {
            // 1. Liquidity sweep: recent candle pierces a prior SwingLength swing, closes back inside.
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
                        if (_mtfBars.LowPrices[j] < priorLow) priorLow = _mtfBars.LowPrices[j];

                    if (_mtfBars.LowPrices[i] < priorLow && _mtfBars.ClosePrices[i] > priorLow)
                    {
                        sweepBarIdx = i;
                        break;
                    }
                }
                else
                {
                    double priorHigh = double.MinValue;
                    for (int j = priorFrom; j <= priorTo; j++)
                        if (_mtfBars.HighPrices[j] > priorHigh) priorHigh = _mtfBars.HighPrices[j];

                    if (_mtfBars.HighPrices[i] > priorHigh && _mtfBars.ClosePrices[i] < priorHigh)
                    {
                        sweepBarIdx = i;
                        break;
                    }
                }
            }

            if (sweepBarIdx < 0) return null;

            double sweepWick = isLong
                ? _mtfBars.LowPrices[sweepBarIdx]
                : _mtfBars.HighPrices[sweepBarIdx];
            double sweepClose = _mtfBars.ClosePrices[sweepBarIdx];

            // 1b. Optional: sweep must have raided a KEY liquidity level (not just a local swing).
            if (RequireKeyLiquiditySweep)
            {
                double[] keys = BuildKeyLiquidityLevels(isLong);
                bool raidedKey = false;
                foreach (var level in keys)
                {
                    if (double.IsNaN(level)) continue;
                    if (isLong && sweepWick < level && sweepClose > level) { raidedKey = true; break; }
                    if (!isLong && sweepWick > level && sweepClose < level) { raidedKey = true; break; }
                }
                if (!raidedKey) return null;
            }

            // 2. Displacement: strong-body candles after the sweep, in direction.
            int dispStart = sweepBarIdx + 1;
            int dispEnd = lastBar;
            if (dispEnd < dispStart + 1) return null;

            double atr = _mtfAtr.Result[sweepBarIdx];
            if (double.IsNaN(atr) || atr <= 0) return null;
            double minBody = atr * DisplacementAtrMult;

            int strongCount = 0;
            for (int i = dispStart; i <= dispEnd; i++)
            {
                double body = Math.Abs(_mtfBars.ClosePrices[i] - _mtfBars.OpenPrices[i]);
                bool bullish = _mtfBars.ClosePrices[i] > _mtfBars.OpenPrices[i];
                if (body >= minBody && ((isLong && bullish) || (!isLong && !bullish)))
                    strongCount++;
            }
            if (strongCount < MinDisplacementCandles) return null;

            // 3. FVG: first 3-bar gap inside the displacement window.
            FvgZone fvg = null;
            for (int i = dispStart + 1; i <= dispEnd; i++)
            {
                int i2 = i - 2;
                if (i2 < 0) continue;
                double h2 = _mtfBars.HighPrices[i2];
                double l2 = _mtfBars.LowPrices[i2];
                double h0 = _mtfBars.HighPrices[i];
                double l0 = _mtfBars.LowPrices[i];

                if (isLong && l0 > h2)
                {
                    fvg = new FvgZone { High = l0, Low = h2, IsBullish = true, CreatedBarIndex = i };
                    break;
                }
                if (!isLong && h0 < l2)
                {
                    fvg = new FvgZone { High = l2, Low = h0, IsBullish = false, CreatedBarIndex = i };
                    break;
                }
            }
            if (fvg == null) return null;

            // 4. Validity: discard if any later MTF bar closed through the FVG.
            for (int j = fvg.CreatedBarIndex + 1; j <= lastBar; j++)
            {
                if (isLong && _mtfBars.ClosePrices[j] < fvg.Low) return null;
                if (!isLong && _mtfBars.ClosePrices[j] > fvg.High) return null;
            }

            fvg.SweepLevel = sweepWick;

            // 5. Optional HTF FVG confluence.
            if (RequireHtfFvg && !HasHtfFvgConfluence(fvg)) return null;

            return fvg;
        }

        private double[] BuildKeyLiquidityLevels(bool isLong)
        {
            return isLong
                ? new[]
                {
                    _pdl, _pwl,
                    _asian.PrevLow, _london.PrevLow, _ny.PrevLow,
                    _asian.CurrentLow, _london.CurrentLow, _ny.CurrentLow
                }
                : new[]
                {
                    _pdh, _pwh,
                    _asian.PrevHigh, _london.PrevHigh, _ny.PrevHigh,
                    _asian.CurrentHigh, _london.CurrentHigh, _ny.CurrentHigh
                };
        }

        private bool HasHtfFvgConfluence(FvgZone mtfFvg)
        {
            if (_htfBars.Count < 4) return false;
            int last = _htfBars.Count - 2;
            int from = Math.Max(2, last - 30);

            for (int i = from; i <= last; i++)
            {
                double h2 = _htfBars.HighPrices[i - 2];
                double l2 = _htfBars.LowPrices[i - 2];
                double h0 = _htfBars.HighPrices[i];
                double l0 = _htfBars.LowPrices[i];

                if (mtfFvg.IsBullish && l0 > h2)
                {
                    if (Math.Min(l0, mtfFvg.High) > Math.Max(h2, mtfFvg.Low)) return true;
                }
                else if (!mtfFvg.IsBullish && h0 < l2)
                {
                    if (Math.Min(l2, mtfFvg.High) > Math.Max(h0, mtfFvg.Low)) return true;
                }
            }
            return false;
        }

        // =====================================================================
        // LTF execution
        // =====================================================================

        protected override void OnBar()
        {
            UpdateTrailingStops();

            if (_activeFvg == null) return;

            int mtfElapsed = (_mtfBars.Count - 2) - _activeFvgMtfIndex;
            if (mtfElapsed > FvgValidBars) { _activeFvg = null; return; }

            if (!CanTradeNow()) return;
            if (Positions.FindAll(TradeLabel, SymbolName).Length >= MaxPositions) return;

            int lastLtf = Bars.Count - 2;
            if (lastLtf < 1) return;

            double open = Bars.OpenPrices[lastLtf];
            double close = Bars.ClosePrices[lastLtf];
            double high = Bars.HighPrices[lastLtf];
            double low = Bars.LowPrices[lastLtf];
            double mid = (_activeFvg.High + _activeFvg.Low) / 2.0;

            if (_activeFvg.IsBullish)
            {
                if (low <= mid) _activeFvg.MidTouched = true;
                if (close < _activeFvg.Low) { _activeFvg = null; return; }
                if (!_activeFvg.MidTouched) return;
                if (close > open && close >= _activeFvg.Low) PlaceTrade(TradeType.Buy);
            }
            else
            {
                if (high >= mid) _activeFvg.MidTouched = true;
                if (close > _activeFvg.High) { _activeFvg = null; return; }
                if (!_activeFvg.MidTouched) return;
                if (close < open && close <= _activeFvg.High) PlaceTrade(TradeType.Sell);
            }
        }

        // =====================================================================
        // Trailing stop (chandelier on MTF ATR, tighten-only)
        // =====================================================================

        private void UpdateTrailingStops()
        {
            if (!UseTrailingStop) return;

            double atr = _mtfAtr.Result.Last(1);
            if (double.IsNaN(atr) || atr <= 0) return;
            double trailDist = TrailAtrMult * atr;

            foreach (var p in Positions.FindAll(TradeLabel, SymbolName))
            {
                if (!_initialRiskById.TryGetValue(p.Id, out double risk) || risk <= 0) continue;

                bool isLong = p.TradeType == TradeType.Buy;
                double current = isLong ? Symbol.Bid : Symbol.Ask;
                double profitDist = isLong ? current - p.EntryPrice : p.EntryPrice - current;
                if (profitDist < TrailActivationR * risk) continue;

                double candidate = isLong ? current - trailDist : current + trailDist;

                // Tighten-only, and only when the move is at least a pip — avoids modify spam.
                if (p.StopLoss.HasValue)
                {
                    if (isLong && candidate <= p.StopLoss.Value + Symbol.PipSize) continue;
                    if (!isLong && candidate >= p.StopLoss.Value - Symbol.PipSize) continue;
                }

                var result = p.ModifyStopLossPrice(candidate);
                if (result.IsSuccessful)
                    Print("Trail: {0} SL -> {1:F3} ({2:F1}p behind, +{3:F2}R)",
                        p.TradeType, candidate, trailDist / Symbol.PipSize, profitDist / risk);
                else
                    Print("Trail modify failed: {0}", result.Error);
            }
        }

        // =====================================================================
        // Gating (session / spread / risk caps)
        // =====================================================================

        private bool CanTradeNow()
        {
            ResetDailyStatsIfNeeded();

            if (UseKillzoneFilter && !IsInActiveKillzone(Server.Time)) return false;

            double spreadPips = Symbol.Spread / Symbol.PipSize;
            if (spreadPips > MaxSpreadPips)
            {
                Print("Spread {0:F2}p > cap {1:F2}p, skipping.", spreadPips, MaxSpreadPips);
                return false;
            }

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

            if (TradeAsianKz && h >= 20.0 && h < 24.0) return true;
            if (TradeLondonKz && h >= 2.0 && h < 5.0) return true;
            if (TradeNyAmKz && h >= 7.0 && h < 10.0) return true;
            if (TradeNyPmKz && h >= 13.5 && h < 16.0) return true;
            if (TradeLondonCloseKz && h >= 10.0 && h < 12.0) return true;
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
        // Liquidity-based TP + execution
        // =====================================================================

        private double? FindLiquidityTp(double entry, double sl, bool isLong)
        {
            double slDist = Math.Abs(entry - sl);
            if (slDist <= 0) return null;

            double minTp = isLong ? entry + MinRR * slDist : entry - MinRR * slDist;
            double maxTp = isLong ? entry + MaxRR * slDist : entry - MaxRR * slDist;

            var candidates = new List<(double level, string name)>();
            void Add(double level, string name)
            {
                if (double.IsNaN(level) || level <= 0) return;
                if (isLong)
                {
                    if (level >= minTp && level <= maxTp) candidates.Add((level, name));
                }
                else
                {
                    if (level <= minTp && level >= maxTp) candidates.Add((level, name));
                }
            }

            if (isLong)
            {
                Add(_pwh, "PWH");
                Add(_pdh, "PDH");
                Add(_asian.PrevHigh, "AsianPrevH");
                Add(_london.PrevHigh, "LondonPrevH");
                Add(_ny.PrevHigh, "NYPrevH");
                Add(_asian.CurrentHigh, "AsianCurH");
                Add(_london.CurrentHigh, "LondonCurH");
                Add(_ny.CurrentHigh, "NYCurH");
            }
            else
            {
                Add(_pwl, "PWL");
                Add(_pdl, "PDL");
                Add(_asian.PrevLow, "AsianPrevL");
                Add(_london.PrevLow, "LondonPrevL");
                Add(_ny.PrevLow, "NYPrevL");
                Add(_asian.CurrentLow, "AsianCurL");
                Add(_london.CurrentLow, "LondonCurL");
                Add(_ny.CurrentLow, "NYCurL");
            }

            if (candidates.Count == 0) return null;

            var best = isLong
                ? candidates.OrderBy(c => c.level).First()
                : candidates.OrderByDescending(c => c.level).First();

            Print("TP target = {0} @ {1:F3}", best.name, best.level);
            return best.level;
        }

        private void PlaceTrade(TradeType type)
        {
            if (_activeFvg == null) return;

            bool isLong = type == TradeType.Buy;
            double entry = isLong ? Symbol.Ask : Symbol.Bid;
            double stop = isLong
                ? _activeFvg.SweepLevel - StopBufferPips * Symbol.PipSize
                : _activeFvg.SweepLevel + StopBufferPips * Symbol.PipSize;

            double slDist = Math.Abs(entry - stop);
            if (slDist <= 0) { _activeFvg = null; return; }

            double? liqTp = FindLiquidityTp(entry, stop, isLong);
            double tp;

            if (liqTp.HasValue)
            {
                double buffer = TpBufferPips * Symbol.PipSize;
                tp = isLong ? liqTp.Value - buffer : liqTp.Value + buffer;
            }
            else
            {
                if (RequireLiquidityTarget)
                {
                    Print("No liquidity target in R:R [{0:F2}-{1:F2}], skipping.", MinRR, MaxRR);
                    _activeFvg = null;
                    return;
                }
                tp = isLong ? entry + FallbackRR * slDist : entry - FallbackRR * slDist;
            }

            double tpDist = Math.Abs(entry - tp);
            double actualRr = tpDist / slDist;
            if (actualRr < MinRR)
            {
                Print("R:R {0:F2} < min {1:F2}, skipping.", actualRr, MinRR);
                _activeFvg = null;
                return;
            }

            double slPips = slDist / Symbol.PipSize;
            double tpPips = tpDist / Symbol.PipSize;

            double volume = CalculatePositionSize(slPips);
            if (volume < Symbol.VolumeInUnitsMin)
            {
                Print("Volume {0} below min {1}, skipping.", volume, Symbol.VolumeInUnitsMin);
                _activeFvg = null;
                return;
            }

            var result = ExecuteMarketOrder(type, SymbolName, volume, TradeLabel, slPips, tpPips);
            if (result.IsSuccessful)
            {
                _dailyTradeCount++;
                Print("{0} filled vol={1} SL={2:F1}p TP={3:F1}p R:R={4:F2} entry={5:F3}",
                    type, volume, slPips, tpPips, actualRr, entry);
                _activeFvg = null;
            }
            else
            {
                Print("Order failed: {0}", result.Error);
            }
        }

        private double CalculatePositionSize(double slPips)
        {
            // Fixed-fractional sizing: units = (equity * risk%) / (SL_pips * pip_value_per_unit)
            double riskAmount = Account.Balance * (RiskPercent / 100.0);
            double pipValue = Symbol.PipValue;
            if (pipValue <= 0 || slPips <= 0) return 0;

            double raw = riskAmount / (slPips * pipValue);
            if (raw > Symbol.VolumeInUnitsMax) raw = Symbol.VolumeInUnitsMax;
            return Symbol.NormalizeVolumeInUnits(raw, RoundingMode.Down);
        }

        // =====================================================================
        // Closed-position bookkeeping
        // =====================================================================

        private void OnPositionOpened(PositionOpenedEventArgs args)
        {
            var p = args.Position;
            if (p.Label != TradeLabel || p.SymbolName != SymbolName) return;
            if (p.StopLoss.HasValue)
                _initialRiskById[p.Id] = Math.Abs(p.EntryPrice - p.StopLoss.Value);
        }

        private void OnPositionClosed(PositionClosedEventArgs args)
        {
            var p = args.Position;
            if (p.Label != TradeLabel || p.SymbolName != SymbolName) return;

            _initialRiskById.Remove(p.Id);
            ResetDailyStatsIfNeeded();
            _dailyRealizedPnl += p.NetProfit;

            if (p.NetProfit < 0) _consecutiveLosses++;
            else if (p.NetProfit > 0) _consecutiveLosses = 0;

            Print("Closed {0}: PnL={1:F2} dayPnL={2:F2} consLosses={3}",
                p.TradeType, p.NetProfit, _dailyRealizedPnl, _consecutiveLosses);
        }
    }

    // =========================================================================
    // Supporting types
    // =========================================================================

    public class FvgZone
    {
        public double High;
        public double Low;
        public bool IsBullish;
        public int CreatedBarIndex;
        public double SweepLevel;
        public bool MidTouched;
    }

    // Session boundaries in a caller-chosen "local" timezone (this bot uses NY local).
    // Update() receives the bar's time already converted to that zone, plus H/L.
    public class SessionTracker
    {
        public string Name { get; }
        public double StartHour { get; }
        public double EndHour { get; }

        public double CurrentHigh = double.NaN;
        public double CurrentLow = double.NaN;
        public double PrevHigh = double.NaN;
        public double PrevLow = double.NaN;
        public bool Active;

        public SessionTracker(string name, double start, double end)
        {
            Name = name;
            StartHour = start;
            EndHour = end;
        }

        public bool IsInSession(DateTime local)
        {
            double h = local.Hour + local.Minute / 60.0;
            if (StartHour <= EndHour) return h >= StartHour && h < EndHour;
            return h >= StartHour || h < EndHour; // wraps midnight
        }

        public void Update(DateTime local, double high, double low)
        {
            bool inSession = IsInSession(local);
            if (inSession)
            {
                if (!Active)
                {
                    if (!double.IsNaN(CurrentHigh))
                    {
                        PrevHigh = CurrentHigh;
                        PrevLow = CurrentLow;
                    }
                    CurrentHigh = high;
                    CurrentLow = low;
                    Active = true;
                }
                else
                {
                    if (high > CurrentHigh) CurrentHigh = high;
                    if (low < CurrentLow) CurrentLow = low;
                }
            }
            else
            {
                Active = false;
            }
        }
    }
}
