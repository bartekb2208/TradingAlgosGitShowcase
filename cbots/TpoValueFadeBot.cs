using System;
using System.Collections.Generic;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    // =====================================================================================
    // TPO Value-Area Fade  —  auction-market mean reversion with a scale-in ladder
    // Designed for US100 / US500 CFD, M5 chart.                            —  BACKTESTABLE
    // -------------------------------------------------------------------------------------
    // HYPOTHESIS (auction market theory, Steidlmayer/CBOT Market Profile). A trading
    // session builds a value area (the price band holding ~70% of time-price activity
    // around the point of control). When the next session probes beyond value and the
    // probe FAILS — price closes back inside — the excursion was an unfilled auction:
    // responsive traders push price back toward the POC. Fade the failed probe, target
    // the POC. This is a DIFFERENT hypothesis family from the falsified band-fade MR
    // (structure-referenced intraday reversion on an instrument with a real session,
    // vs indicator-stretch reversion on a 24h cross).
    //
    // TPO NOTE. cTrader has no true volume, and TickVolume is a price-change count. This
    // bot builds the profile from TIME spent at price (each closed chart bar contributes
    // one count to every price bin its range covers) — which is exactly the original TPO
    // definition (time-price opportunity), not a volume profile. Tick data affects only
    // stop/TP fill fidelity, not profile construction.
    //
    // MECHANICS.
    //   * PROFILE  — built from the PREVIOUS RTH session's chart-TF bars (NY 09:30–16:00,
    //     self-contained US DST handling). POC = most-visited bin; VA = expand from POC
    //     until ValueAreaPercent of counts. Monday uses Friday's profile.
    //   * SIGNAL   — today, after the opening window: price extends ≥ MinExcursion×ATR
    //     beyond VAH (or below VAL), then a bar CLOSES back inside the value area →
    //     enter fade (short from above VAH / long from below VAL). Target = POC (server
    //     TP). Shared hard stop = excursion extreme ± StopBuffer×ATR.
    //   * SCALING  — if price re-extends and prints ANOTHER failed-probe close at least
    //     RungStep×ATR beyond the previous rung, add a rung: same shared stop, same POC
    //     target, up to MaxEntries rungs. Each rung is sized to AggregateRisk/MaxEntries
    //     at the SHARED stop, so a full ladder loses exactly AggregateRisk and a partial
    //     ladder loses less. Scaling without an aggregate cap is how prop accounts die;
    //     this ladder's worst case is fixed before the first entry.
    //   * FLAT     — everything closes FlatMinutesBeforeClose before the RTH close.
    //     Intraday auction thesis dies with the session; no overnight holds, no swaps.
    //   * INVALID  — a probe that runs past the shared stop is a trend day / range
    //     extension: the auction genuinely repriced, stand down (per side) for the day.
    //
    // PRE-REGISTERED CAMPAIGN (memory: tpo-value-fade-campaign):
    //   * IS Nov 2020 → Dec 2023; OOS Jan 2024 → now, untouched until config frozen.
    //   * Envelope at defaults: 1–3 baskets/day-with-signal, win 52–62%, payoff 0.8–1.3,
    //     PF 1.15–1.5, flat-not-red on trend-heavy months (the veto is the failed-probe
    //     requirement itself).
    //   * Falsifier: IS PF < 1.05 at neutral defaults → abandon WITHOUT a sweep.
    //   * The DOM/absorption confirmation layer is deliberately ABSENT here — cTrader's
    //     backtester has no order book. That layer belongs to the MotiveWave/Rithmic
    //     futures phase, where it can be learned and measured on real depth data.
    //
    // Helpers prefixed `Tvf...`. Bar-close logic; OHLC OK, tick for fill fidelity.
    // =====================================================================================
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class TpoValueFadeBot : Robot
    {
        #region Parameters - Profile

        [Parameter("Value Area (%)", Group = "Profile", DefaultValue = 70, MinValue = 50, MaxValue = 95)]
        public int ValueAreaPercent { get; set; }

        [Parameter("Profile Bins", Group = "Profile", DefaultValue = 40, MinValue = 10, MaxValue = 200)]
        public int ProfileBins { get; set; }

        #endregion

        #region Parameters - Signal

        [Parameter("ATR Period", Group = "Signal", DefaultValue = 14, MinValue = 5, MaxValue = 100)]
        public int AtrPeriod { get; set; }

        [Parameter("Min Excursion (xATR)", Group = "Signal", DefaultValue = 0.5, MinValue = 0.0, MaxValue = 5.0)]
        public double MinExcursionAtr { get; set; }

        [Parameter("Entry Delay After Open (min)", Group = "Signal", DefaultValue = 30, MinValue = 0, MaxValue = 180)]
        public int EntryDelayMinutes { get; set; }

        [Parameter("Allow Longs (fade below VAL)", Group = "Signal", DefaultValue = true)]
        public bool AllowLong { get; set; }

        [Parameter("Allow Shorts (fade above VAH)", Group = "Signal", DefaultValue = true)]
        public bool AllowShort { get; set; }

        #endregion

        #region Parameters - Ladder / Exits

        [Parameter("Max Entries Per Basket", Group = "Ladder", DefaultValue = 3, MinValue = 1, MaxValue = 5)]
        public int MaxEntries { get; set; }

        [Parameter("Rung Step (xATR)", Group = "Ladder", DefaultValue = 0.75, MinValue = 0.1, MaxValue = 5.0)]
        public double RungStepAtr { get; set; }

        [Parameter("Stop Beyond Extreme (xATR)", Group = "Ladder", DefaultValue = 1.0, MinValue = 0.25, MaxValue = 5.0)]
        public double StopBufferAtr { get; set; }

        // Floor on entry→stop distance. A market order can fill beyond the bar-close price
        // the signal was computed from (fast tape); without this floor the sizing divides
        // by a near-zero distance (monster volume) and cTrader drops a sub-minimum SL
        // entirely — naked position. If price is already within this floor of the stop,
        // the probe is effectively invalidated anyway: skip the rung.
        [Parameter("Min Rung Stop (xATR)", Group = "Ladder", DefaultValue = 0.5, MinValue = 0.1, MaxValue = 3.0)]
        public double MinRungStopAtr { get; set; }

        [Parameter("Flat Before Close (min)", Group = "Ladder", DefaultValue = 5, MinValue = 1, MaxValue = 60)]
        public int FlatMinutesBeforeClose { get; set; }

        #endregion

        #region Parameters - Risk

        [Parameter("Aggregate Risk Per Basket (%)", Group = "Risk", DefaultValue = 0.5, MinValue = 0.05, MaxValue = 3.0)]
        public double AggregateRiskPercent { get; set; }

        [Parameter("Max Baskets Per Day", Group = "Risk", DefaultValue = 2, MinValue = 1, MaxValue = 6)]
        public int MaxBasketsPerDay { get; set; }

        [Parameter("Max Daily Loss (% of balance)", Group = "Risk", DefaultValue = 2.0, MinValue = 0.5, MaxValue = 25.0)]
        public double MaxDailyLossPercent { get; set; }

        [Parameter("Max Spread (pips)", Group = "Risk", DefaultValue = 3.0, MinValue = 0.0, MaxValue = 50.0)]
        public double MaxSpreadPips { get; set; }

        #endregion

        #region Parameters - Meta

        [Parameter("Trade Label", Group = "Meta", DefaultValue = "TVF")]
        public string TradeLabel { get; set; }

        [Parameter("Show Diagnostics", Group = "Meta", DefaultValue = true)]
        public bool ShowDiagnostics { get; set; }

        #endregion

        // --- Helper types ---
        private sealed class TvfProfile
        {
            public DateTime NyDate;
            public double Poc, Vah, Val;
        }

        private sealed class TvfBasket
        {
            public TradeType Side;
            public double SharedStop;
            public double Target;
            public double LastRungPrice;
            public int Rungs;
        }

        // --- State ---
        private AverageTrueRange _atr;
        private readonly List<double[]> _sessionBars = new List<double[]>();  // [high, low] of today's RTH bars
        private DateTime _sessionNyDate = DateTime.MinValue;
        private TvfProfile _profile;            // yesterday's profile, used today
        private TvfBasket _basket;              // active ladder (one at a time)
        private int _basketsToday;
        private bool _sideDoneLong, _sideDoneShort;   // per-day invalidation latches
        private double _excursionHi, _excursionLo;    // today's extremes beyond VA
        private double _dailyRealizedPnl;
        private bool _haltPrintedToday;

        // =====================================================================
        // Lifecycle
        // =====================================================================

        protected override void OnStart()
        {
            _atr = Indicators.AverageTrueRange(Bars, AtrPeriod, MovingAverageType.Exponential);
            Bars.BarClosed += OnBarClosed;
            Positions.Closed += OnPositionClosed;

            Print("TpoValueFade started on {0} {1}. VA {2}% bins {3} | exc {4}xATR rung {5}xATR stop {6}xATR | ladder {7} @ {8}% aggregate",
                SymbolName, Bars.TimeFrame, ValueAreaPercent, ProfileBins,
                MinExcursionAtr, RungStepAtr, StopBufferAtr, MaxEntries, AggregateRiskPercent);
        }

        protected override void OnStop()
        {
            Bars.BarClosed -= OnBarClosed;
            Positions.Closed -= OnPositionClosed;
        }

        // =====================================================================
        // NY session clock (self-contained US DST: 2nd Sun Mar – 1st Sun Nov)
        // =====================================================================

        private static bool TvfIsUsDst(DateTime utc)
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

        private static DateTime TvfToNy(DateTime utc) => utc.AddHours(TvfIsUsDst(utc) ? -4 : -5);

        private static bool TvfInRth(DateTime ny) =>
            ny.DayOfWeek != DayOfWeek.Saturday && ny.DayOfWeek != DayOfWeek.Sunday &&
            ny.TimeOfDay >= new TimeSpan(9, 30, 0) && ny.TimeOfDay < new TimeSpan(16, 0, 0);

        // =====================================================================
        // Core
        // =====================================================================

        private void OnBarClosed(BarClosedEventArgs e)
        {
            var bars = e.Bars;
            int idx = bars.Count - 2;
            if (idx < AtrPeriod + 1) return;

            var utc = bars.OpenTimes[idx];
            var ny = TvfToNy(utc);
            if (!TvfInRth(ny)) return;

            // new RTH session → freeze yesterday's bars into a profile, reset day state
            if (ny.Date != _sessionNyDate)
            {
                if (_sessionBars.Count > 0) _profile = BuildProfile(_sessionNyDate);
                _sessionBars.Clear();
                _sessionNyDate = ny.Date;
                _basket = null;
                _basketsToday = 0;
                _sideDoneLong = _sideDoneShort = false;
                _excursionHi = double.MinValue;
                _excursionLo = double.MaxValue;
                _dailyRealizedPnl = 0;
                _haltPrintedToday = false;
                if (_profile != null && ShowDiagnostics)
                    Print("Session {0:ddd dd MMM}: profile POC {1} VA [{2} .. {3}]",
                        ny, _profile.Poc, _profile.Val, _profile.Vah);
            }

            _sessionBars.Add(new[] { bars.HighPrices[idx], bars.LowPrices[idx] });

            // forced flat near the close (16:00 NY minus FlatMinutesBeforeClose)
            if (ny.TimeOfDay >= new TimeSpan(16, 0, 0).Subtract(TimeSpan.FromMinutes(FlatMinutesBeforeClose)))
            {
                FlattenAll("session close");
                return;
            }

            if (_profile == null) return;
            double atr = _atr.Result[idx];
            if (double.IsNaN(atr) || atr <= 0) return;

            TrackExcursions(bars, idx);
            ManageBasket(bars, idx);
            TrySignal(bars, idx, ny, atr);
        }

        private void TrackExcursions(Bars bars, int idx)
        {
            _excursionHi = Math.Max(_excursionHi, bars.HighPrices[idx]);
            _excursionLo = Math.Min(_excursionLo, bars.LowPrices[idx]);
        }

        private void ManageBasket(Bars bars, int idx)
        {
            if (_basket == null) return;
            // invalidation latch: price ran through the shared stop level → trend day for that side
            if (Positions.FindAll(TradeLabel, SymbolName).Length == 0)
            {
                _basket = null;   // stop or TP took everything out server-side
                return;
            }
            double close = bars.ClosePrices[idx];
            if ((_basket.Side == TradeType.Sell && close >= _basket.SharedStop) ||
                (_basket.Side == TradeType.Buy && close <= _basket.SharedStop))
            {
                if (_basket.Side == TradeType.Sell) _sideDoneShort = true; else _sideDoneLong = true;
            }
        }

        private void TrySignal(Bars bars, int idx, DateTime ny, double atr)
        {
            if (ny.TimeOfDay < new TimeSpan(9, 30, 0).Add(TimeSpan.FromMinutes(EntryDelayMinutes))) return;
            if (ny.TimeOfDay >= new TimeSpan(15, 30, 0)) return;   // no fresh risk in the last half hour
            if (!GuardsPass()) return;

            double close = bars.ClosePrices[idx];
            double minExc = MinExcursionAtr * atr;

            // SHORT: probed above VAH by minExc, now closed back inside value
            bool shortProbe = AllowShort && !_sideDoneShort &&
                _excursionHi >= _profile.Vah + minExc && close < _profile.Vah && close > _profile.Val;
            // LONG: probed below VAL by minExc, now closed back inside value
            bool longProbe = AllowLong && !_sideDoneLong &&
                _excursionLo <= _profile.Val - minExc && close > _profile.Val && close < _profile.Vah;

            if (_basket == null)
            {
                if (!shortProbe && !longProbe) return;
                if (_basketsToday >= MaxBasketsPerDay) return;
                var side = shortProbe ? TradeType.Sell : TradeType.Buy;
                double extreme = shortProbe ? _excursionHi : _excursionLo;
                double stop = shortProbe ? extreme + StopBufferAtr * atr : extreme - StopBufferAtr * atr;
                OpenRung(side, stop, _profile.Poc, close, atr, isFirst: true);
            }
            else
            {
                // add a rung only on a fresh failed probe at a worse price than the last rung
                bool sameSideSignal = (_basket.Side == TradeType.Sell && shortProbe) ||
                                      (_basket.Side == TradeType.Buy && longProbe);
                if (!sameSideSignal || _basket.Rungs >= MaxEntries) return;
                // require the excursion to have extended at least RungStep beyond the last rung
                double step = RungStepAtr * atr;
                double extreme = _basket.Side == TradeType.Sell ? _excursionHi : _excursionLo;
                bool extended = _basket.Side == TradeType.Sell
                    ? extreme >= _basket.LastRungPrice + step
                    : extreme <= _basket.LastRungPrice - step;
                if (!extended) return;
                OpenRung(_basket.Side, _basket.SharedStop, _basket.Target, close, atr, isFirst: false);
            }
        }

        private void OpenRung(TradeType side, double stop, double target, double refClose, double atr, bool isFirst)
        {
            double entryRef = side == TradeType.Sell ? Symbol.Bid : Symbol.Ask;

            // stop must be on the protective side of the LIVE price by at least the floor —
            // no Abs(): a sign inversion here means the fill would be beyond its own stop.
            double signedDist = side == TradeType.Sell ? stop - entryRef : entryRef - stop;
            if (signedDist < MinRungStopAtr * atr)
            {
                if (ShowDiagnostics)
                    Print("Rung skipped: live price {0} within {1:F2} of stop {2} (floor {3:F2}) — probe invalidated",
                        entryRef, signedDist, stop, MinRungStopAtr * atr);
                return;
            }
            double stopDistPips = signedDist / Symbol.PipSize;

            // each rung carries an equal share of the aggregate budget at the SHARED stop
            double rungRisk = Account.Balance * (AggregateRiskPercent / 100.0) / MaxEntries;
            double perUnit = stopDistPips * Symbol.PipValue;
            if (perUnit <= 0) return;
            double volume = Symbol.NormalizeVolumeInUnits(rungRisk / perUnit, RoundingMode.Down);
            if (volume < Symbol.VolumeInUnitsMin)
            {
                Print("Rung volume too small (risk share {0:F2}, stop {1:F1}p) — skipped.", rungRisk, stopDistPips);
                return;
            }
            volume = Math.Min(volume, Symbol.VolumeInUnitsMax);

            double tpPips = Math.Abs(entryRef - target) / Symbol.PipSize;
            if (tpPips <= 0) return;

            var result = ExecuteMarketOrder(side, SymbolName, volume, TradeLabel, stopDistPips, tpPips);
            if (!result.IsSuccessful || result.Position == null)
            {
                Print("Rung order failed: {0}", result.Error);
                return;
            }

            if (isFirst)
            {
                _basket = new TvfBasket { Side = side, SharedStop = stop, Target = target, Rungs = 0 };
                _basketsToday++;
            }
            _basket.Rungs++;
            _basket.LastRungPrice = result.Position.EntryPrice;

            if (ShowDiagnostics)
                Print("RUNG {0}/{1} {2} @ {3} | stop {4} target(POC) {5} | vol {6}",
                    _basket.Rungs, MaxEntries, side, result.Position.EntryPrice, stop, target, volume);
        }

        private void FlattenAll(string reason)
        {
            foreach (var pos in Positions.FindAll(TradeLabel, SymbolName))
            {
                var r = ClosePosition(pos);
                if (r.IsSuccessful && ShowDiagnostics) Print("FLAT ({0}): {1} net {2:F2}", reason, pos.Id, pos.NetProfit);
            }
            _basket = null;
        }

        private bool GuardsPass()
        {
            double dailyCap = Account.Balance * MaxDailyLossPercent / 100.0;
            if (_dailyRealizedPnl <= -dailyCap) return Halt("daily-loss cap hit");
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

        private void OnPositionClosed(PositionClosedEventArgs args)
        {
            var pos = args.Position;
            if (pos.Label != TradeLabel || pos.SymbolName != SymbolName) return;
            _dailyRealizedPnl += pos.NetProfit;
            if (ShowDiagnostics)
                Print("CLOSED {0} net {1:F2} | day PnL {2:F2}", pos.Id, pos.NetProfit, _dailyRealizedPnl);
        }

        // =====================================================================
        // Profile construction (previous session's bars → POC / VAH / VAL)
        // =====================================================================

        private TvfProfile BuildProfile(DateTime nyDate)
        {
            double hi = double.MinValue, lo = double.MaxValue;
            foreach (var b in _sessionBars) { hi = Math.Max(hi, b[0]); lo = Math.Min(lo, b[1]); }
            if (hi <= lo) return null;

            int nBins = ProfileBins;
            double binH = (hi - lo) / nBins;
            var counts = new int[nBins];
            foreach (var b in _sessionBars)
            {
                int i0 = (int)((b[1] - lo) / binH); if (i0 < 0) i0 = 0; if (i0 >= nBins) i0 = nBins - 1;
                int i1 = (int)((b[0] - lo) / binH); if (i1 < 0) i1 = 0; if (i1 >= nBins) i1 = nBins - 1;
                for (int i = i0; i <= i1; i++) counts[i]++;
            }

            int poc = 0;
            for (int i = 1; i < nBins; i++) if (counts[i] > counts[poc]) poc = i;

            long total = 0; foreach (var c in counts) total += c;
            long need = (long)Math.Ceiling(total * ValueAreaPercent / 100.0);
            long acc = counts[poc];
            int up = poc, dn = poc;
            while (acc < need && (up < nBins - 1 || dn > 0))
            {
                int upC = up < nBins - 1 ? counts[up + 1] : -1;
                int dnC = dn > 0 ? counts[dn - 1] : -1;
                if (upC >= dnC) { up++; acc += Math.Max(upC, 0); }
                else { dn--; acc += Math.Max(dnC, 0); }
            }

            return new TvfProfile
            {
                NyDate = nyDate,
                Poc = Math.Round(lo + (poc + 0.5) * binH, Symbol.Digits),
                Vah = Math.Round(lo + (up + 1) * binH, Symbol.Digits),
                Val = Math.Round(lo + dn * binH, Symbol.Digits)
            };
        }
    }
}
