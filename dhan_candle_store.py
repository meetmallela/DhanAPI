"""
dhan_candle_store.py
--------------------
Persistent 1-min (and resampled 5-min / 15-min) candle store using Dhan API.

Replaces Kite as the historical data source for OmniEngine warmup.

Storage  : MasterConfiguration/data/kite_candles.db  (shared schema)
Policy   : INSERT OR IGNORE — never overwrites existing candles
Intervals: 1-min stored raw; 5-min and 15-min resampled on read

Public API
----------
seed_engine(data_dict)
    Populate OmniEngine's self.data dict from local DB + Dhan API fetch.
    Call once at OmniEngine startup instead of _seed_from_kite().

save_live_candle(symbol, interval_min, row)
    Append a single completed candle to the DB.
    Call from OmniEngine each time a new candle closes.

fetch_and_store_today(symbols=None)
    Pull today's 1-min candles from Dhan API and persist to DB.

db_stats()
    Return row/date/instrument counts for monitoring.
"""

import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz

from master_resource import MasterResource

# ── Kite candle store (primary data source) ───────────────────────────────────
_lib = Path(MasterResource.MASTER_ROOT) / "lib"
if str(_lib) not in sys.path:
    sys.path.insert(0, str(_lib))

from kite_candle_store import (
    get_candles    as _kcs_get,
    ensure_tables  as _kcs_ensure,
    save_to_db     as _kcs_save,
    load_from_db   as _kcs_load,
    INDEX_TOKENS   as KITE_INDEX_TOKENS,
    _BSE_SYMS,
)

# ── Constants ─────────────────────────────────────────────────────────────────
IST     = pytz.timezone("Asia/Kolkata")
DB_PATH = Path(MasterResource.MASTER_ROOT) / "data" / "kite_candles.db"

# All tracked indices with their Kite tokens and exchange
INDEX_META = {
    "NIFTY":      (256265, "NSE"),
    "BANKNIFTY":  (260105, "NSE"),
    "FINNIFTY":   (257801, "NSE"),
    "MIDCPNIFTY": (288009, "NSE"),
    "SENSEX":     (265,    "BSE"),
}

logger = logging.getLogger("dhan_candle_store")

# ── DB helpers — delegate to kite_candle_store ────────────────────────────────

def ensure_tables():
    _kcs_ensure()


def _kite_interval(interval_min: int) -> str:
    return {1: "minute", 5: "5minute", 15: "15minute"}.get(interval_min, "minute")


def _save_df(symbol: str, df: pd.DataFrame, interval_min: int):
    """Append-only save via kite_candle_store (INSERT OR IGNORE)."""
    if df is None or df.empty:
        return
    token    = INDEX_META.get(symbol, (0, "NSE"))[0]
    exchange = "BSE" if symbol in _BSE_SYMS else "NSE"
    _kcs_save(token, symbol, exchange, df, _kite_interval(interval_min))
    logger.debug(f"[DB] {symbol} {interval_min}m: {len(df)} rows saved")


def save_live_candle(symbol: str, interval_min: int, row: dict):
    """Persist a single completed candle row."""
    ensure_tables()
    _save_df(symbol, pd.DataFrame([row]), interval_min)


def _load_db(symbol: str, interval_min: int, date_str: str) -> pd.DataFrame:
    token = INDEX_META.get(symbol, (0, "NSE"))[0]
    return _kcs_load(token, date_str, _kite_interval(interval_min))


