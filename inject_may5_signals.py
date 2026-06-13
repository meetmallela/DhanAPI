"""
inject_may5_signals.py
----------------------
Retroactive WhatIf backtest for 2026-05-05.

All 97 approved signals hit DH-905 that day → zero orders in DB → the
scheduled WhatIf had nothing to process.

This script:
  1. Parses [SIGNAL] lines from the engine log (exact expiry dates included).
  2. Fetches real 1-min candles from Kite API (primary — real entry/exit times).
  3. Falls back to NSE/BSE bhavcopy daily OHLC if Kite has no data.
  4. Simulates the same SL logic as the live system.
  5. Writes results to the whatif_trades table (signal_id 900000+).
  6. Prints a summary.

Safe to re-run: INSERT OR REPLACE on (run_date, signal_id).
"""

import io, json, re, sqlite3, sys, zipfile
from datetime import date, datetime
from pathlib import Path

import pytz
import pandas as pd
import requests

sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
sys.path.insert(0, str(Path(__file__).parent))
from master_resource import MasterResource
from kite_candle_store import resolve_option_token, get_candles as _kite_get_candles, ensure_tables as _kite_ensure

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
RUN_DATE = "2026-05-05"
LOG_FILE = (
    r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration"
    r"\logs\dhan_omni_engine_05May2026_08_39_23.log"
)
DB_PATH   = MasterResource.get_trading_db_path()
IST       = pytz.timezone("Asia/Kolkata")
REPORT_DIR = Path(MasterResource.MASTER_ROOT) / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

SL_CFG = {
    "initial_sl_percent":          5.0,
    "trailing_activation_percent": 3.0,
    "trailing_step_percent":       1.0,
    "hard_cutoff_time":            "15:25",
    "time_sl_enabled":             True,
    "time_sl_minutes":             15,
    "time_sl_min_move_pct":        1.0,
}
try:
    import json as _json
    with open(Path(__file__).parent / "sl_config.json") as _f:
        _loaded = _json.load(_f)
        SL_CFG.update({k: v for k, v in _loaded.items() if not k.startswith("_")})
except Exception:
    pass

_BSE_SYMS = {"SENSEX", "BANKEX", "SENSEX50"}
_INDEX_LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 35, "FINNIFTY": 65,
    "SENSEX": 20, "MIDCPNIFTY": 120, "BANKEX": 15, "SENSEX50": 50,
}

# ── Log parsing ───────────────────────────────────────────────────────────────
# 2026-05-05 09:44:40,012 - dhan_omni_engine - INFO - [SIGNAL] ORB_VWAP → BUY MIDCPNIFTY-May2026-13950-CE (spot=13946, strike=13950, expiry=2026-05-26, lot=120)
_SIGNAL_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - .+? - INFO - "
    r"\[SIGNAL\] (\S+) \S+ (?:BUY|SELL) (\S+) "
    r"\(spot=[\d.]+, strike=([\d.]+), expiry=(\d{4}-\d{2}-\d{2}), lot=(\d+)\)"
)

def parse_log_signals(log_path: str) -> list[dict]:
    signals = []
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _SIGNAL_RE.match(line.strip())
            if not m:
                continue
            ts_str, strategy, tradingsymbol, strike, expiry, lot = m.groups()
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            symbol   = tradingsymbol.split("-")[0].upper()
            opt_type = tradingsymbol.split("-")[-1].upper()
            signals.append({
                "strategy":      strategy,
                "tradingsymbol": tradingsymbol,
                "symbol":        symbol,
                "strike":        float(strike),
                "opt_type":      opt_type,
                "expiry":        expiry,
                "lot":           int(lot),
                "ts":            ts,
            })
    return signals

# ── Bhavcopy fetch (cached) ───────────────────────────────────────────────────
_nse_cache: dict[str, pd.DataFrame | None] = {}
_bse_cache: dict[str, pd.DataFrame | None] = {}

