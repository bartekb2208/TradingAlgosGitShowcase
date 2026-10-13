"""fvt_data.py — FVT V1 data access. Every byte off disk or network is here.

Read-only with respect to data/. The only file this module writes is its own
yfinance cache under python/fvt/cache/.

SPLIT DISCIPLINE: `load_*_test` and `load_*_validation` are separate entry points
on purpose. The validation loaders are called from exactly one place in
fvt_stats.py, behind an explicit flag. Do not call them during development.

NOT FINANCIAL ADVICE. Research only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


NY = "America/New_York"
ROOT = Path(__file__).resolve().parent.parent.parent      # repository root
L1 = ROOT / "data" / "L1"
L2 = ROOT / "data" / "L2"
CACHE = Path(__file__).resolve().parent / "cache" / "NQ_1m_yfinance.parquet"

# --- 1-fold holdout, chronological. Touch validation exactly once. ---------
ES_TEST = ("2016-01-01", "2021-12-31")
ES_VALIDATION = ("2022-01-01", "2026-07-22")
NQ_TEST_SESSIONS = [
    "2026-04-06", "2026-04-07", "2026-04-08", "2026-04-09", "2026-04-10",
    "2026-04-13", "2026-04-14", "2026-04-15", "2026-04-16", "2026-04-17",
    "2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23", "2026-04-24",
    "2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-01",
    "2026-05-04", "2026-05-05", "2026-05-06",
]
OHLC = ["open", "high", "low", "close"]

# Set by load_es_1m as a side-channel for the dropped-day count (see
# _screen_misaligned_days). load_es_1m keeps returning a bare DataFrame rather
# than a (df, count) tuple because it already has several existing call sites
# that share one result across multiple uses (fvt_data's own selftest,
# fvt_stats.py's main() which slices the same frame for both the Test and
# Validation windows) -- widening the return type would ripple a diagnostic
# int into all of them. A module-level counter keeps the call sites clean.
LAST_ES_DROPPED_DAYS: int = 0


def slice_dates(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    lo = pd.Timestamp(start, tz=NY)
    hi = pd.Timestamp(end, tz=NY) + pd.Timedelta(days=1)
    return df.loc[(df.index >= lo) & (df.index < hi)]


def _screen_misaligned_days(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop calendar days whose last bar precedes 12:00 NY, plus Saturdays
    among the early-ending days.

    A day whose last bar comes early is one of two things, and the two land
    on opposite sides of noon:

    - A **timestamp-shifted day** (the artifact this screen exists for): the
      MT5 export has ~200 days shifted 5 hours early (a local-time-as-epoch
      artifact) -- last bar 10:59 or 11:59 instead of 16:59, intraday range
      peaking at 03:30/04:30 instead of 08:30/09:30, Saturday bars appearing
      in the same week. A shifted day still HAS a bar timestamped 09:30 (it
      is just the wrong wall-clock moment wearing that label), so counting
      09:30 bars cannot see the problem -- only a same-day END-of-session
      check can.
    - A **legitimate early-close half-session** (Black Friday, Christmas Eve,
      July 3rd, and similar exchange-shortened days): these close around
      13:00-13:15 NY, not 10:59/11:59. A predicate that cuts at 16:00 cannot
      tell these apart from the shifted days and silently discards real
      trading sessions along with the artifact -- 16 of them, confirmed
      against the raw CSV.

    12:00 NY is the cut because every shifted day's last bar falls at or
    before 11:59 and every observed half-session's last bar falls at or after
    12:59 NY: nothing in the source data straddles the boundary, so 12:00
    separates the two cases cleanly without sweeping the good sessions in
    with the bad.

    Saturdays get an extra check: real ES RTH never trades on a Saturday, so
    a Saturday-dated day that already ends before 16:00 is still dropped even
    if its last bar happens to land at or after 12:00 -- it is weekend
    artifact data, not a half-session, and the 12:00 cut alone cannot
    distinguish it from a real Friday half-day. This does not touch
    Saturday-dated rows whose last bar is already at or after 16:00 (i.e.
    outside this screen's candidate set) -- fixing those is a separate,
    unreviewed change and out of scope here.
    """
    if df.empty:
        return df, 0
    days = df.index.normalize()
    last_ts_per_day = pd.Series(df.index, index=days).groupby(level=0).max()
    ends_before_16 = last_ts_per_day.dt.strftime("%H:%M") < "16:00"
    ends_before_12 = last_ts_per_day.dt.strftime("%H:%M") < "12:00"
    is_saturday = last_ts_per_day.index.dayofweek == 5
    bad_days = last_ts_per_day.index[ends_before_16 & (ends_before_12 | is_saturday)]
    if len(bad_days) == 0:
        return df, 0
    return df.loc[~days.isin(bad_days)], int(len(bad_days))


