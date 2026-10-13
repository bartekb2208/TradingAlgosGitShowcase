using System;
using System.Collections.Generic;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    // =====================================================================================
    // Overnight Drift cBot — Boyarchenko, Larsen & Whelan (2022), "The Overnight Drift",
    // Federal Reserve Bank of New York Staff Reports, no. 917.
    // -------------------------------------------------------------------------------------
    // THE ANOMALY. S&P 500 futures returns do not accrue linearly around the clock: the
    // largest positive returns concentrate in the European-open hour, 2:00-3:00 New York
    // time (~3.7% annualized from that single hour). The paper attributes this to the
    // overnight resolution of dealer inventory imbalances left over from the previous U.S.
    // trading day: after net SELLING into the U.S. close, market makers sit long, and prices
    // drift up overnight as new (European) participants arrive and take inventory off their
    // hands. Reversals after sell-offs are strong; after rallies they are modest — hence the
    // strategy is LONG-ONLY.
    //
    // STRATEGIES (paper, Table IX; sample 2004-2020, ES futures):
    //   OD   : long 2:00-3:00 NY every day .............. Sharpe 1.1 pre-cost, -0.5 net
    //   OD+  : long 1:30-3:30 NY every day .............. Sharpe 1.3 pre-cost, +0.3 net
    //   BtD  : long 1:30-3:30 NY ONLY when the previous day's closing order imbalance was
    //          negative (RSV_close < 0, sampled 15:15-16:15 NY) — "buy the dip".
    //          .................................. Sharpe 1.8 pre-cost, 1.1 NET OF SPREADS.
    // Only BtD robustly survives transaction costs; it trades ~half of all days and is the
    // default mode here. RSV (relative signed volume) = signed volume / gross volume.
    //
    // IMPLEMENTATION NOTES (cTrader / retail CFD):
    //   * RSV proxy — true CME aggressor-signed volume is unavailable on a CFD feed. We
    //     classify every tick in the 15:15-16:15 NY window with the uptick/downtick rule
    //     (zero-ticks carry the previous sign) and take RSV = signedTicks / totalTicks,
    //     exactly like the repo's CVD bot. If too few ticks were seen (OHLC-mode backtest),
    //     we fall back to an M1-bar proxy: sum(sign(close-open) * tickVolume) / sum(tickVolume).
    //   * Windows are anchored to NEW YORK local time with self-contained US-DST handling
    //     (2nd Sun Mar -> 1st Sun Nov), the same approach as IctLiquiditySweepFvgBot.
    //   * Async-DST weeks — for ~3 weeks a year US DST is on while European DST is off; the
    //     paper shows the drift shifts to 4:00-5:00 NY then. 'Async DST Handling' can shift
    //     the window with Europe (+2h), skip those weeks, or ignore the effect.
    //   * The 1:30-3:30 NY hold does NOT cross the 17:00 NY swap cut, so no overnight
    //     financing is charged on most CFD brokers — the main cost is the spread, which is
    //     exactly what the paper says kills the unconditional variants. Check YOUR broker's
    //     overnight index spread during 1:30-3:30 NY before trading this live.
    //   * Backtest with TICK-DATA accuracy so the RSV tick classification is reproduced.
    //     Exits are time-based; the ATR stop is a disaster stop only.
    //
    // As with the other bots in this repo: research scaffold, not a validated edge. The
    // paper's sample is ES futures 2004-2020 — walk-forward test on your broker's US500
    // CFD (spread, financing, feed) before risking capital.
    // =====================================================================================
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class OvernightDriftBot : Robot
    {
        #region Parameters - Strategy

        [Parameter("Entry Mode", Group = "Strategy", DefaultValue = OndEntryMode.BuyTheDip)]
        public OndEntryMode Mode { get; set; }

        // Paper windows (NY decimal hours): OD = 2.0 -> 3.0, OD+ / BtD = 1.5 -> 3.5.
        [Parameter("Window Start (NY hour)", Group = "Strategy", DefaultValue = 1.5, MinValue = 0.0, MaxValue = 23.5)]
        public double WindowStartNy { get; set; }

        [Parameter("Window End (NY hour)", Group = "Strategy", DefaultValue = 3.5, MinValue = 0.5, MaxValue = 24.0)]
        public double WindowEndNy { get; set; }

        // BtD condition: previous day's closing RSV must be BELOW this (0 = paper spec).
        [Parameter("RSV Threshold", Group = "Strategy", DefaultValue = 0.0, MinValue = -1.0, MaxValue = 1.0)]
        public double RsvThreshold { get; set; }

        // Give up on entering if conditions (spread etc.) stay bad this long past window start.
        [Parameter("Max Entry Delay (minutes)", Group = "Strategy", DefaultValue = 30, MinValue = 1, MaxValue = 120)]
        public int MaxEntryDelayMinutes { get; set; }

        [Parameter("Async DST Handling", Group = "Strategy", DefaultValue = OndAsyncDstMode.ShiftWithEurope)]
        public OndAsyncDstMode AsyncDst { get; set; }

        // Monday's window follows Friday's close (weekend in between). Disable to skip Mondays.
        [Parameter("Trade Monday Window", Group = "Strategy", DefaultValue = true)]
        public bool TradeMonday { get; set; }

        #endregion

        #region Parameters - RSV Measurement (NY local time)

        // Paper samples closing order flow between 15:15 and 16:15 NY (cash close + futures settle).
        [Parameter("RSV Window Start (NY hour)", Group = "RSV", DefaultValue = 15.25, MinValue = 12.0, MaxValue = 16.0)]
        public double RsvStartNy { get; set; }

        [Parameter("RSV Window End (NY hour)", Group = "RSV", DefaultValue = 16.25, MinValue = 13.0, MaxValue = 18.0)]
        public double RsvEndNy { get; set; }

        // Below this many classified ticks the tick-rule RSV is considered unreliable and
        // the M1-bar fallback is used instead (lets OHLC-mode backtests still function).
        [Parameter("Min Ticks For Tick-Rule RSV", Group = "RSV", DefaultValue = 100, MinValue = 0, MaxValue = 100000)]
        public int MinRsvTicks { get; set; }

        // Reject stale signals (holidays, data gaps). Friday close -> Monday window is ~57h.
        [Parameter("Max RSV Age (hours)", Group = "RSV", DefaultValue = 80, MinValue = 12, MaxValue = 200)]
        public int MaxRsvAgeHours { get; set; }

        #endregion

        #region Parameters - Risk

        [Parameter("Risk Per Trade (%)", Group = "Risk", DefaultValue = 0.5, MinValue = 0.05, MaxValue = 3.0)]
        public double RiskPercent { get; set; }

        // Disaster stop only — the real exit is the timed close at window end.
        [Parameter("Stop ATR Multiplier (H1 ATR)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.5, MaxValue = 10.0)]
        public double StopAtrMult { get; set; }

        [Parameter("ATR Period (H1)", Group = "Risk", DefaultValue = 14, MinValue = 5, MaxValue = 50)]
        public int AtrPeriod { get; set; }

        // The overnight session is exactly where index CFD spreads widen; the paper's net
        // Sharpe hinges on paying ~1 tick. Skip the day rather than pay a bad spread.
        [Parameter("Max Spread (pips)", Group = "Risk", DefaultValue = 1.5, MinValue = 0.0, MaxValue = 100.0)]
        public double MaxSpreadPips { get; set; }

        [Parameter("Max Daily Loss (% of balance)", Group = "Risk", DefaultValue = 2.0, MinValue = 0.5, MaxValue = 25.0)]
        public double MaxDailyLossPercent { get; set; }

        [Parameter("Max Consecutive Losses", Group = "Risk", DefaultValue = 20, MinValue = 1, MaxValue = 30)]
        public int MaxConsecutiveLosses { get; set; }

        // At ~55% win rate a 5-loss streak is near-certain within months; without a cooldown
        // the halt is permanent (the counter only resets on a win, and a halted bot never
        // wins). Stand aside this many days after a streak, then reset. 0 = halt forever.
        [Parameter("Halt Cooldown (days)", Group = "Risk", DefaultValue = 5, MinValue = 0, MaxValue = 60)]
        public int HaltCooldownDays { get; set; }

        [Parameter("Max Concurrent Positions", Group = "Risk", DefaultValue = 1, MinValue = 1, MaxValue = 3)]
        public int MaxPositions { get; set; }

        #endregion

        #region Parameters - Meta

        [Parameter("Trade Label", Group = "Meta", DefaultValue = "OND_DRIFT")]
        public string TradeLabel { get; set; }

        // The anomaly is documented for S&P 500 futures. Override knowingly.
        [Parameter("Restrict To S&P 500 Symbols", Group = "Meta", DefaultValue = true)]
        public bool RestrictToSpx { get; set; }

        [Parameter("Show Diagnostics", Group = "Meta", DefaultValue = true)]
        public bool ShowDiagnostics { get; set; }

        #endregion

        // --- Indicator / data state ---
        private Bars _h1Bars;
        private Bars _m1Bars;
        private AverageTrueRange _h1Atr;

        // --- RSV accumulation (tick rule) ---
        private double _prevBid = double.NaN;
        private int _lastTickSign;                      // carried classification for zero-ticks
        private DateTime _rsvAccumNyDate = DateTime.MinValue;
        private long _rsvSignedTicks;
        private long _rsvTotalTicks;
        private long _rsvSignedM1Volume;                // M1 fallback: sign(close-open) * tickVolume
        private long _rsvGrossM1Volume;

        // --- Finalized signal ---
        private double _lastRsv = double.NaN;
        private DateTime _lastRsvUtc = DateTime.MinValue;
        private string _lastRsvSource = "none";

        // --- Trade / day state (all dates are NY-local dates) ---
        private DateTime _entryDoneNyDate = DateTime.MinValue;   // window consumed for this NY day
        private DateTime _skipLoggedNyDate = DateTime.MinValue;  // once-per-day skip logging
        private DateTime _statsNyDate = DateTime.MinValue;
        private double _dailyRealizedPnl;
        private int _consecutiveLosses;
        private DateTime _haltUntilUtc = DateTime.MinValue;   // loss-streak cooldown expiry

        // =====================================================================
        // Lifecycle
        // =====================================================================

        protected override void OnStart()
        {
            if (WindowEndNy <= WindowStartNy)
            {
                Print("Window End must be after Window Start (no midnight wrap supported). Aborting.");
                Stop();
                return;
            }
            if (RsvEndNy <= RsvStartNy)
            {
                Print("RSV Window End must be after RSV Window Start. Aborting.");
                Stop();
                return;
            }

            if (RestrictToSpx && !IsSpxSymbol(SymbolName))
            {
                Print("The overnight drift is documented for S&P 500 futures. Current symbol '{0}' does not look like an S&P 500 CFD.", SymbolName);
                Print("Set 'Restrict To S&P 500 Symbols' = false to override (results on other indices are NOT covered by the paper's headline tables).");
                Stop();
                return;
            }

            _h1Bars = MarketData.GetBars(TimeFrame.Hour);
            _m1Bars = MarketData.GetBars(TimeFrame.Minute);
            _h1Atr = Indicators.AverageTrueRange(_h1Bars, AtrPeriod, MovingAverageType.Exponential);

            Symbol.Tick += OnSymbolTick;
            _m1Bars.BarClosed += OnM1BarClosed;
            Positions.Closed += OnPositionClosed;

            // Survive a restart with a position open: don't double-enter today's window.
            foreach (var p in Positions.FindAll(TradeLabel, SymbolName))
            {
                _entryDoneNyDate = UtcToNy(Server.Time).Date;
                Print("Adopted open position PID{0} from a previous run; entry disabled for NY {1:yyyy-MM-dd}.", p.Id, _entryDoneNyDate);
            }

            _statsNyDate = UtcToNy(Server.Time).Date;

            LogDstAudit();
            Print("OvernightDriftBot started on {0}. Mode={1} window NY {2:F2}-{3:F2}h, RSV NY {4:F2}-{5:F2}h, threshold {6:F2}.",
                SymbolName, Mode, WindowStartNy, WindowEndNy, RsvStartNy, RsvEndNy, RsvThreshold);
            Print("PipSize={0} PipValue={1} VolMin={2} VolMax={3} VolStep={4}",
                Symbol.PipSize, Symbol.PipValue, Symbol.VolumeInUnitsMin, Symbol.VolumeInUnitsMax, Symbol.VolumeInUnitsStep);
        }

        protected override void OnStop()
        {
            // _m1Bars is still null when OnStart aborted before wiring anything up.
            if (_m1Bars == null) return;
            Symbol.Tick -= OnSymbolTick;
            _m1Bars.BarClosed -= OnM1BarClosed;
            Positions.Closed -= OnPositionClosed;
            // Positions deliberately left open on manual stop; the timed exit is gone once
            // the bot is stopped, so close manually if you stop it mid-window.
            if (Positions.FindAll(TradeLabel, SymbolName).Length > 0)
                Print("WARNING: bot stopped with an open position; the timed window exit will NOT fire.");
        }

        private static bool IsSpxSymbol(string symbolName)
        {
            if (string.IsNullOrEmpty(symbolName)) return false;
            var s = symbolName.ToUpperInvariant().Replace("/", "").Replace("_", "").Replace("-", "").Replace(".", "");
            return s.StartsWith("US500") || s.StartsWith("SPX500") || s.StartsWith("USA500")
                || s.StartsWith("SP500") || s.StartsWith("SPX") || s.StartsWith("ES");
        }

        // =====================================================================
        // Tick pipeline: RSV accumulation + timed entry/exit
        // =====================================================================

        private void OnSymbolTick(SymbolTickEventArgs e)
        {
            var utc = Server.Time;
            var ny = UtcToNy(utc);

            ClassifyTickForRsv(e.Bid, ny);
            FinalizeRsvIfDue(utc, ny);
            ManageWindow(utc, ny);
        }

        // Uptick/downtick classification inside the RSV window only.
        private void ClassifyTickForRsv(double bid, DateTime ny)
        {
            double prev = _prevBid;
            _prevBid = bid;
            if (double.IsNaN(prev)) return;

            if (!InNyWindow(ny, RsvStartNy, RsvEndNy)) return;

            if (_rsvAccumNyDate != ny.Date)
                ResetRsvAccum(ny.Date);

            int sign;
            if (bid > prev) sign = +1;
            else if (bid < prev) sign = -1;
            else sign = _lastTickSign;   // zero-tick: carry previous classification

            if (sign == 0) return;
            _rsvSignedTicks += sign;
            _rsvTotalTicks++;
            _lastTickSign = sign;
        }

        // M1 fallback accumulation: signed tick volume by bar direction, same NY window.
        private void OnM1BarClosed(BarClosedEventArgs args)
        {
            int idx = _m1Bars.Count - 2;
            if (idx < 0) return;

            var ny = UtcToNy(_m1Bars.OpenTimes[idx]);
            if (!InNyWindow(ny, RsvStartNy, RsvEndNy)) return;

            if (_rsvAccumNyDate != ny.Date)
                ResetRsvAccum(ny.Date);

            long vol = (long)_m1Bars.TickVolumes[idx];
            double open = _m1Bars.OpenPrices[idx];
            double close = _m1Bars.ClosePrices[idx];
            int dir = close > open ? +1 : close < open ? -1 : 0;
            _rsvSignedM1Volume += dir * vol;
            _rsvGrossM1Volume += vol;
        }

        private void ResetRsvAccum(DateTime nyDate)
        {
            _rsvAccumNyDate = nyDate;
            _rsvSignedTicks = 0;
            _rsvTotalTicks = 0;
            _rsvSignedM1Volume = 0;
            _rsvGrossM1Volume = 0;
            _lastTickSign = 0;
        }

        // Once the clock passes the RSV window end on the accumulation day, freeze the signal.
        // A ~3-minute grace period lets the final M1 fallback bar close and be counted first
        // (Bars.BarClosed only fires when the tick that opens the NEXT bar arrives).
        private void FinalizeRsvIfDue(DateTime utc, DateTime ny)
        {
            if (_rsvAccumNyDate == DateTime.MinValue) return;
            const double graceHours = 0.05;
            bool due = ny.Date > _rsvAccumNyDate
                       || (ny.Date == _rsvAccumNyDate && NyDecimalHour(ny) >= RsvEndNy + graceHours);
            if (!due) return;

            if (_rsvTotalTicks >= Math.Max(1, MinRsvTicks))
            {
                _lastRsv = (double)_rsvSignedTicks / _rsvTotalTicks;
                _lastRsvSource = string.Format("tick-rule ({0} ticks)", _rsvTotalTicks);
            }
            else if (_rsvGrossM1Volume > 0)
            {
                _lastRsv = (double)_rsvSignedM1Volume / _rsvGrossM1Volume;
                _lastRsvSource = string.Format("M1 fallback (gross {0})", _rsvGrossM1Volume);
            }
            else
            {
                _lastRsv = double.NaN;
                _lastRsvSource = "no data";
            }
            _lastRsvUtc = utc;

            Print("RSV_close[{0:yyyy-MM-dd} NY] = {1:F4} via {2}",
                _rsvAccumNyDate, _lastRsv, _lastRsvSource);

            _rsvAccumNyDate = DateTime.MinValue;
            if (ShowDiagnostics) DrawDiagnostics(ny);
        }

        // =====================================================================
        // Window management: enter at start, flat at end
        // =====================================================================

        private void ManageWindow(DateTime utc, DateTime ny)
        {
            RollDailyStats(ny);

            double start = WindowStartNy;
            double end = WindowEndNy;
            bool asyncWeek = IsUsEdt(utc) != IsEuCest(utc);
            if (asyncWeek)
            {
                if (AsyncDst == OndAsyncDstMode.SkipAsyncWeeks)
                {
                    CloseWindowPositions("async-DST week skip");
                    return;
                }
                if (AsyncDst == OndAsyncDstMode.ShiftWithEurope)
                {
                    // US-Europe gap is 4h instead of 5h; the paper finds the drift at
                    // 4:00-5:00 NY in these weeks (European open in NY terms), i.e. +2h.
                    start += 2.0;
                    end += 2.0;
                }
            }

            double h = NyDecimalHour(ny);

            // ---- Timed exit: anything at/after window end (or outside the day's window) goes flat.
            if (h >= end || h < start - 1e-9)
            {
                if (h >= end && _entryDoneNyDate == ny.Date)
                    CloseWindowPositions("window end");
                else if (h < start)
                    CloseWindowPositions("outside window");   // safety net (restart leftovers)
                return;
            }

            // ---- Inside the window: try to enter once per NY day.
            if (_entryDoneNyDate == ny.Date) return;
            if (h > start + MaxEntryDelayMinutes / 60.0) { LogSkipOnce(ny, "entry delay exceeded"); return; }
            if (Positions.FindAll(TradeLabel, SymbolName).Length >= MaxPositions) return;

            if (ny.DayOfWeek == DayOfWeek.Saturday || ny.DayOfWeek == DayOfWeek.Sunday) return;
            if (ny.DayOfWeek == DayOfWeek.Monday && !TradeMonday) { LogSkipOnce(ny, "Monday disabled"); return; }

            // BtD conditioning on the previous close's order imbalance.
            if (Mode == OndEntryMode.BuyTheDip)
            {
                if (double.IsNaN(_lastRsv))
                {
                    LogSkipOnce(ny, "no RSV signal yet");
                    return;
                }
                double ageHours = (utc - _lastRsvUtc).TotalHours;
                if (ageHours > MaxRsvAgeHours)
                {
                    LogSkipOnce(ny, string.Format("RSV stale ({0:F0}h old)", ageHours));
                    return;
                }
                if (_lastRsv >= RsvThreshold)
                {
                    LogSkipOnce(ny, string.Format("RSV {0:F4} >= threshold {1:F2} (no dip to buy)", _lastRsv, RsvThreshold));
                    _entryDoneNyDate = ny.Date;   // decision made for the day; don't re-test every tick
                    return;
                }
            }

            if (!RiskGuardsPass(ny)) return;

            TryEnterLong(ny, asyncWeek, start, end);
        }

        private void TryEnterLong(DateTime ny, bool asyncWeek, double start, double end)
        {
            double atr = _h1Atr.Result.LastValue;
            if (double.IsNaN(atr) || atr <= 0) { LogSkipOnce(ny, "ATR not ready"); return; }

            double stopPips = StopAtrMult * atr / Symbol.PipSize;
            if (stopPips <= 0) return;

            double volume = CalculateVolume(stopPips);
            if (volume <= 0)
            {
                LogSkipOnce(ny, "volume below broker minimum at this risk setting");
                _entryDoneNyDate = ny.Date;
                return;
            }

            var result = ExecuteMarketOrder(TradeType.Buy, SymbolName, volume, TradeLabel, stopPips, null);
            if (result.IsSuccessful)
            {
                _entryDoneNyDate = ny.Date;
                Print("LONG {0} @ {1} | NY {2:HH:mm} window {3:F2}-{4:F2}{5} | RSV={6} ({7}) SL={8:F1}p vol={9}",
                    SymbolName, result.Position.EntryPrice, ny, start, end,
                    asyncWeek ? " (async-DST shifted)" : "",
                    double.IsNaN(_lastRsv) ? "n/a" : _lastRsv.ToString("F4"),
                    Mode == OndEntryMode.BuyTheDip ? _lastRsvSource : "unconditional",
                    stopPips, volume);
                if (ShowDiagnostics) DrawDiagnostics(ny);
            }
            else
            {
                // Leave _entryDoneNyDate unset: retry on later ticks until MaxEntryDelay.
                Print("Entry rejected: {0}", result.Error);
            }
        }

        private void CloseWindowPositions(string reason)
        {
            foreach (var p in Positions.FindAll(TradeLabel, SymbolName))
            {
                var result = ClosePosition(p);
                if (result.IsSuccessful)
                    Print("Timed exit ({0}): closed PID{1} PnL={2:F2}", reason, p.Id, p.NetProfit);
                else
                    Print("Timed exit ({0}) FAILED for PID{1}: {2}", reason, p.Id, result.Error);
            }
        }

        // =====================================================================
        // Risk guards / sizing / bookkeeping
        // =====================================================================

        private bool RiskGuardsPass(DateTime ny)
        {
            double spreadPips = Symbol.Spread / Symbol.PipSize;
            if (spreadPips > MaxSpreadPips)
            {
                // Spread can tighten later in the window — no LogSkipOnce, keep retrying.
                return false;
            }

            double maxLossCash = Account.Balance * MaxDailyLossPercent / 100.0;
            if (_dailyRealizedPnl <= -maxLossCash)
            {
                LogSkipOnce(ny, "daily loss cap hit");
                return false;
            }

            if (_consecutiveLosses >= MaxConsecutiveLosses)
            {
                var utc = Server.Time;
                if (_haltUntilUtc == DateTime.MinValue)
                {
                    if (HaltCooldownDays <= 0)
                    {
                        LogSkipOnce(ny, string.Format("consecutive-loss halt ({0}, permanent)", _consecutiveLosses));
                        return false;
                    }
                    _haltUntilUtc = utc.AddDays(HaltCooldownDays);
                    Print("Consecutive-loss halt ({0}): standing aside until {1:yyyy-MM-dd HH:mm} UTC.",
                        _consecutiveLosses, _haltUntilUtc);
                }
                if (utc < _haltUntilUtc)
                {
                    LogSkipOnce(ny, string.Format("consecutive-loss cooldown until {0:yyyy-MM-dd} UTC", _haltUntilUtc));
                    return false;
                }
                Print("Consecutive-loss cooldown over; counter reset, resuming.");
                _consecutiveLosses = 0;
                _haltUntilUtc = DateTime.MinValue;
            }

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

        private void RollDailyStats(DateTime ny)
        {
            if (ny.Date == _statsNyDate) return;
            _statsNyDate = ny.Date;
            _dailyRealizedPnl = 0;
            // _consecutiveLosses persists across days; cleared on first win.
        }

        private void OnPositionClosed(PositionClosedEventArgs args)
        {
            var p = args.Position;
            if (p.SymbolName != SymbolName || p.Label != TradeLabel) return;

            _dailyRealizedPnl += p.NetProfit;
            if (p.NetProfit < 0) _consecutiveLosses++;
            else if (p.NetProfit > 0) _consecutiveLosses = 0;

            Print("Closed {0}: PnL={1:F2} dayPnL={2:F2} consLosses={3}",
                p.TradeType, p.NetProfit, _dailyRealizedPnl, _consecutiveLosses);
        }

        private void LogSkipOnce(DateTime ny, string reason)
        {
            if (_skipLoggedNyDate == ny.Date) return;
            _skipLoggedNyDate = ny.Date;
            Print("Skip [{0:yyyy-MM-dd} NY]: {1}", ny.Date, reason);
        }

        // =====================================================================
        // Diagnostics
        // =====================================================================

        private void DrawDiagnostics(DateTime ny)
        {
            string txt = string.Format(
                "Overnight Drift ({0})\nNY now {1:HH:mm} ({2})\nwindow NY {3:F2}-{4:F2}h\nRSV_close {5} via {6}\nconsLosses {7} dayPnL {8:F2}",
                Mode, ny, IsUsEdt(Server.Time) ? "EDT" : "EST",
                WindowStartNy, WindowEndNy,
                double.IsNaN(_lastRsv) ? "n/a" : _lastRsv.ToString("F4"), _lastRsvSource,
                _consecutiveLosses, _dailyRealizedPnl);
            Chart.DrawStaticText("ond_stats", txt, VerticalAlignment.Top, HorizontalAlignment.Left, Color.White);
        }

        private void LogDstAudit()
        {
            var utc = Server.Time;
            bool edt = IsUsEdt(utc);
            bool cest = IsEuCest(utc);
            Print("DST audit @ {0:yyyy-MM-dd HH:mm} UTC | NY={1} (UTC{2:+0;-0}) | EU={3} (UTC{4:+0;-0})",
                utc, edt ? "EDT" : "EST", edt ? -4 : -5, cest ? "CEST" : "CET", cest ? 2 : 1);
            if (edt != cest)
                Print("NOTE: US and European DST are OUT OF SYNC this week. AsyncDst={0} governs the drift window.", AsyncDst);
        }

        // =====================================================================
        // Time helpers (self-contained, no TimeZoneInfo dependency)
        //   US EDT : 2nd Sun Mar 07:00 UTC -> 1st Sun Nov 06:00 UTC
        //   EU CEST: last Sun Mar 01:00 UTC -> last Sun Oct 01:00 UTC
        // =====================================================================

        private static double NyDecimalHour(DateTime ny)
        {
            return ny.Hour + ny.Minute / 60.0 + ny.Second / 3600.0;
        }

        private static bool InNyWindow(DateTime ny, double startHour, double endHour)
        {
            double h = NyDecimalHour(ny);
            return h >= startHour && h < endHour;
        }

        private static DateTime UtcToNy(DateTime utc)
        {
            return utc.AddHours(IsUsEdt(utc) ? -4 : -5);
        }

        private static DateTime NthSundayOfMonth(int year, int month, int n)
        {
            var first = new DateTime(year, month, 1);
            int fwd = ((int)DayOfWeek.Sunday - (int)first.DayOfWeek + 7) % 7;
            return first.AddDays(fwd + 7 * (n - 1));
        }

        private static DateTime LastSundayOfMonth(int year, int month)
        {
            var last = new DateTime(year, month, DateTime.DaysInMonth(year, month));
            int back = ((int)last.DayOfWeek - (int)DayOfWeek.Sunday + 7) % 7;
            return last.AddDays(-back);
        }

        private static bool IsUsEdt(DateTime utc)
        {
            int y = utc.Year;
            var start = NthSundayOfMonth(y, 3, 2).AddHours(7);
            var end = NthSundayOfMonth(y, 11, 1).AddHours(6);
            return utc >= start && utc < end;
        }

        private static bool IsEuCest(DateTime utc)
        {
            int y = utc.Year;
            var start = LastSundayOfMonth(y, 3).AddHours(1);
            var end = LastSundayOfMonth(y, 10).AddHours(1);
            return utc >= start && utc < end;
        }
    }

    // =========================================================================
    // Supporting types (prefixed Ond... per repo convention)
    // =========================================================================

    public enum OndEntryMode
    {
        Unconditional,   // OD / OD+: long the window every trading day
        BuyTheDip        // BtD: long only when previous closing RSV < threshold
    }

    public enum OndAsyncDstMode
    {
        NyAnchored,       // keep NY-time window through async-DST weeks
        ShiftWithEurope,  // shift window +2h during async weeks (paper: drift at 4:00-5:00 NY)
        SkipAsyncWeeks    // stand aside during async weeks
    }
}