def _nse_bhavcopy(run_date: str) -> pd.DataFrame | None:
    if run_date in _nse_cache:
        return _nse_cache[run_date]
    dt_str = run_date.replace("-", "")
    url    = (f"https://nsearchives.nseindia.com/content/fo/"
              f"BhavCopy_NSE_FO_0_0_0_{dt_str}_F_0000.csv.zip")
    try:
        r  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        z  = zipfile.ZipFile(io.BytesIO(r.content))
        df = pd.read_csv(z.open(z.namelist()[0]))
        print(f"  [DATA] NSE bhavcopy {run_date}: {len(df):,} rows")
        _nse_cache[run_date] = df
        return df
    except Exception as e:
        print(f"  [DATA] NSE bhavcopy failed: {e}")
        _nse_cache[run_date] = None
        return None

def _bse_bhavcopy(run_date: str) -> pd.DataFrame | None:
    if run_date in _bse_cache:
        return _bse_cache[run_date]
    dt_str = run_date.replace("-", "")
    url    = (f"https://www.bseindia.com/download/BhavCopy/Derivative/"
              f"BhavCopy_BSE_FO_0_0_0_{dt_str}_F_0000.csv")
    try:
        r  = requests.get(url,
                          headers={"User-Agent": "Mozilla/5.0",
                                   "Referer": "https://www.bseindia.com"},
                          timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        print(f"  [DATA] BSE bhavcopy {run_date}: {len(df):,} rows")
        _bse_cache[run_date] = df
        return df
    except Exception as e:
        print(f"  [DATA] BSE bhavcopy failed: {e}")
        _bse_cache[run_date] = None
        return None

def get_ohlc(symbol: str, strike: float, opt_type: str,
             expiry: str, run_date: str) -> dict | None:
    if symbol in _BSE_SYMS:
        df = _bse_bhavcopy(run_date)
        if df is None:
            return None
        mask = (
            (df["TckrSymb"]  == symbol)
            & (df["StrkPric"] == float(strike))
            & (df["OptnTp"]   == opt_type)
            & (df["XpryDt"]   == expiry)
        )
        rows = df[mask]
        if rows.empty:
            return None
        row = rows.iloc[0]
        close_px = float(row.get("LastPric", 0) or 0)
        if close_px <= 0:
            close_px = float(row.get("ClsPric", 0) or 0)
        return {"open": float(row["OpnPric"]), "high": float(row["HghPric"]),
                "low":  float(row["LwPric"]),  "close": close_px}
    else:
        df = _nse_bhavcopy(run_date)
        if df is None:
            return None
        mask = (
            (df["TckrSymb"]  == symbol)
            & (df["StrkPric"] == float(strike))
            & (df["OptnTp"]   == opt_type)
            & (df["XpryDt"]   == expiry)
        )
        rows = df[mask]
        if rows.empty:
            return None
        row = rows.iloc[0]
        return {"open": float(row["OpnPric"]), "high": float(row["HghPric"]),
                "low":  float(row["LwPric"]),  "close": float(row["ClsPric"])}

def make_candles(ohlc: dict, run_date: str, entry_ts: datetime) -> pd.DataFrame:
    """3 synthetic candles from daily OHLC: open, mid, close."""
    market_open = IST.localize(datetime.strptime(f"{run_date} 09:15:00", "%Y-%m-%d %H:%M:%S"))
    cutoff      = IST.localize(datetime.strptime(f"{run_date} 15:25:00", "%Y-%m-%d %H:%M:%S"))
    mid_ts      = IST.localize(entry_ts.replace(tzinfo=None))
    if mid_ts < market_open:
        mid_ts = market_open + pd.Timedelta(hours=3)
    return pd.DataFrame([
        {"timestamp": market_open, "open": ohlc["open"],  "high": ohlc["open"],
         "low": ohlc["open"],  "close": ohlc["open"],  "volume": 0},
        {"timestamp": mid_ts,      "open": ohlc["open"],  "high": ohlc["high"],
         "low": ohlc["low"],   "close": ohlc["close"], "volume": 0},
        {"timestamp": cutoff,      "open": ohlc["close"], "high": ohlc["close"],
         "low": ohlc["close"], "close": ohlc["close"], "volume": 0},
    ])

# ── Kite 1-min fetch ─────────────────────────────────────────────────────────
_BSE_EXCHANGE = {"SENSEX": "BFO", "BANKEX": "BFO", "SENSEX50": "BFO"}

def _fetch_kite_1min(symbol: str, strike: float, opt_type: str,
                     expiry: str) -> pd.DataFrame | None:
    """Resolve Kite token and fetch 1-min candles for the option. Returns None if unavailable."""
    try:
        token = resolve_option_token(symbol, strike, opt_type, expiry)
        if not token:
            return None
        exchange  = _BSE_EXCHANGE.get(symbol, "NFO")
        tradingsym = f"{symbol}_{int(strike)}_{opt_type}_{expiry}"
        df, source = _kite_get_candles(token, tradingsym, exchange, RUN_DATE, interval="minute")
        if df is not None and not df.empty:
            print(f"    [KITE] {tradingsym}: {len(df)} 1-min candles ({source})")
            return df
    except Exception as e:
        print(f"    [KITE] fetch error: {e}")
    return None


def get_candles_for_signal(symbol: str, strike: float, opt_type: str,
                           expiry: str, entry_ts: datetime
                           ) -> tuple[pd.DataFrame | None, str, float | None]:
    """
    Try Kite 1-min first; fall back to bhavcopy 3-candle synthetic.
    Returns (candles_df, data_quality, entry_price).
    entry_price is the first candle close at/after entry_ts (Kite) or bhavcopy open (fallback).
    """
    # 1. Kite 1-min
    df_kite = _fetch_kite_1min(symbol, strike, opt_type, expiry)
    if df_kite is not None and not df_kite.empty:
        # entry price = close of first 1-min candle at or after signal time
        entry_ist = IST.localize(entry_ts) if entry_ts.tzinfo is None else entry_ts
        after = df_kite[df_kite["timestamp"] >= entry_ist]
        if after.empty:
            after = df_kite   # if signal fired after last candle, use last candle
        entry_px = float(after.iloc[0]["close"])
        return df_kite, "KITE_1MIN", entry_px

    # 2. Bhavcopy fallback
    ohlc = get_ohlc(symbol, strike, opt_type, expiry, RUN_DATE)
    if ohlc and ohlc.get("high", 0) > 0:
        candles  = make_candles(ohlc, RUN_DATE, entry_ts)
        return candles, "DAILY_OHLC", ohlc["open"]

    return None, "NONE", None


# ── SL simulation (copy of eod_whatif_backtest logic) ────────────────────────
def _update_trailing_sl(entry, peak, sl, stage, cfg):
    act  = cfg["trailing_activation_percent"] / 100
    step = cfg["trailing_step_percent"] / 100
    gain = (peak - entry) / entry if entry else 0
    if gain >= 3 * act and stage < 3:
        new_sl = peak * (1 - step)
        if new_sl > sl:
            return new_sl, 3
    elif gain >= 2 * act and stage < 2:
        new_sl = entry + (peak - entry) * 0.5
        if new_sl > sl:
            return new_sl, 2
    elif gain >= act and stage < 1:
        if entry > sl:
            return entry, 1
    return sl, stage

def simulate_sl(entry_price: float, entry_time: datetime, candles: pd.DataFrame) -> dict:
    cfg   = SL_CFG
    sl    = entry_price * (1 - cfg["initial_sl_percent"] / 100)
    peak  = entry_price
    stage = 0
    max_p = entry_price
    min_p = entry_price
    cutoff_h, cutoff_m = map(int, cfg["hard_cutoff_time"].split(":"))

    if entry_time.tzinfo is None:
        entry_time = IST.localize(entry_time)
    market_open = entry_time.replace(hour=9, minute=15, second=0, microsecond=0)
    start_time  = max(entry_time, market_open)

    df = candles[candles["timestamp"] >= start_time].copy()
    if df.empty:
        return _exit(entry_time, entry_price, "NO_DATA", entry_price, entry_price, entry_price)

    tsl_enabled = cfg.get("time_sl_enabled", True)
    tsl_min     = cfg.get("time_sl_minutes", 15)
    tsl_move    = cfg.get("time_sl_min_move_pct", 1.0) / 100

    for _, c in df.iterrows():
        ts: datetime = c["timestamp"].to_pydatetime()
        max_p = max(max_p, c["high"])
        min_p = min(min_p, c["low"])
        ltp   = c["close"]

        if ts.hour > cutoff_h or (ts.hour == cutoff_h and ts.minute >= cutoff_m):
            return _exit(ts, ltp, "CUTOFF", entry_price, max_p, min_p)

        peak  = max(peak, c["high"])
        sl, stage = _update_trailing_sl(entry_price, peak, sl, stage, cfg)

        if c["low"] <= sl:
            reason = "TRAILING_SL" if stage > 0 else "INITIAL_SL"
            return _exit(ts, sl, reason, entry_price, max_p, min_p)

        if tsl_enabled:
            elapsed = (ts - start_time).total_seconds() / 60
            if elapsed >= tsl_min:
                move = (ltp - entry_price) / entry_price if entry_price else 0
                if move < tsl_move:
                    return _exit(ts, ltp, "TIME_SL", entry_price, max_p, min_p)

    last = df.iloc[-1]
    return _exit(last["timestamp"].to_pydatetime(), last["close"],
                 "EOD", entry_price, max_p, min_p)

def _exit(ts, exit_px, reason, entry, max_p, min_p):
    pnl = exit_px - entry
    pct = pnl / entry * 100 if entry else 0
    return {
        "exit_time":    ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "exit_price":   round(float(exit_px), 4),
        "exit_reason":  reason,
        "pnl_per_unit": round(float(pnl), 4),
        "pnl_pct":      round(float(pct), 4),
        "max_price":    round(float(max_p), 4),
        "min_price":    round(float(min_p), 4),
    }

# ── DB helpers ────────────────────────────────────────────────────────────────
def _ensure_whatif_table():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS whatif_trades (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date       TEXT,
            signal_id      INTEGER,
            symbol         TEXT,
            tradingsymbol  TEXT,
            channel_name   TEXT,
            action         TEXT,
            entry_time     TEXT,
            entry_price    REAL,
            sl_initial     REAL,
            exit_time      TEXT,
            exit_price     REAL,
            exit_reason    TEXT,
            pnl_per_unit   REAL,
            pnl_pct        REAL,
            max_price      REAL,
            min_price      REAL,
            lot_size       INTEGER,
            pnl_total      REAL,
            result         TEXT,
            data_available INTEGER,
            data_quality   TEXT,
            created_at     TEXT,
            UNIQUE(run_date, signal_id)
        )
    """)
    existing = {row[1] for row in con.execute("PRAGMA table_info(whatif_trades)")}
    for col, defn in [("data_quality", "TEXT"), ("expiry_date", "TEXT")]:
        if col not in existing:
            con.execute(f"ALTER TABLE whatif_trades ADD COLUMN {col} {defn}")
    con.commit()
    con.close()

def _save(rows: list[dict]):
    con = sqlite3.connect(DB_PATH)
    for r in rows:
        con.execute("""
            INSERT OR REPLACE INTO whatif_trades
            (run_date, signal_id, symbol, tradingsymbol, channel_name, action,
             entry_time, entry_price, sl_initial, exit_time, exit_price,
             exit_reason, pnl_per_unit, pnl_pct, max_price, min_price,
             lot_size, pnl_total, result, data_available, data_quality,
             expiry_date, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, tuple(r.get(k) for k in [
            "run_date","signal_id","symbol","tradingsymbol","channel_name","action",
            "entry_time","entry_price","sl_initial","exit_time","exit_price",
            "exit_reason","pnl_per_unit","pnl_pct","max_price","min_price",
            "lot_size","pnl_total","result","data_available","data_quality",
            "expiry_date","created_at",
        ]))
    con.commit()
    con.close()
    print(f"\n[DB] {len(rows)} rows saved to whatif_trades")

# ── TG signal processing ─────────────────────────────────────────────────────
# Candidate expiries to try when TG parsed_data has expiry=None.
# NIFTY has daily expiry → try 2026-05-05 first (same day).
# SENSEX weekly expires Wednesday = 2026-05-07 (verified from Kite data).
# BANKNIFTY weekly = Thursday = 2026-05-07.
_TG_EXPIRY_CANDIDATES: dict[str, list[str]] = {
    "NIFTY":      ["2026-05-05", "2026-05-07", "2026-05-12", "2026-05-26"],
    "BANKNIFTY":  ["2026-05-07", "2026-05-28"],
    "FINNIFTY":   ["2026-05-05", "2026-05-19", "2026-05-26"],
    "MIDCPNIFTY": ["2026-05-26"],
    "SENSEX":     ["2026-05-07", "2026-05-30"],
    "BANKEX":     ["2026-05-07", "2026-05-30"],
    "SENSEX50":   ["2026-05-07", "2026-05-30"],
}
_TRADEABLE_SYMBOLS = set(_TG_EXPIRY_CANDIDATES.keys())
# Symbols that are definitely MCX commodities or otherwise un-tradeable
_SKIP_SYMBOLS = {"CRUDEOIL", "GOLD", "SILVER", "COPPER", "NATURALGAS",
                 "CRUDE", "MAY", "PETRONET", "?", ""}


def _try_all_expiries(symbol: str, strike: float, opt_type: str,
                      ts: datetime) -> tuple:
    """Try each candidate expiry in order; return first that yields real candle data."""
    candidates = _TG_EXPIRY_CANDIDATES.get(symbol, [])
    for exp in candidates:
        candles, dq, entry_px = get_candles_for_signal(symbol, strike, opt_type, exp, ts)
        if candles is not None and entry_px is not None:
            return candles, dq, entry_px, exp
    return None, "NONE", None, candidates[0] if candidates else None


def _get_tg_signals_for_date(date_str: str) -> list[dict]:
    """Read raw TG signals from DB; filter to tradeable index options within market hours."""
    market_close = datetime.strptime(f"{date_str} 15:30:00", "%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, timestamp, channel_name, parsed_data "
        "FROM signals WHERE DATE(timestamp) = ? ORDER BY id",
        (date_str,)
    ).fetchall()
    con.close()

    signals = []
    for sig_id, ts_raw, channel, pd_raw in rows:
        try:
            d = json.loads(pd_raw) if pd_raw else {}
        except Exception:
            continue
        symbol    = str(d.get("symbol") or "").strip().upper()
        strike    = d.get("strike")
        opt_type  = str(d.get("option_type") or "").strip().upper()
        ts        = datetime.fromisoformat(ts_raw)

        # Skip filters
        if symbol in _SKIP_SYMBOLS or symbol not in _TRADEABLE_SYMBOLS:
            continue
        if not strike or not opt_type:
            continue
        if ts >= market_close:
            continue

        # Lot size from index table (TG parsed_data rarely has lot_size)
        lot = int(d.get("lot_size") or 0) or _INDEX_LOT_SIZES.get(symbol, 1)

        signals.append({
            "sig_id":   sig_id,
            "channel":  channel,
            "symbol":   symbol,
            "strike":   float(strike),
            "opt_type": opt_type,
            "lot":      lot,
            "ts":       ts,
        })
    return signals


def process_tg_signals(results: list[dict], now_str: str) -> tuple[int, int, int]:
    """
    Pull all TG signals for RUN_DATE from the signals table, resolve Kite 1-min
    candles, simulate SL, and append rows to *results*.
    Returns (kite_count, bhavcopy_count, no_data_count).
    """
    raw = _get_tg_signals_for_date(RUN_DATE)
    if not raw:
        print("[TG] No tradeable TG signals found for", RUN_DATE)
        return 0, 0, 0

    print(f"\n[TG] Processing {len(raw)} tradeable TG signals from DB ...\n")
    kite_n = bhavc_n = nodata_n = 0

    for s in raw:
        sym    = s["symbol"]
        strike = s["strike"]
        opt    = s["opt_type"]
        lot    = s["lot"]
        ts     = s["ts"]
        sig_id = s["sig_id"]

        tradingsym = f"{sym}-TG-{int(strike)}-{opt}"

        candidates = _TG_EXPIRY_CANDIDATES.get(sym, [])
        if not candidates:
            print(f"  TG#{sig_id:4d} {tradingsym:<30} SKIP: no expiry candidates")
            continue

        candles, data_quality, entry_px, expiry = _try_all_expiries(sym, strike, opt, ts)
        if candles is None or entry_px is None:
            print(f"  TG#{sig_id:4d} {tradingsym:<30} NO_DATA (tried: {', '.join(candidates)})")
            nodata_n += 1
            results.append({
                "run_date": RUN_DATE, "signal_id": sig_id, "symbol": sym,
                "tradingsymbol": tradingsym, "channel_name": s["channel"],
                "action": "BUY", "entry_time": ts.isoformat(),
                "entry_price": 0.0, "sl_initial": 0.0,
                "exit_time": ts.isoformat(), "exit_price": 0.0,
                "exit_reason": "NO_DATA", "pnl_per_unit": 0.0, "pnl_pct": 0.0,
                "max_price": 0.0, "min_price": 0.0,
                "lot_size": lot, "pnl_total": 0.0, "result": "UNKNOWN",
                "data_available": 0, "data_quality": "NONE",
                "expiry_date": expiry, "created_at": now_str,
                "capital": 0.0, "roc_pct": 0.0,
            })
            continue

        if data_quality == "KITE_1MIN":
            kite_n += 1
        else:
            bhavc_n += 1

        sim       = simulate_sl(entry_px, ts, candles)
        pnl_total = round(sim["pnl_per_unit"] * lot, 2)
        capital   = round(entry_px * lot, 2)
        roc_pct   = round(pnl_total / capital * 100, 2) if capital else 0.0
        sl_init   = entry_px * (1 - SL_CFG["initial_sl_percent"] / 100)
        result_lbl = (
            "PROFIT"    if sim["pnl_per_unit"] >  0.01
            else "LOSS" if sim["pnl_per_unit"] < -0.01
            else "BREAKEVEN"
        )
        icon = "+" if result_lbl == "PROFIT" else ("-" if result_lbl == "LOSS" else "=")
        src  = "K" if data_quality == "KITE_1MIN" else "B"
        exit_t = sim["exit_time"][11:16] if sim["exit_time"] else "—"
        print(
            f"  TG#{sig_id:4d}[{src}][{icon}] {tradingsym:<30} "
            f"@ {ts.strftime('%H:%M')}  entry={entry_px:7.2f}  "
            f"exit={sim['exit_price']:7.2f}@{exit_t}  "
            f"{sim['exit_reason']:<12}  PnL={sim['pnl_per_unit']:+7.2f} ({sim['pnl_pct']:+6.2f}%)"
            f"  capital=₹{capital:,.0f}  ROC={roc_pct:+.1f}%"
        )
        results.append({
            "run_date": RUN_DATE, "signal_id": sig_id, "symbol": sym,
            "tradingsymbol": tradingsym, "channel_name": s["channel"],
            "action": "BUY", "entry_time": ts.isoformat(),
            "entry_price": entry_px, "sl_initial": round(sl_init, 4),
            "exit_time": sim["exit_time"], "exit_price": sim["exit_price"],
            "exit_reason": sim["exit_reason"], "pnl_per_unit": sim["pnl_per_unit"],
            "pnl_pct": sim["pnl_pct"], "max_price": sim["max_price"],
            "min_price": sim["min_price"], "lot_size": lot,
            "pnl_total": pnl_total, "result": result_lbl,
            "data_available": 1, "data_quality": data_quality,
            "expiry_date": expiry, "created_at": now_str,
            "capital": capital, "roc_pct": roc_pct,
        })

    return kite_n, bhavc_n, nodata_n


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    _ensure_whatif_table()

    print(f"Parsing [SIGNAL] lines from engine log ...")
    signals = parse_log_signals(LOG_FILE)
    print(f"Found {len(signals)} signals for {RUN_DATE}")
    if not signals:
        print("No signals found — check LOG_FILE path")
        sys.exit(1)

    print(f"\nFetching Kite 1-min candles (fallback: bhavcopy) and simulating P&L ...\n")
    _kite_ensure()
    results  = []
    now_str  = datetime.now().isoformat()
    no_data  = 0
    kite_count = 0

    for i, s in enumerate(signals, start=1):
        sid    = 900000 + i        # namespace: 9xxxxx = retroactive May 5
        symbol = s["symbol"]
        strike = s["strike"]
        opt    = s["opt_type"]
        expiry = s["expiry"]
        lot    = s["lot"]
        ts     = s["ts"]           # naive datetime in IST

        candles, data_quality, entry_px = get_candles_for_signal(symbol, strike, opt, expiry, ts)
        if candles is None or entry_px is None:
            print(f"  #{i:3d} {s['tradingsymbol']:<30} NO_DATA")
            no_data += 1
            results.append({
                "run_date": RUN_DATE, "signal_id": sid, "symbol": symbol,
                "tradingsymbol": s["tradingsymbol"], "channel_name": s["strategy"],
                "action": "BUY", "entry_time": ts.isoformat(),
                "entry_price": 0.0, "sl_initial": 0.0,
                "exit_time": ts.isoformat(), "exit_price": 0.0,
                "exit_reason": "NO_DATA", "pnl_per_unit": 0.0, "pnl_pct": 0.0,
                "max_price": 0.0, "min_price": 0.0,
                "lot_size": lot, "pnl_total": 0.0, "result": "UNKNOWN",
                "data_available": 0, "data_quality": "NONE",
                "expiry_date": expiry, "created_at": now_str,
                "capital": 0.0, "roc_pct": 0.0,
            })
            continue

        if data_quality == "KITE_1MIN":
            kite_count += 1
        sim        = simulate_sl(entry_px, ts, candles)

        pnl_total  = round(sim["pnl_per_unit"] * lot, 2)
        capital    = round(entry_px * lot, 2)
        roc_pct    = round(pnl_total / capital * 100, 2) if capital else 0.0
        result_lbl = (
            "PROFIT"    if sim["pnl_per_unit"] >  0.01
            else "LOSS" if sim["pnl_per_unit"] < -0.01
            else "BREAKEVEN"
        )
        sl_initial = entry_px * (1 - SL_CFG["initial_sl_percent"] / 100)
        icon   = "+" if result_lbl == "PROFIT" else ("-" if result_lbl == "LOSS" else "=")
        src    = "K" if data_quality == "KITE_1MIN" else "B"
        exit_t = sim["exit_time"][11:16] if sim["exit_time"] else "—"
        print(
            f"  #{i:3d}[{src}][{icon}] {s['tradingsymbol']:<30} "
            f"@ {ts.strftime('%H:%M')}  entry={entry_px:7.2f}  "
            f"exit={sim['exit_price']:7.2f}@{exit_t}  "
            f"{sim['exit_reason']:<12}  PnL={sim['pnl_per_unit']:+7.2f} ({sim['pnl_pct']:+6.2f}%)"
            f"  capital=₹{capital:,.0f}  ROC={roc_pct:+.1f}%"
        )
        results.append({
            "run_date": RUN_DATE, "signal_id": sid, "symbol": symbol,
            "tradingsymbol": s["tradingsymbol"], "channel_name": s["strategy"],
            "action": "BUY", "entry_time": ts.isoformat(),
            "entry_price": entry_px, "sl_initial": round(sl_initial, 4),
            "exit_time": sim["exit_time"], "exit_price": sim["exit_price"],
            "exit_reason": sim["exit_reason"], "pnl_per_unit": sim["pnl_per_unit"],
            "pnl_pct": sim["pnl_pct"], "max_price": sim["max_price"],
            "min_price": sim["min_price"], "lot_size": lot,
            "pnl_total": pnl_total, "result": result_lbl,
            "data_available": 1, "data_quality": data_quality,
            "expiry_date": expiry, "created_at": now_str,
            "capital": capital, "roc_pct": roc_pct,
        })

    # ── TG signals ────────────────────────────────────────────────────────────
    tg_k, tg_b, tg_nd = process_tg_signals(results, now_str)

    # Save to DB (capital/roc_pct are CSV-only — dashboard computes them live)
    _save(results)

    # CSV with extra columns
    csv_path = REPORT_DIR / f"whatif_{RUN_DATE}_retroactive.csv"
    df_out = pd.DataFrame(results)
    front_cols = ["run_date","signal_id","channel_name","tradingsymbol","action",
                  "entry_time","entry_price","lot_size","capital",
                  "exit_time","exit_price","exit_reason",
                  "pnl_per_unit","pnl_pct","pnl_total","roc_pct","result","data_quality"]
    rest = [c for c in df_out.columns if c not in front_cols]
    df_out = df_out[front_cols + rest]
    df_out.to_csv(csv_path, index=False)
    print(f"[CSV] {csv_path}")

    # Summary
    log_results = [r for r in results if r["signal_id"] >= 900000]
    tg_results  = [r for r in results if r["signal_id"] < 900000]

    def _stats(rows):
        good = [r for r in rows if r["data_available"] == 1]
        return (
            len(good),
            sum(1 for r in good if r["result"] == "PROFIT"),
            sum(1 for r in good if r["result"] == "LOSS"),
            sum(1 for r in good if r["result"] == "BREAKEVEN"),
            sum(r["pnl_total"] for r in good),
        )

    l_good, l_p, l_l, l_e, l_pnl = _stats(log_results)
    t_good, t_p, t_l, t_e, t_pnl = _stats(tg_results)
    all_pnl = l_pnl + t_pnl

    print()
    print("=" * 65)
    print(f"  RETROACTIVE WHAT-IF SUMMARY — {RUN_DATE}")
    print("=" * 65)
    print(f"  ── Engine log signals (900001+) ──")
    print(f"  Parsed              : {len(signals)}")
    print(f"  Kite 1-min [K]      : {kite_count}")
    print(f"  Bhavcopy [B]        : {l_good - kite_count}")
    print(f"  No data             : {no_data}")
    print(f"  Profit/Loss/Even    : {l_p}/{l_l}/{l_e}")
    print(f"  Net P&L             : {l_pnl:+.2f}")
    print(f"  ── Telegram signals (DB id) ──────")
    print(f"  Processed           : {len(tg_results)}")
    print(f"  Kite 1-min [K]      : {tg_k}")
    print(f"  Bhavcopy [B]        : {tg_b}")
    print(f"  No data             : {tg_nd}")
    print(f"  Profit/Loss/Even    : {t_p}/{t_l}/{t_e}")
    print(f"  Net P&L             : {t_pnl:+.2f}")
    print(f"  ── Combined ─────────────────────")
    print(f"  Total saved to DB   : {len(results)}")
    print(f"  Combined Net P&L    : {all_pnl:+.2f}")
    print("=" * 65)

if __name__ == "__main__":
    main()