def load_es_1m(path: Path | None = None) -> pd.DataFrame:
    """MT5 export: epoch SECONDS, bar-OPEN timestamps. Verified NY-aligned.

    Screens out timestamp-shifted days (see _screen_misaligned_days) before
    returning. The dropped-day count is left in the module-level
    LAST_ES_DROPPED_DAYS for callers that want to report it.
    """
    global LAST_ES_DROPPED_DAYS
    path = path or (L1 / "EP_PERIOD_CURRENT_M1.csv")
    df = pd.read_csv(path, usecols=["time", "open", "high", "low", "close"])
    idx = pd.to_datetime(df.pop("time"), unit="s", utc=True).dt.tz_convert(NY)
    df = df.set_index(idx)[OHLC].sort_index()
    df, dropped = _screen_misaligned_days(df)
    LAST_ES_DROPPED_DAYS = dropped
    return df


def load_nq_1m_tape(session: str) -> pd.DataFrame:
    """One RTH session of NQ trade prints -> 1-minute OHLC. RTH-only by nature."""
    t = pd.read_parquet(L2 / f"NQ_{session}_trades.parquet", columns=["ts", "price"])
    idx = pd.to_datetime(t["ts"], unit="ns", utc=True).dt.tz_convert(NY)
    bars = t.set_index(idx)["price"].resample("1min").ohlc().dropna()
    return bars[OHLC].sort_index()


def load_nq_test() -> pd.DataFrame:
    return pd.concat([load_nq_1m_tape(s) for s in NQ_TEST_SESSIONS]).sort_index()


def harvest_nq_yfinance(cache: Path | None = None) -> pd.DataFrame:
    """Append-only. Yahoo caps 1m at 8 days/request, so the cache only grows by
    being run regularly. Returns the merged cache."""
    import yfinance as yf
    cache = cache or CACHE
    cache.parent.mkdir(parents=True, exist_ok=True)
    new = yf.download("NQ=F", period="8d", interval="1m",
                      progress=False, auto_adjust=False)
    if isinstance(new.columns, pd.MultiIndex):
        new.columns = new.columns.droplevel(1)
    new.columns = [str(c).lower() for c in new.columns]
    new = new[OHLC]
    new.index = pd.DatetimeIndex(new.index).tz_convert(NY)
    if cache.exists():
        old = pd.read_parquet(cache)
        old.index = pd.DatetimeIndex(old.index).tz_convert(NY)
        new = pd.concat([old, new])
    new = new[~new.index.duplicated(keep="last")].sort_index()
    new.to_parquet(cache)
    return new


def load_nq_validation(cache: Path | None = None) -> pd.DataFrame:
    """VALIDATION SPLIT — call exactly once, after the Test config is frozen."""
    cache = cache or CACHE
    if not cache.exists():
        return pd.DataFrame(columns=OHLC,
                            index=pd.DatetimeIndex([], tz=NY, name="ts"))
    df = pd.read_parquet(cache)
    df.index = pd.DatetimeIndex(df.index).tz_convert(NY)
    return df[OHLC].sort_index()


