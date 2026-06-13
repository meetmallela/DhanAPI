"""
futures_candle_store.py
-----------------------
Fetch and persist 1-min OHLCV + OI candles for index futures contracts:
  NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX

Why futures and not spot?
  The spot NIFTY index has volume=0 (derived price index, not traded).
  Futures carry real traded volume + open interest (OI) — essential for
  blast / spike pattern analysis (NiftyBlast1 etc.)

API used
  kite.historical_data(instrument_token, from_dt, to_dt, interval)
  Tokens are resolved dynamically from Kite instruments API (NFO/BFO).
  Dhan scrip master CSV is kept as a reference but no longer used for fetching.

Storage
  New tables in the shared kite_candles.db:
    candles_futures_1min   — primary 1-min bars
    candles_futures_5min   — resampled on write
    candles_futures_15min  — resampled on write

  Symbol naming: "NIFTY_FUT", "BANKNIFTY_FUT" etc. (generic, continuous)

Public API
  ensure_tables()
  get_futures_security_id(index, expiry_offset=0)  → (security_id, trading_sym)
  fetch_and_store(client, symbol, from_date, to_date)  → rows_saved
  backfill(client, lookback_days=10, symbols=None)  → {sym: rows}
  seed_futures_to_engine(data_dict, symbols, lookback_days=5)  → {key: count}
  futures_db_stats()  → {key: {rows, from, to}}
"""

import sqlite3
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

logger = logging.getLogger("futures_candle_store")

# ── Paths ──────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent / "MasterConfiguration"
DB_PATH      = _ROOT / "data" / "kite_candles.db"
SCRIP_MASTER = _ROOT / "data" / "dhan_scrip_master.csv"

# ── Futures config ─────────────────────────────────────────────────────────────
# Maps index name → Dhan segment + storage symbol key
FUTURES_CONFIG: dict[str, dict] = {
    "NIFTY":      {"segment": "NSE_FNO", "instrument": "FUTIDX", "sym": "NIFTY_FUT"},
    "BANKNIFTY":  {"segment": "NSE_FNO", "instrument": "FUTIDX", "sym": "BANKNIFTY_FUT"},
    "FINNIFTY":   {"segment": "NSE_FNO", "instrument": "FUTIDX", "sym": "FINNIFTY_FUT"},
    "MIDCPNIFTY": {"segment": "NSE_FNO", "instrument": "FUTIDX", "sym": "MIDCPNIFTY_FUT"},
    "SENSEX":     {"segment": "BSE_FNO", "instrument": "FUTIDX", "sym": "SENSEX_FUT"},
}

# How long to pause between API calls (avoid rate-limit)
API_PAUSE = 0.8

# Resample 1m → these intervals on write
RESAMPLE_INTERVALS = [5, 15]

# ── Scrip master lookup ────────────────────────────────────────────────────────

_scrip_df: pd.DataFrame | None = None

def _load_scrip_master() -> pd.DataFrame:
    global _scrip_df
    if _scrip_df is not None:
        return _scrip_df
    if not SCRIP_MASTER.exists():
        logger.warning("[FUT] Scrip master not found — run StrikeLookup first")
        return pd.DataFrame()
    df = pd.read_csv(str(SCRIP_MASTER), low_memory=False)
    _scrip_df = df[df["SEM_INSTRUMENT_NAME"] == "FUTIDX"].copy()
    _scrip_df["SEM_EXPIRY_DATE"] = pd.to_datetime(
        _scrip_df["SEM_EXPIRY_DATE"], errors="coerce"
    )
    logger.info(f"[FUT] Scrip master loaded: {len(_scrip_df)} FUTIDX rows")
    return _scrip_df