def _resample(df1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Resample 1-min DataFrame to N-min OHLCV."""
    if df1m.empty:
        return pd.DataFrame()
    df = df1m.set_index("timestamp").sort_index()
    rule = f"{minutes}min"
    rs = df.resample(rule, closed="left", label="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna(subset=["open"])
    rs = rs.reset_index().rename(columns={"timestamp": "timestamp"})
    return rs



def save_from_nse_feed(nse_feed, symbols: list[str] | None = None):
    """
    Pull completed 1-min candles from a running NSELiveFeed and persist to DB.
    Call this periodically (e.g. every minute) from OmniEngine.
    Only saves candles whose timestamp < current minute (i.e. closed candles).
    """
    ensure_tables()
    if symbols is None:
        symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    now_floor = pd.Timestamp.now(tz=IST).floor("1min")
    saved_total = 0
    for sym in symbols:
        df1m = nse_feed.get_candles(sym, 1)
        if df1m is None or df1m.empty:
            continue
        # Only completed candles (not the currently-forming one)
        completed = df1m[df1m["timestamp"] < now_floor].copy()
        if completed.empty:
            continue
        _save_df(sym, completed, 1)
        saved_total += len(completed)
    if saved_total:
        logger.debug(f"[PERSIST] Saved {saved_total} completed 1-min candles to DB")


def save_from_data_dict(data_dict: dict, symbols: list[str] | None = None):
    """
    Persist completed 1-min candles for indices whose source is Kite (e.g. SENSEX,
    MIDCPNIFTY) — not available on the NSE live feed used by save_from_nse_feed().

    data_dict is DhanOmniEngine.self.data — reads keys like "SENSEX_1m".
    Only completed candles (timestamp < current floored minute) are saved.
    """
    ensure_tables()
    if symbols is None:
        symbols = ["SENSEX", "MIDCPNIFTY"]
    now_floor   = pd.Timestamp.now(tz=IST).floor("1min")
    saved_total = 0
    for sym in symbols:
        df1m = data_dict.get(f"{sym}_1m")
        if df1m is None or df1m.empty:
            continue
        ts = pd.to_datetime(df1m["timestamp"])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize(IST)
        else:
            ts = ts.dt.tz_convert(IST)
        completed = df1m[ts < now_floor].copy()
        if completed.empty:
            continue
        _save_df(sym, completed, 1)
        saved_total += len(completed)
    if saved_total:
        logger.debug(f"[PERSIST] BSE/MIDCP: saved {saved_total} completed 1-min candles")


def fetch_and_store_today(symbols: list[str] | None = None):
    """
    Fetch today's 1-min candles from Kite API and persist to DB.
    Skips symbols already fully cached for today.
    """
    ensure_tables()
    if symbols is None:
        symbols = list(INDEX_META.keys())
    today = date.today().strftime("%Y-%m-%d")
    for sym in symbols:
        meta = INDEX_META.get(sym)
        if not meta:
            continue
        token, exchange = meta
        try:
            df, src = _kcs_get(token, sym, exchange, today, "minute")
            if df is not None and not df.empty:
                _save_df(sym, _resample(df, 5),  5)
                _save_df(sym, _resample(df, 15), 15)
                logger.info(f"[FETCH] {sym}: {len(df)} 1m candles from {src} for {today}")
            else:
                logger.debug(f"[FETCH] {sym}: no data for {today} (market closed or weekend)")
        except Exception as e:
            logger.warning(f"[FETCH] {sym} failed: {e}")
        time.sleep(0.2)


# ── OmniEngine seed ───────────────────────────────────────────────────────────

def seed_engine(data_dict: dict, indices: list[str] | None = None, min_candles: int = 5,
                lookback_days: int = 5) -> dict:
    """
    Populate OmniEngine's self.data dict from locally stored 1-min candles.
    Loads the last `lookback_days` trading days so strategies that need many
    candles (e.g. OptionScalper EMA44 needing 46 15m bars) are warm on startup.

    Returns dict of {key: candle_count} for logging.
    """
    ensure_tables()
    if indices is None:
        indices = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]

    # Build list of last N trading days (Mon-Fri), most recent last
    trading_days = []
    d = date.today()
    trading_days.append(d.strftime("%Y-%m-%d"))   # always include today
    while len(trading_days) < lookback_days + 1:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            trading_days.insert(0, d.strftime("%Y-%m-%d"))

    seeded = {}
    for idx in indices:
        frames = []
        for ds in trading_days:
            df_day = _load_db(idx, 1, ds)
            if not df_day.empty:
                frames.append(df_day)

        if not frames:
            logger.debug(f"[SEED] {idx}: no candles in DB (will warm from live feed)")
            continue

        df1m = pd.concat(frames, ignore_index=True)
        df1m["timestamp"] = pd.to_datetime(df1m["timestamp"])
        if df1m["timestamp"].dt.tz is not None:
            df1m["timestamp"] = df1m["timestamp"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        df1m = (df1m.drop_duplicates(subset="timestamp")
                    .sort_values("timestamp")
                    .reset_index(drop=True))

        df5m  = _resample(df1m, 5)
        df15m = _resample(df1m, 15)

        data_dict[f"{idx}_1m"]  = df1m.tail(400).reset_index(drop=True)
        data_dict[f"{idx}_5m"]  = df5m.tail(150).reset_index(drop=True)
        data_dict[f"{idx}_15m"] = df15m.tail(100).reset_index(drop=True)

        seeded[f"{idx}_1m"]  = len(data_dict[f"{idx}_1m"])
        seeded[f"{idx}_5m"]  = len(data_dict[f"{idx}_5m"])
        seeded[f"{idx}_15m"] = len(data_dict[f"{idx}_15m"])

        logger.info(f"[SEED] {idx}: days={len(frames)}  1m={len(df1m)}  "
                    f"5m={len(df5m)}  15m={len(df15m)}")

    ready = [k for k, v in seeded.items() if v >= min_candles]
    if ready:
        logger.info(f"[SEED] Restored from DB — ready: {ready}")
    else:
        logger.info("[SEED] No DB candles yet — NSE live feed will warm up from scratch")
    return seeded


# ── Stats ─────────────────────────────────────────────────────────────────────

def db_stats() -> dict:
    try:
        con   = _conn()
        stats = {}
        for t in ("candles_1min", "candles_5min", "candles_15min"):
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            d = con.execute(f"SELECT COUNT(DISTINCT substr(dt,1,10)) FROM {t}").fetchone()[0]
            i = con.execute(f"SELECT COUNT(DISTINCT instrument_token) FROM {t}").fetchone()[0]
            stats[t] = {"rows": n, "dates": d, "instruments": i}
        con.close()
        return stats
    except Exception as e:
        return {"error": str(e)}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - DHAN_CANDLE - %(levelname)s - %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "stats":
        ensure_tables()
        s = db_stats()
        print("\n=== kite_candles.db stats (Dhan source) ===")
        for t, v in s.items():
            if isinstance(v, dict):
                print(f"  {t:<20} rows={v['rows']:>8,}  dates={v['dates']:>4}  instruments={v['instruments']:>5}")
        print()

    elif cmd == "fetch":
        print(f"Fetching today's 1-min candles for all indices...")
        fetch_and_store_today()
        print("Done.")

    elif cmd == "backfill":
        # python dhan_candle_store.py backfill 5
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        print(f"Backfilling last {n} trading days via Kite...")
        ensure_tables()
        d = date.today()
        filled = 0
        while filled < n:
            d -= timedelta(days=1)
            if d.weekday() >= 5:
                continue
            ds = d.strftime("%Y-%m-%d")
            print(f"  >> {ds}")
            for sym, (token, exchange) in INDEX_META.items():
                try:
                    df, src = _kcs_get(token, sym, exchange, ds, "minute")
                    if df is not None and not df.empty:
                        _save_df(sym, _resample(df, 5),  5)
                        _save_df(sym, _resample(df, 15), 15)
                        print(f"     {sym}: {len(df)} 1m candles ({src})")
                    else:
                        print(f"     {sym}: no data")
                    time.sleep(0.2)
                except Exception as e:
                    print(f"     {sym}: ERROR {e}")
            filled += 1
        print("Backfill done.")
