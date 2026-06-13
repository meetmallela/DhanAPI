"""
historical_data_fetch.py
------------------------
Fetches and caches 12 months of historical NIFTY / BANKNIFTY / FINNIFTY data
from Kite API (primary) with Yahoo Finance fallback for daily data.

Run once (or weekly) to refresh the cache:
    python historical_data_fetch.py
    python historical_data_fetch.py --symbols NIFTY BANKNIFTY
    python historical_data_fetch.py --daily-only
    python historical_data_fetch.py --months 12

Kite API limits per call:
    day    -> no limit (years of history)
    5min   -> max 100 trading days (~5 months)
    1min   -> max 60 trading days (~3 months)

Data stored in: backtest/hist_cache.db
  Table: hist_candles (symbol, interval, ts, open, high, low, close, volume)
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# -- Path setup ----------------------------------------------------------------
_ROOT = Path(__file__).parent
_MASTER = _ROOT.parent / "MasterConfiguration"
_LIB    = _MASTER / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

CACHE_DB = _ROOT / "backtest" / "hist_cache.db"

# -- Kite instrument tokens (spot indices) -------------------------------------
KITE_TOKENS = {
    "NIFTY":      256265,
    "BANKNIFTY":  260105,
    "FINNIFTY":   257801,
    "MIDCPNIFTY": 288009,
    "SENSEX":     265,
}

# Yahoo tickers as fallback for daily data
YAHOO_TICKERS = {
    "NIFTY":      "^NSEI",
    "BANKNIFTY":  "^NSEBANK",
    "FINNIFTY":   "NIFTY_FIN_SERVICE.NS",
    "SENSEX":     "^BSESN",
    "MIDCPNIFTY": "NIFTY_MIDCAP_SELECT.NS",
}

# Kite interval strings
_KITE_INTERVALS = {
    "day":   "day",
    "5min":  "5minute",
    "1min":  "minute",
    "15min": "15minute",
}

# Max CALENDAR days Kite allows per API call (not trading days)
_KITE_CHUNK_CAL_DAYS = {
    "day":   1000,   # no real limit for daily
    "5min":  90,     # Kite limit is 100 cal days; use 90 to be safe
    "1min":  55,     # Kite limit is 60 cal days; use 55 to be safe
    "15min": 180,    # Kite limit is 200 cal days; use 180 to be safe
}


# -- DB helpers ----------------------------------------------------------------

def _db_conn() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB), timeout=15)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hist_candles (
            symbol   TEXT NOT NULL,
            interval TEXT NOT NULL,
            ts       TEXT NOT NULL,
            open     REAL,
            high     REAL,
            low      REAL,
            close    REAL,
            volume   REAL,
            PRIMARY KEY (symbol, interval, ts)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hist_sym_int_ts
        ON hist_candles (symbol, interval, ts)
    """)
    conn.commit()
    return conn