def get_futures_security_id(index: str, expiry_offset: int = 0) -> tuple[str, str]:
    """
    Return (security_id, trading_symbol) for the near-month futures contract.

    expiry_offset=0 → near month (current)
    expiry_offset=1 → next month (for rollover overlap)
    """
    # Map index name to prefix in trading symbol
    sym_prefix_map = {
        "NIFTY":      "NIFTY-",
        "BANKNIFTY":  "BANKNIFTY-",
        "FINNIFTY":   "FINNIFTY-",
        "MIDCPNIFTY": "MIDCPNIFTY-",
        "SENSEX":     "SENSEX-",
    }
    prefix = sym_prefix_map.get(index.upper())
    if not prefix:
        raise ValueError(f"Unknown index: {index}")

    df = _load_scrip_master()
    if df.empty:
        raise RuntimeError("Scrip master unavailable")

    # Filter to this index, future dates only, sorted by expiry
    today = pd.Timestamp(date.today())
    subset = df[
        df["SEM_TRADING_SYMBOL"].str.startswith(prefix, na=False) &
        (df["SEM_EXPIRY_DATE"] >= today)
    ].sort_values("SEM_EXPIRY_DATE")

    if len(subset) <= expiry_offset:
        raise RuntimeError(
            f"Not enough future expiries for {index} (need offset {expiry_offset})"
        )

    row = subset.iloc[expiry_offset]
    sec_id  = str(int(row["SEM_SMST_SECURITY_ID"]))
    trading = str(row["SEM_TRADING_SYMBOL"])
    return sec_id, trading


def get_futures_kite_token(index: str, expiry_offset: int = 0) -> tuple[int, str]:
    """
    Return (kite_instrument_token, kite_tradingsymbol) for the near-month
    futures contract using Kite instruments API (NFO for NSE indices, BFO for SENSEX).

    expiry_offset=0 → near month (current)
    expiry_offset=1 → next month (for rollover overlap)
    """
    import sys as _sys
    _lib = str(Path(__file__).resolve().parent.parent / "MasterConfiguration" / "lib")
    if _lib not in _sys.path:
        _sys.path.insert(0, _lib)
    from kite_candle_store import _kite_instruments

    cfg = FUTURES_CONFIG.get(index.upper())
    if not cfg:
        raise ValueError(f"Unknown index: {index}")

    exchange = "BFO" if cfg["segment"] == "BSE_FNO" else "NFO"
    df = _kite_instruments(exchange)
    if df is None or df.empty:
        raise RuntimeError(f"Could not load {exchange} instruments from Kite")

    today = date.today()
    hits = df[
        (df["name"]            == index.upper()) &
        (df["instrument_type"] == "FUT") &
        (df["expiry"].apply(lambda d: d >= today))
    ].sort_values("expiry")

    if len(hits) <= expiry_offset:
        raise RuntimeError(
            f"Not enough future expiries for {index} in Kite {exchange} "
            f"(need offset {expiry_offset}, found {len(hits)})"
        )

    row = hits.iloc[expiry_offset]
    return int(row["instrument_token"]), str(row["tradingsymbol"])


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=15)
    c.row_factory = sqlite3.Row
    return c


def ensure_tables():
    """Create futures candle tables if they don't exist yet."""
    ddl_tpl = """
    CREATE TABLE IF NOT EXISTS candles_futures_{iv}min (
        tradingsymbol TEXT NOT NULL,
        dt            TEXT NOT NULL,
        open          REAL,
        high          REAL,
        low           REAL,
        close         REAL,
        volume        INTEGER,
        oi            INTEGER,
        PRIMARY KEY (tradingsymbol, dt)
    );
    CREATE INDEX IF NOT EXISTS idx_fut_{iv}min_sym_dt
        ON candles_futures_{iv}min (tradingsymbol, dt);
    """
    with _conn() as con:
        for iv in [1] + RESAMPLE_INTERVALS:
            con.executescript(ddl_tpl.format(iv=iv))
    logger.info("[FUT] Tables ready: candles_futures_1min / 5min / 15min")