def session_count(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rth = df.between_time("09:30", "15:59")
    return int(rth.index.normalize().nunique())


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    check("ES M1 source file exists", (L1 / "EP_PERIOD_CURRENT_M1.csv").exists())
    check("23 NQ test sessions declared", len(NQ_TEST_SESSIONS) == 23)
    check("NQ test sessions are unique and sorted",
          NQ_TEST_SESSIONS == sorted(set(NQ_TEST_SESSIONS)))

    # Splits must not overlap — this is the leakage guard.
    check("ES test ends before ES validation begins",
          pd.Timestamp(ES_TEST[1]) < pd.Timestamp(ES_VALIDATION[0]))
    check("NQ test ends before the NQ validation window opens",
          pd.Timestamp(NQ_TEST_SESSIONS[-1]) < pd.Timestamp("2026-07-26"))

    es = load_es_1m()
    check("ES frame has the OHLC columns",
          list(es.columns) == ["open", "high", "low", "close"])
    check("ES index is tz-aware New York", str(es.index.tz) == NY)
    check("ES index is sorted", es.index.is_monotonic_increasing)
    check("ES spans 2016 to 2026", es.index.min().year == 2016 and es.index.max().year == 2026)
    # The whole strategy is session-anchored, so this alignment is load-bearing.
    by_min = es.groupby(es.index.strftime("%H:%M")).size()
    check("09:30 bars exist on ~2,700 sessions", 2_600 <= by_min.get("09:30", 0) <= 2_800)

    # A shifted day still HAS a bar timestamped 09:30 (just the wrong
    # wall-clock moment), so the count above cannot see the export artifact.
    # The check that actually proves alignment: which minute of day carries
    # the biggest average range. On real, correctly-anchored RTH data that is
    # the 09:30 open.
    real_by_min = (es["high"] - es["low"]).groupby(es.index.strftime("%H:%M")).mean()
    check("on the real (screened) ES file, 09:30 is the max-average-range "
          "minute of day -- proves session alignment directly, unlike merely "
          "counting how many 09:30 bars exist",
          real_by_min.idxmax() == "09:30")
    check("load_es_1m actually found and dropped misaligned days in the real "
          "export -- silently reporting 0 here would mean the screen had "
          "stopped firing",
          LAST_ES_DROPPED_DAYS > 0)

    # --- Screen correctness, proven on a controlled fixture (real data alone
    # can't demonstrate teeth: only ~8% of days are misaligned, so even with
    # the screen fully disabled the correctly-aligned majority still wins the
    # real-data vote above). 3 well-formed days (last bar into the 16:00
    # close, biggest range at the 09:30 open) plus 2 misaligned days (last bar
    # 10:59, with a deliberately huge range planted at 04:30 -- exactly the
    # kind of mislabeled-anchor spike the real shifted-epoch export produces).
    def _mkrow(ts_str: str, rng: float):
        ts = pd.Timestamp(ts_str, tz=NY)
        return ts, {"open": 100.0, "high": 100.0 + rng, "low": 100.0,
                    "close": 100.0 + rng / 2}

    rows: dict = {}
    for d in ("2030-01-02", "2030-01-03", "2030-01-04"):          # aligned
        for hm, rng in (("09:30", 5.0), ("12:00", 1.0), ("16:00", 1.0)):
            ts, row = _mkrow(f"{d} {hm}", rng)
            rows[ts] = row
    for d in ("2030-02-06", "2030-02-07"):                        # misaligned
        for hm, rng in (("04:30", 100.0), ("10:59", 1.0)):
            ts, row = _mkrow(f"{d} {hm}", rng)
            rows[ts] = row
    synth = pd.DataFrame.from_dict(rows, orient="index")[OHLC].sort_index()

    unscreened_by_min = ((synth["high"] - synth["low"])
                         .groupby(synth.index.strftime("%H:%M")).mean())
    check("sanity: the UNSCREENED synthetic frame's max-range minute is NOT "
          "09:30 -- the misaligned days' planted 04:30 spike wins outright, "
          "which is what proves this fixture can detect a missing/broken "
          "screen rather than passing vacuously",
          unscreened_by_min.idxmax() != "09:30")

    screened, dropped = _screen_misaligned_days(synth)
    check("_screen_misaligned_days drops exactly the 2 misaligned days",
          dropped == 2)
    check("_screen_misaligned_days does not touch the 3 aligned days",
          len(screened) == 9)
    screened_by_min = ((screened["high"] - screened["low"])
                       .groupby(screened.index.strftime("%H:%M")).mean())
    check("after screening, 09:30 is restored as the max-average-range "
          "minute of day",
          screened_by_min.idxmax() == "09:30")

    # --- The 16:00-vs-12:00 distinction itself, on a fixture with all three
    # day types the predicate has to tell apart. All three dates are
    # weekdays (Mon/Tue/Wed) so the Saturday guard cannot be what passes
    # this test -- only the 12:00 cut can. Full day: last bar 16:59. Shifted
    # day: last bar 10:59 (the artifact). Half-day: last bar 13:14 (a real
    # early-close session, e.g. Christmas Eve/Black Friday shaped).
    threeday_rows: dict = {}
    for hm in ("09:30", "12:00", "16:59"):                       # full day
        ts, row = _mkrow(f"2031-01-06 {hm}", 1.0)
        threeday_rows[ts] = row
    for hm in ("04:30", "10:59"):                                 # shifted day
        ts, row = _mkrow(f"2031-01-07 {hm}", 1.0)
        threeday_rows[ts] = row
    for hm in ("09:30", "13:14"):                                 # half-day
        ts, row = _mkrow(f"2031-01-08 {hm}", 1.0)
        threeday_rows[ts] = row
    threeday = pd.DataFrame.from_dict(threeday_rows, orient="index")[OHLC].sort_index()

    threeday_screened, threeday_dropped = _screen_misaligned_days(threeday)
    check("of the full/shifted/half-day trio, exactly the shifted day is dropped",
          threeday_dropped == 1)
    check("the full day (last bar 16:59) survives the 12:00 cut",
          pd.Timestamp("2031-01-06 16:59", tz=NY) in threeday_screened.index)
    check("the half-day (last bar 13:14, a real early-close session) survives "
          "the 12:00 cut -- this is the bug being fixed: a 16:00 predicate "
          "would have swept this good session out with the shifted one",
          pd.Timestamp("2031-01-08 13:14", tz=NY) in threeday_screened.index)
    check("the shifted day (last bar 10:59) does not survive",
          pd.Timestamp("2031-01-07 10:59", tz=NY) not in threeday_screened.index)
    check("no row leaks in or out beyond the 2 dropped shifted-day rows",
          len(threeday_screened) == len(threeday) - 2)

    test_es = slice_dates(es, *ES_TEST)
    val_es = slice_dates(es, *ES_VALIDATION)
    check("ES test slice is non-empty", len(test_es) > 0)
    check("ES validation slice is non-empty", len(val_es) > 0)
    check("ES slices are disjoint", test_es.index.max() < val_es.index.min())

    nq = load_nq_1m_tape(NQ_TEST_SESSIONS[0])
    check("NQ tape resamples to OHLC",
          list(nq.columns) == ["open", "high", "low", "close"])
    check("NQ tape session is RTH-only as documented",
          nq.index.min().strftime("%H:%M") == "09:30"
          and nq.index.max().strftime("%H:%M") == "15:59")
    check("NQ tape gives 390 one-minute bars", len(nq) == 390)
    check("NQ tape index is New York", str(nq.index.tz) == NY)
    check("no NaN rows survive the resample", not nq.isna().any().any())

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else 0)