def _store(symbol: str, interval: str, df: pd.DataFrame):
    if df is None or df.empty:
        return
    conn = _db_conn()
    rows = [
        (symbol, interval, str(r["timestamp"]),
         r["open"], r["high"], r["low"], r["close"], r.get("volume", 0) or 0)
        for _, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO hist_candles VALUES (?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()
    conn.close()


def load(symbol: str, interval: str,
         from_date: str, to_date: str) -> pd.DataFrame:
    """Load cached candles for a symbol/interval/date range."""
    conn = _db_conn()
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume
           FROM   hist_candles
           WHERE  symbol=? AND interval=? AND ts>=? AND ts<=?
           ORDER BY ts""",
        (symbol, interval, from_date, to_date + " 23:59:59")
    ).fetchall()
    conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def cached_dates(symbol: str, interval: str,
                 from_date: str, to_date: str) -> set:
    """Return set of date strings already in cache for this symbol/interval."""
    df = load(symbol, interval, from_date, to_date)
    if df.empty:
        return set()
    return set(df["timestamp"].dt.strftime("%Y-%m-%d").unique())


# -- Trading day utilities -----------------------------------------------------

def trading_days(from_date: str, to_date: str) -> list[str]:
    """Return list of Mon-Fri date strings between from_date and to_date."""
    days = []
    cur  = datetime.strptime(from_date, "%Y-%m-%d")
    end  = datetime.strptime(to_date,   "%Y-%m-%d")
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days


def chunk_calendar(from_date: str, to_date: str,
                   cal_days: int) -> list[tuple[str, str]]:
    """Split a date range into (from, to) pairs each spanning at most cal_days calendar days."""
    chunks = []
    cur = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date,   "%Y-%m-%d")
    while cur <= end:
        chunk_end = min(cur + timedelta(days=cal_days - 1), end)
        chunks.append((cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cur = chunk_end + timedelta(days=1)
    return chunks


# -- Kite fetch ----------------------------------------------------------------

def _get_kite():
    """Return a working KiteConnect instance using the refreshed token."""
    try:
        from kite_candle_store import get_kite
        kite = get_kite()
        if kite is None:
            raise RuntimeError("kite_candle_store.get_kite() returned None")
        return kite
    except Exception as e:
        print(f"  [Kite] Init failed: {e}")
        return None


def _kite_historical(kite, token: int, from_dt: datetime, to_dt: datetime,
                     interval: str) -> pd.DataFrame:
    """Call kite.historical_data and return a clean DataFrame."""
    try:
        raw = kite.historical_data(token, from_dt, to_dt, interval,
                                   continuous=False, oi=False)
    except Exception as e:
        print(f"    API error: {e}")
        return pd.DataFrame()

    if not raw:
        return pd.DataFrame()

    df = pd.DataFrame(raw)
    if "date" in df.columns:
        df.rename(columns={"date": "timestamp"}, inplace=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = 0
    return df[["timestamp", "open", "high", "low", "close", "volume"]].dropna(subset=["open"])


def fetch_kite(symbol: str, interval: str, from_date: str, to_date: str,
               kite=None, verbose: bool = True) -> pd.DataFrame:
    """
    Fetch historical candles from Kite API for symbol/interval, caching results.

    interval: 'day', '5min', '1min', '15min'
    """
    if kite is None:
        kite = _get_kite()
    if kite is None:
        print(f"  [Kite] No client -- cannot fetch {symbol} {interval}")
        return pd.DataFrame()

    token       = KITE_TOKENS.get(symbol)
    if token is None:
        print(f"  [Kite] Unknown symbol: {symbol}")
        return pd.DataFrame()

    kite_int    = _KITE_INTERVALS[interval]
    cal_limit   = _KITE_CHUNK_CAL_DAYS[interval]

    all_days    = trading_days(from_date, to_date)
    cached      = cached_dates(symbol, interval, from_date, to_date)
    missing     = [d for d in all_days if d not in cached]

    if not missing:
        if verbose:
            cached_df = load(symbol, interval, from_date, to_date)
            print(f"  [{symbol} {interval}] All cached -- {len(cached_df)} bars")
        return load(symbol, interval, from_date, to_date)

    # Find earliest/latest missing date and chunk by calendar days
    miss_from = missing[0]
    miss_to   = missing[-1]
    chunks    = chunk_calendar(miss_from, miss_to, cal_limit)
    if verbose:
        print(f"  [{symbol} {interval}] {len(missing)} days missing -> {len(chunks)} API call(s)")

    frames = []
    for c_from, c_to in chunks:
        if verbose:
            print(f"    Fetching {c_from} -> {c_to} ...", end=" ", flush=True)
        from_dt = datetime.strptime(c_from, "%Y-%m-%d")
        to_dt   = datetime.strptime(c_to,   "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        df_chunk = _kite_historical(kite, token, from_dt, to_dt, kite_int)
        if df_chunk.empty:
            if verbose:
                print("no data")
            continue
        if verbose:
            print(f"{len(df_chunk)} bars")
        _store(symbol, interval, df_chunk)
        frames.append(df_chunk)
        time.sleep(0.35)   # respect Kite rate limit (~3 req/s)

    return load(symbol, interval, from_date, to_date)


# -- Yahoo Finance daily fallback ----------------------------------------------

def fetch_yahoo_daily(symbol: str, from_date: str, to_date: str,
                      verbose: bool = True) -> pd.DataFrame:
    """Fetch daily OHLCV from Yahoo Finance. No API key needed."""
    import urllib.request, json, calendar

    ticker = YAHOO_TICKERS.get(symbol)
    if ticker is None:
        print(f"  [Yahoo] No ticker for {symbol}")
        return pd.DataFrame()

    def _unix(d: str) -> int:
        return int(calendar.timegm(datetime.strptime(d, "%Y-%m-%d").timetuple()))

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&period1={_unix(from_date)}&period2={_unix(to_date)+86400}"
        f"&includePrePost=false"
    )
    if verbose:
        print(f"  [Yahoo daily] {symbol} ({ticker}) {from_date} -> {to_date} ...", end=" ", flush=True)
    try:
        req  = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        })
        resp = urllib.request.urlopen(req, timeout=20)
        data = json.loads(resp.read())
        res  = data["chart"]["result"][0]
        ts   = res["timestamp"]
        q    = res["indicators"]["quote"][0]
        df   = pd.DataFrame({
            "timestamp": pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None),
            "open":   q["open"],
            "high":   q["high"],
            "low":    q["low"],
            "close":  q["close"],
            "volume": q["volume"],
        }).dropna(subset=["open"])
        df = df[df["timestamp"].dt.weekday < 5].reset_index(drop=True)
        if verbose:
            print(f"{len(df)} bars")
        _store(symbol, "day", df)
        return df
    except Exception as e:
        if verbose:
            print(f"ERROR: {e}")
        return pd.DataFrame()


# -- Main entry point ----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch 12M historical data for backtesting")
    parser.add_argument("--symbols", nargs="+",
                        default=["NIFTY", "BANKNIFTY"],
                        help="Symbols to fetch (default: NIFTY BANKNIFTY)")
    parser.add_argument("--months", type=int, default=12,
                        help="How many months back to fetch (default: 12)")
    parser.add_argument("--daily-only", action="store_true",
                        help="Only fetch daily candles (skip 5m)")
    parser.add_argument("--intraday-only", action="store_true",
                        help="Only fetch 5m candles (skip daily)")
    parser.add_argument("--yahoo-fallback", action="store_true",
                        help="Use Yahoo Finance for daily instead of Kite")
    args = parser.parse_args()

    today     = datetime.now()
    from_date = (today - timedelta(days=args.months * 31)).strftime("%Y-%m-%d")
    to_date   = today.strftime("%Y-%m-%d")

    print(f"=== Historical Data Fetch ===")
    print(f"Symbols  : {', '.join(args.symbols)}")
    print(f"Window   : {from_date} -> {to_date} ({args.months} months)")
    print(f"Cache DB : {CACHE_DB}")
    print()

    kite = None if args.yahoo_fallback else _get_kite()
    if kite:
        print(f"[Kite] Connected successfully\n")
    else:
        print(f"[Kite] Not available -- using Yahoo Finance for daily data\n")

    total_bars = {}

    for sym in args.symbols:
        print(f"== {sym} ==")

        # Daily candles
        if not args.intraday_only:
            if kite and not args.yahoo_fallback:
                df_day = fetch_kite(sym, "day", from_date, to_date, kite=kite)
            else:
                df_day = fetch_yahoo_daily(sym, from_date, to_date)
            total_bars[f"{sym}/day"] = len(df_day)

        # 5m candles -- chunk into 100-day windows
        if not args.daily_only:
            if kite:
                df_5m = fetch_kite(sym, "5min", from_date, to_date, kite=kite)
                total_bars[f"{sym}/5min"] = len(df_5m)
            else:
                print(f"  [{sym} 5min] Kite not available -- 5m requires Kite token")

        print()

    print("=== Summary ===")
    for key, cnt in total_bars.items():
        sym, interval = key.split("/")
        print(f"  {sym:12s} {interval:5s}: {cnt:6d} bars cached")

    print(f"\nCache DB: {CACHE_DB}")
    print("Done. Run pattern_discovery.py next.")


if __name__ == "__main__":
    main()