def _save_df(sym: str, df: pd.DataFrame, interval_min: int):
    if df.empty:
        return
    tbl  = f"candles_futures_{interval_min}min"
    rows = []
    for _, r in df.iterrows():
        ts = r.get("timestamp")
        if hasattr(ts, "isoformat"):
            ts = ts.isoformat()
        rows.append((
            sym,
            str(ts),
            float(r.get("open",   0) or 0),
            float(r.get("high",   0) or 0),
            float(r.get("low",    0) or 0),
            float(r.get("close",  0) or 0),
            int(  r.get("volume", 0) or 0),
            int(  r.get("oi",     0) or 0),
        ))
    sql = (
        f"INSERT OR REPLACE INTO {tbl} "
        f"(tradingsymbol,dt,open,high,low,close,volume,oi) VALUES (?,?,?,?,?,?,?,?)"
    )
    with _conn() as con:
        con.executemany(sql, rows)
    logger.debug(f"[FUT] {sym} {interval_min}m: {len(rows)} rows saved")


def _resample(df1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if df1m.empty:
        return pd.DataFrame()
    df = df1m.copy()
    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    agg = {"open":"first","high":"max","low":"min","close":"last",
           "volume":"sum","oi":"last"}
    present = {k: v for k, v in agg.items() if k in df.columns}
    rs = (df.resample(f"{minutes}min", label="left", closed="left")
            .agg(present)
            .dropna(subset=["close"])
            .reset_index())
    return rs


# ── API response parser ────────────────────────────────────────────────────────

def _parse_response(raw: dict) -> pd.DataFrame:
    """
    Parse Dhan intraday_minute_data response.
    Response format:
      { "open":[...], "high":[...], "low":[...], "close":[...],
        "volume":[...], "oi":[...], "start_Time":[unix_epoch,...] }
    """
    if not raw or not isinstance(raw, dict):
        return pd.DataFrame()
    if not {"open","high","low","close"}.issubset(raw.keys()):
        return pd.DataFrame()
    n = len(raw["open"])
    if n == 0:
        return pd.DataFrame()

    df = pd.DataFrame({
        "open":   [float(x) for x in raw["open"]],
        "high":   [float(x) for x in raw["high"]],
        "low":    [float(x) for x in raw["low"]],
        "close":  [float(x) for x in raw["close"]],
        "volume": [int(x)   for x in raw.get("volume", [0]*n)],
        "oi":     [int(x)   for x in raw.get("oi",     [0]*n)],
    })

    ts_key = next(
        (k for k in ("start_Time","timestamp","time") if k in raw), None
    )
    if ts_key:
        df["timestamp"] = (
            pd.to_datetime(raw[ts_key], unit="s", utc=True)
            .dt.tz_convert("Asia/Kolkata")
            .dt.tz_localize(None)
        )
    else:
        df["timestamp"] = pd.NaT

    return (df.dropna(subset=["timestamp"])
              .sort_values("timestamp")
              .reset_index(drop=True))


# ── Fetch + store one symbol / date range ──────────────────────────────────────

def fetch_and_store(symbol: str,
                    from_date: str, to_date: str,
                    interval: int = 1,
                    client=None) -> int:
    """
    Fetch futures candles for one index over a date range via Kite API and persist.

    Args:
        symbol    : index name, e.g. "NIFTY"
        from_date : "YYYY-MM-DD"
        to_date   : "YYYY-MM-DD"
        interval  : 1 (default), 5, 15
        client    : unused — kept for backward-compatible call sites

    Returns:
        Number of 1m rows saved.
    """
    cfg = FUTURES_CONFIG.get(symbol.upper())
    if not cfg:
        logger.warning(f"[FUT] Unknown symbol: {symbol}")
        return 0

    sym_store = cfg["sym"]

    # Resolve Kite instrument token for near-month futures
    try:
        kite_token, trading_sym = get_futures_kite_token(symbol, expiry_offset=0)
    except Exception as e:
        logger.warning(f"[FUT] Cannot resolve Kite token for {symbol}: {e}")
        return 0

    import sys as _sys
    _lib = str(Path(__file__).resolve().parent.parent / "MasterConfiguration" / "lib")
    if _lib not in _sys.path:
        _sys.path.insert(0, _lib)
    from kite_candle_store import get_kite
    kite = get_kite()

    kite_interval = {1: "minute", 5: "5minute", 15: "15minute"}.get(interval, "minute")
    logger.info(
        f"[FUT] Fetching {sym_store} ({trading_sym} token={kite_token}) "
        f"{from_date}→{to_date} interval={interval}m via Kite"
    )

    try:
        raw = kite.historical_data(
            kite_token,
            f"{from_date} 09:15:00",
            f"{to_date} 15:30:00",
            kite_interval,
            continuous=False,
            oi=True,
        )
    except Exception as e:
        logger.warning(f"[FUT] Kite API error for {symbol}: {e}")
        return 0

    if not raw:
        logger.debug(f"[FUT] No data from Kite for {symbol} {from_date}→{to_date}")
        return 0

    df1m = pd.DataFrame(raw).rename(columns={"date": "timestamp"})
    df1m["timestamp"] = pd.to_datetime(df1m["timestamp"])
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df1m.columns:
            df1m[col] = 0
    if "oi" not in df1m.columns:
        df1m["oi"] = 0
    df1m = df1m[["timestamp", "open", "high", "low", "close", "volume", "oi"]]

    if df1m.empty:
        logger.warning(f"[FUT] Empty DataFrame after parsing for {symbol}")
        return 0

    _save_df(sym_store, df1m, 1)
    for iv in RESAMPLE_INTERVALS:
        dfN = _resample(df1m, iv)
        if not dfN.empty:
            _save_df(sym_store, dfN, iv)

    vol_sum = int(df1m["volume"].sum())
    oi_last = int(df1m["oi"].iloc[-1])
    logger.info(
        f"[FUT] {sym_store}: {len(df1m)} 1m bars saved "
        f"| total_vol={vol_sum:,} | last_oi={oi_last:,}"
    )
    return len(df1m)


# ── Public store helper (Kite-fetched df → DB) ────────────────────────────────

def store_dataframe(symbol: str, df1m: pd.DataFrame) -> int:
    """
    Save a 1m OHLCV+OI DataFrame into the futures candle tables.
    Also resamples to 5m and 15m automatically.

    symbol : index name ("NIFTY") or storage key ("NIFTY_FUT")
    df1m   : DataFrame with columns timestamp, open, high, low, close, volume, oi

    Returns number of 1m rows saved (0 if df is empty or symbol unknown).
    """
    sym_keys = {v["sym"] for v in FUTURES_CONFIG.values()}
    if symbol.upper() in FUTURES_CONFIG:
        sym_store = FUTURES_CONFIG[symbol.upper()]["sym"]
    elif symbol.upper() in sym_keys:
        sym_store = symbol.upper()
    else:
        logger.warning(f"[FUT] store_dataframe: unknown symbol '{symbol}'")
        return 0

    if df1m.empty:
        return 0

    # Ensure oi column exists (Kite includes it for futures; default 0 otherwise)
    if "oi" not in df1m.columns:
        df1m = df1m.copy()
        df1m["oi"] = 0

    _save_df(sym_store, df1m, 1)
    for iv in RESAMPLE_INTERVALS:
        dfN = _resample(df1m, iv)
        if not dfN.empty:
            _save_df(sym_store, dfN, iv)

    vol_sum  = df1m["volume"].sum()
    oi_last  = df1m["oi"].iloc[-1]
    logger.info(
        f"[FUT] {sym_store}: {len(df1m)} 1m bars stored "
        f"| total_vol={vol_sum:,} | last_oi={oi_last:,}"
    )
    return len(df1m)


# ── Backfill all indices (Dhan — kept for reference) ──────────────────────────

def backfill(client=None, lookback_days: int = 10,
             symbols: list[str] | None = None) -> dict:
    """
    Backfill futures candles for all (or specified) indices via Kite API.
    Kite supports 60+ days of 1-min history for futures.

    client    : unused — kept for backward-compatible call sites
    Returns dict of {symbol: rows_saved}.
    """
    ensure_tables()
    targets   = symbols or list(FUTURES_CONFIG.keys())
    to_date   = date.today().strftime("%Y-%m-%d")
    from_date = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    results = {}
    for sym in targets:
        n = fetch_and_store(sym, from_date, to_date, interval=1)
        results[sym] = n
        time.sleep(API_PAUSE)

    return results


# ── Seed engine buffers ────────────────────────────────────────────────────────

def seed_futures_to_engine(data_dict: dict,
                            symbols: list[str] | None = None,
                            lookback_days: int = 5) -> dict:
    """
    Load recent futures candles from DB into OmniEngine's data_dict.

    Adds keys: "NIFTY_FUT_1m", "NIFTY_FUT_5m", "NIFTY_FUT_15m", etc.
    These carry real volume + OI (unlike spot index buffers).
    """
    targets = symbols or list(FUTURES_CONFIG.keys())
    seeded  = {}
    d_from  = (date.today() - timedelta(days=lookback_days + 2)).strftime("%Y-%m-%d")

    for sym in targets:
        sym_key = FUTURES_CONFIG[sym]["sym"]
        for iv in [1] + RESAMPLE_INTERVALS:
            tbl  = f"candles_futures_{iv}min"
            tail = {1: 400, 5: 150, 15: 100}.get(iv, 400)
            try:
                with _conn() as con:
                    df = pd.read_sql_query(
                        f"SELECT dt as timestamp, open, high, low, close, volume, oi "
                        f"FROM {tbl} WHERE tradingsymbol=? AND dt >= ? "
                        f"ORDER BY dt DESC LIMIT {tail}",
                        con, params=(sym_key, d_from),
                    )
                if df.empty:
                    continue
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").reset_index(drop=True)
                key              = f"{sym_key}_{iv}m"
                data_dict[key]   = df
                seeded[key]      = len(df)
            except Exception as e:
                logger.warning(f"[FUT] seed failed {sym_key} {iv}m: {e}")

    if seeded:
        logger.info(f"[FUT] Engine seeded with futures: {seeded}")
    return seeded


# ── Live read helper (used by CEP order-flow workers) ─────────────────────────

def read_latest(symbol: str, n: int = 10, interval: int = 1) -> pd.DataFrame:
    """Return the last *n* candles for *symbol* from candles_futures_{interval}min.

    Columns: dt (datetime), open, high, low, close, volume, oi.
    Sorted oldest-first.  Returns empty DataFrame on any error or unknown symbol.
    """
    cfg = FUTURES_CONFIG.get(symbol.upper())
    if cfg is None:
        return pd.DataFrame()
    sym = cfg["sym"]                          # e.g. "NIFTY_FUT"
    tbl = f"candles_futures_{interval}min"
    try:
        with _conn() as con:
            df = pd.read_sql_query(
                f"SELECT dt, open, high, low, close, volume, oi "
                f"FROM {tbl} WHERE tradingsymbol = ? ORDER BY dt DESC LIMIT ?",
                con, params=(sym, n),
            )
        if df.empty:
            return df
        df["dt"] = pd.to_datetime(df["dt"])
        return df.sort_values("dt").reset_index(drop=True)
    except Exception as e:
        logger.debug("[FUT] read_latest(%s, %dm): %s", symbol, interval, e)
        return pd.DataFrame()


# ── DB stats ───────────────────────────────────────────────────────────────────

def futures_db_stats() -> dict:
    stats = {}
    try:
        with _conn() as con:
            for iv in [1] + RESAMPLE_INTERVALS:
                tbl = f"candles_futures_{iv}min"
                try:
                    cur = con.execute(
                        f"SELECT tradingsymbol, COUNT(*) as cnt, MIN(dt), MAX(dt) "
                        f"FROM {tbl} GROUP BY tradingsymbol ORDER BY tradingsymbol"
                    )
                    for row in cur.fetchall():
                        key = f"{row[0]}_{iv}m"
                        stats[key] = {"rows": row[1], "from": row[2], "to": row[3]}
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[FUT] stats error: {e}")
    return stats
