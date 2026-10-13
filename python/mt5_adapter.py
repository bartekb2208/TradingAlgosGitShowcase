"""mt5_adapter.py — clean MT5 daily CSV exports into standard OHLC.

MT5 daily exports carry traps (see research/mr-v1-connors-rsi2/RESULTS.md): `time` is epoch
SECONDS, and "daily" files include Saturday/Sunday globex partial bars that
distort RSI/ATR/SMA. This module produces one clean Mon-Fri bar per session.
"""
from __future__ import annotations
from pathlib import Path
import sys
import pandas as pd


def clean_mt5_daily(raw: pd.DataFrame) -> pd.DataFrame:
    dt = pd.to_datetime(raw["time"], unit="s", utc=True).dt.tz_localize(None).dt.normalize()
    df = raw.assign(Date=dt)
    df = df[df["Date"].dt.dayofweek < 5]                      # Mon-Fri only
    agg = (
        df.groupby("Date")
        .agg(Open=("open", "first"), High=("high", "max"),
             Low=("low", "min"), Close=("close", "last"), Volume=("tickvol", "sum"))
        .reset_index()
        .sort_values("Date")
    )
    return agg


def clean_file(src: str, dst: str) -> int:
    out = clean_mt5_daily(pd.read_csv(src))
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(dst, index=False)
    return len(out)


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    # Build a raw frame: Fri, Sat, Sun, Mon (epoch seconds at UTC midnight).
    base = pd.Timestamp("2016-01-01")  # a Friday
    times = [int((base + pd.Timedelta(days=d)).timestamp()) for d in range(4)]
    raw = pd.DataFrame({
        "time": times,
        "open": [10, 11, 12, 13], "high": [10.5, 11.5, 12.5, 13.5],
        "low": [9.5, 10.5, 11.5, 12.5], "close": [10.2, 11.2, 12.2, 13.2],
        "tickvol": [100, 5, 5, 100], "realvol": [0, 0, 0, 0],
    })
    out = clean_mt5_daily(raw)
    check("drops Sat+Sun", len(out) == 2)
    check("keeps Fri & Mon", list(out["Date"].dt.dayofweek) == [4, 0])
    check("columns exact", list(out.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"])
    check("ohlc mapped", out.iloc[0]["Close"] == 10.2 and out.iloc[0]["High"] == 10.5)

    print()
    if failures:
        print(f"SELFTEST FAILED: {failures}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest() if "--selftest" in sys.argv else 0)
