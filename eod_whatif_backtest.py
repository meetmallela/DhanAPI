"""
eod_whatif_backtest.py
----------------------
Daily EOD what-if backtester.

For every signal received today, fetches 1-minute OHLC data and replays
the EXACT same SL monitor logic to answer:
  "If we had entered at the signal price and applied our SL logic,
   what would our P&L have been by EOD?"

SL logic replicated (from dhan_sl_monitor.py):
  - Initial SL   : entry × (1 − 5%)
  - Stage 1      : +3% gain → move SL to entry (breakeven)
  - Stage 2      : +6% gain → SL = entry + 50% of (peak - entry)
  - Stage 3      : +9% gain → trail at peak × (1 − 1%)
  - Time SL      : no 1% move in 15 min → exit
  - Hard cutoff  : 15:25 IST → exit at close

Runs automatically at 16:00 IST every weekday.
Add to trading system via start_trading_system.py PROCESSES list.

Usage (manual):
    python eod_whatif_backtest.py          # run immediately
    python eod_whatif_backtest.py schedule # wait until 16:00 IST, then run daily
"""

import io
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import pytz
import pandas as pd

# ── Windows encoding ──────────────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Paths ─────────────────────────────────────────────────────────────────────
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
sys.path.insert(0, str(Path(__file__).parent))
from master_resource import MasterResource

# ── Logging ───────────────────────────────────────────────────────────────────
log_ts   = datetime.now().strftime("%d%b%Y_%H_%M_%S").upper()
log_dir  = Path(MasterResource.MASTER_ROOT) / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = str(log_dir / f"eod_whatif_{log_ts}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - WHATIF - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("EOD_WHATIF")
logger.info(f"[LOG] {log_file}")

from core.strike_lookup import StrikeLookup
from kite_candle_store import (
    get_candles       as _store_get,
    resolve_option_token,
    prefetch_signals_for_date,
    ensure_tables     as _store_ensure,
    db_stats,
    INDEX_TOKENS,
)
from sl_engine import step_sl

IST        = pytz.timezone("Asia/Kolkata")
DB_PATH    = MasterResource.get_trading_db_path()
REPORT_DIR = Path(MasterResource.MASTER_ROOT) / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def _kite_token(symbol: str, strike: float, option_type: str,
                expiry_date: str) -> tuple[int | None, str]:
    """Resolve Kite instrument_token via the candle store's instrument lookup."""
    token    = resolve_option_token(symbol, float(strike), option_type, expiry_date)
    exchange = "BFO" if symbol in _BSE_SYMS else "NFO"
    return token, exchange

# ── SL config — loaded from both config files, MasterConfiguration takes priority ──
# Defaults match the optimised live monitor settings (breakeven@1%, trail@2%, peak-anchored)
SL_CFG = {
    "atr_multiplier":        1.5,
    "atr_period":            14,
    "default_sl_pct":        5.0,
    "atr_max_pct_options":   8.0,
    "atr_trail_mult":        2.0,
    "atr_beven_mult":        0.5,
    "breakeven_trigger_pct": 1.0,   # aligned with live monitor (was 3.0)
    "trail_trigger_pct":     2.0,   # aligned with live monitor (was 5.0)
    "trail_pct_am":          3.0,   # trail % from PEAK before 13:00
    "trail_pct_pm":          2.0,   # trail % from PEAK after 13:00 (was 1.5)
    "hard_cutoff_time":    "15:25",
    "time_sl_enabled":       True,
    "time_sl_minutes":       15,
    "time_sl_min_move_pct":  1.0,
    "min_hold_candles":      10,   # 10-min opening hold; mirrors live monitor
}
# Load local overrides first, then MasterConfiguration overrides (highest priority)

# ── Transaction cost constants (Indian market, Zerodha/Kite rates) ─────────────
# Applied to pnl_total after simulation to give realistic net P&L.
_TXN_BROKERAGE   = 20.0     # ₹20 flat per order leg (Zerodha/Kite)
_TXN_GST_RATE    = 0.18     # 18% GST on brokerage + exchange charges
_TXN_EXCH_PCT    = 0.00053  # 0.053% exchange transaction charge on notional
_TXN_STT_SELL    = 0.00125  # 0.125% STT on option sell (exit leg only, SEBI)
_TXN_STAMP_BUY   = 0.00003  # 0.003% stamp duty on buy (entry leg only)
_TXN_SPREAD_PCT  = 0.005    # 0.5% bid-ask spread simulation per leg


def _compute_txn_cost(premium: float, lot_size: int, is_sell: bool = False) -> float:
    """
    Realistic one-leg transaction cost for an index option at `premium` × `lot_size`.
    Round-trip cost = _compute_txn_cost(entry, lot) + _compute_txn_cost(exit, lot, is_sell=True).
    """
    if premium <= 0 or lot_size <= 0:
        return 0.0
    notional  = premium * lot_size
    spread    = notional * _TXN_SPREAD_PCT
    brokerage = _TXN_BROKERAGE
    exch      = notional * _TXN_EXCH_PCT
    gst       = (brokerage + exch) * _TXN_GST_RATE
    stt       = notional * _TXN_STT_SELL  if is_sell  else 0.0
    stamp     = notional * _TXN_STAMP_BUY if not is_sell else 0.0
    return round(spread + brokerage + exch + gst + stt + stamp, 2)
for _cfg_path in [
    Path(__file__).parent / "sl_config.json",
    Path(MasterResource.MASTER_ROOT) / "config" / "sl_config.json",
]:
    try:
        with open(_cfg_path) as _f:
            _loaded = json.load(_f)
            SL_CFG.update({k: v for k, v in _loaded.items() if not k.startswith("_")})
    except Exception:
        pass


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
            expiry_date    TEXT,
            created_at     TEXT,
            UNIQUE(run_date, signal_id)
        )
    """)
    existing = {row[1] for row in con.execute("PRAGMA table_info(whatif_trades)")}
    for col, defn in [
        ("data_quality", "TEXT"),
        ("expiry_date",  "TEXT"),
        ("source",       "TEXT"),   # "EXECUTED" | "FILTERED"
        ("txn_cost",     "REAL DEFAULT 0"),  # realistic transaction costs (brokerage+STT+spread)
    ]:
        if col not in existing:
            con.execute(f"ALTER TABLE whatif_trades ADD COLUMN {col} {defn}")
            logger.info(f"[DB] Migrated: added {col} column to whatif_trades")
    con.commit()
    con.close()


def _load_paper_outcomes(run_date: str) -> dict:
    """
    Return orders keyed by signal_id for today, so TG signals can look up
    their actual fill price and be tagged as EXECUTED vs FILTERED.
    """
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT signal_id, actual_entry_price, entry_price, quantity "
            "FROM orders "
            "WHERE date(created_at) = ? AND signal_id IS NOT NULL",
            (run_date,),
        ).fetchall()
        con.close()
        return {r["signal_id"]: dict(r) for r in rows}
    except Exception:
        return {}


def _get_today_signals(run_date: str) -> list[dict]:
    """Return all tradeable TG signals for run_date regardless of processed flag.
    OmniEngine strategy names are excluded — they are handled by _get_strategy_orders()
    to avoid double-counting the same trade twice.

    Each signal is tagged with _source = "EXECUTED" or "FILTERED" based on
    whether it was placed as a paper order.  Executed signals use the actual
    fill price (actual_entry_price from orders) so MetaAgent sees a clean
    simulation from the real fill — not from the signal LTP.
    """
    paper_map = _load_paper_outcomes(run_date)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, channel_name, parsed_data, timestamp, order_status
        FROM   signals
        WHERE  date(timestamp) = ?
        ORDER  BY id
    """, (run_date,)).fetchall()
    con.close()
    result = []
    for r in rows:
        # Skip signals whose channel_name matches an OmniEngine strategy — those
        # come from the strategy_signals path and are already captured via _get_strategy_orders()
        if (r["channel_name"] or "") in _OMNI_STRATEGIES:
            continue
        try:
            pd_json = json.loads(r["parsed_data"])
        except Exception:
            continue

        # Tag executed vs filtered; use actual fill price when available
        paper = paper_map.get(r["id"])
        if paper:
            actual_ep = paper.get("actual_entry_price") or paper.get("entry_price")
            if actual_ep:
                pd_json["entry_price"] = actual_ep
            source = "EXECUTED"
        else:
            source = "FILTERED"

        result.append({
            "signal_id":    r["id"],
            "channel_name": r["channel_name"] or "",
            "order_status": r["order_status"] or "",
            "timestamp":    r["timestamp"],
            "parsed":       pd_json,
            "_source":      source,
        })
    return result


def _parse_omni_tradingsymbol(ts: str) -> tuple[str, float, str] | None:
    """Parse 'NIFTY-Apr2026-24350-PE' → (symbol, strike, opt_type). Returns None on error."""
    try:
        parts = ts.split("-")
        # Expect at least 4 parts: SYMBOL, MonYYYY, STRIKE, OPTTYPE
        if len(parts) < 4:
            return None
        symbol   = parts[0].upper()
        strike   = float(parts[-2])
        opt_type = parts[-1].upper()
        if opt_type not in ("CE", "PE"):
            return None
        return symbol, strike, opt_type
    except Exception:
        return None


def _resolve_expiry_from_kite(symbol: str, strike: float, opt_type: str, run_date: str) -> str | None:
    """Find the expiry date for a contract that has candle data on run_date."""
    try:
        con = sqlite3.connect(_KITE_CANDLES_DB)
        pattern = f"{symbol}_{int(strike)}_{opt_type}_%"
        row = con.execute(
            "SELECT DISTINCT tradingsymbol FROM candles_1min "
            "WHERE tradingsymbol LIKE ? AND dt LIKE ? "
            "ORDER BY tradingsymbol LIMIT 1",
            (pattern, f"{run_date}%"),
        ).fetchone()
        con.close()
        if row:
            # tradingsymbol = 'NIFTY_24350_PE_2026-04-24' → last segment is expiry
            return row[0].split("_")[-1]
        return None
    except Exception:
        return None


def _get_strategy_orders(run_date: str) -> list[dict]:
    """Return OmniEngine strategy orders for run_date as WhatIf-compatible signal dicts.
    Uses actual_entry_price from the orders table so WhatIf simulates from the real fill
    price — making it a true counterfactual for MetaAgent training data.
    """
    placeholders = ",".join("?" * len(_OMNI_STRATEGIES))
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT id, strategy_name, tradingsymbol, action, created_at, "
        f"       actual_entry_price, entry_price "
        f"FROM orders "
        f"WHERE strategy_name IN ({placeholders}) "
        f"AND date(created_at) = ? "
        f"ORDER BY id",
        (*_OMNI_STRATEGIES, run_date),
    ).fetchall()
    con.close()

    result = []
    for r in rows:
        parsed = _parse_omni_tradingsymbol(r["tradingsymbol"] or "")
        if not parsed:
            logger.debug(f"[OMNI] Cannot parse tradingsymbol: {r['tradingsymbol']}")
            continue
        symbol, strike, opt_type = parsed
        expiry_date = _resolve_expiry_from_kite(symbol, strike, opt_type, run_date)

        # Use actual fill price when available (real paper entry); fall back to signal price.
        # None → WhatIf resolves from first kite candle close (old behaviour, less accurate).
        actual_ep = r["actual_entry_price"] or r["entry_price"] or None

        result.append({
            "signal_id":    100000 + r["id"],
            "channel_name": r["strategy_name"],
            "order_status": "",
            "timestamp":    r["created_at"],
            "_source":      "EXECUTED",   # all OmniEngine orders are paper trades
            "parsed": {
                "symbol":          symbol,
                "strike":          strike,
                "option_type":     opt_type,
                "expiry_date":     expiry_date,
                "action":          "BUY",
                "entry_price":     actual_ep,
                "instrument_type": "OPTIONS",
                "_omni_order":     True,
            },
        })
    logger.info(f"[OMNI] {len(result)} strategy order(s) found for {run_date} "
                f"(using actual fill prices)")
    return result


# ── Market data fetching ──────────────────────────────────────────────────────
# Data priority:
#   1. Dhan intraday_minute_data (requires valid production token — 1-min granularity)
#   2. NSE F&O bhavcopy (free, daily OHLC — available after ~18:00 IST)
#   3. No data

_INDEX_SYMS    = {"NIFTY","BANKNIFTY","FINNIFTY","SENSEX","MIDCPNIFTY","BANKEX","SENSEX50"}
_BSE_SYMS      = {"SENSEX","BANKEX","SENSEX50"}
_OMNI_STRATEGIES = {
    # A–K original workers
    "EMA_9_21", "OptionScalper_EMA44", "Supertrend_MACD",
    "EMA_VWAP_SR", "ORB_VWAP", "TriplePattern", "IndexMomentum",
    "BB_MeanReversion", "VWAPReclaim", "CPRBreakout", "PairLeadership",
    "VWAP_Slope",
    # L (Phase 16, disabled but name registered)
    "MultiStrikeScalp",
    # M–Q Phase 18 new workers
    "Ichimoku", "SMC_FVG_BOS", "FibRetracement", "StochRSI_Div", "MACD_Hist_Div",
    # R–V Phase 19 new workers
    "ElliotWave", "Harmonic_Pattern", "Candle_Reversal", "Donchian_Breakout", "MultiTF_EMA",
}
_KITE_CANDLES_DB = str(Path(MasterResource.MASTER_ROOT) / "data" / "kite_candles.db")

# Module-level bhavcopy cache to avoid re-downloading for each signal
_bhav_cache:     dict[str, pd.DataFrame | None] = {}
_bse_bhav_cache: dict[str, pd.DataFrame | None] = {}


def _fetch_nse_bhavcopy(run_date: str) -> pd.DataFrame | None:
    """Download NSE F&O bhavcopy for run_date. Returns raw DataFrame or None."""
    if run_date in _bhav_cache:
        return _bhav_cache[run_date]

    import io as _io, zipfile, requests
    dt_str = run_date.replace("-", "")   # YYYYMMDD
    url    = (f"https://nsearchives.nseindia.com/content/fo/"
              f"BhavCopy_NSE_FO_0_0_0_{dt_str}_F_0000.csv.zip")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        z  = zipfile.ZipFile(_io.BytesIO(r.content))
        df = pd.read_csv(z.open(z.namelist()[0]))
        logger.info(f"[DATA] NSE bhavcopy loaded: {len(df):,} rows for {run_date}")
        _bhav_cache[run_date] = df
        return df
    except Exception as e:
        logger.warning(f"[DATA] NSE bhavcopy failed for {run_date}: {e}")
        _bhav_cache[run_date] = None
        return None


def _fetch_bse_bhavcopy(run_date: str) -> pd.DataFrame | None:
    """Download BSE F&O bhavcopy for run_date. Returns raw DataFrame or None."""
    if run_date in _bse_bhav_cache:
        return _bse_bhav_cache[run_date]

    import requests
    dt_str = run_date.replace("-", "")   # YYYYMMDD
    url    = (f"https://www.bseindia.com/download/BhavCopy/Derivative/"
              f"BhavCopy_BSE_FO_0_0_0_{dt_str}_F_0000.csv")
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bseindia.com"},
            timeout=30,
        )
        r.raise_for_status()
        import io as _io
        df = pd.read_csv(_io.StringIO(r.text))
        logger.info(f"[DATA] BSE bhavcopy loaded: {len(df):,} rows for {run_date}")
        _bse_bhav_cache[run_date] = df
        return df
    except Exception as e:
        logger.warning(f"[DATA] BSE bhavcopy failed for {run_date}: {e}")
        _bse_bhav_cache[run_date] = None
        return None


def _ohlc_from_bhavcopy(
    symbol: str,
    strike: float,
    option_type: str,
    expiry_date: str,
    run_date: str,
) -> dict | None:
    """
    Look up a single contract's OHLC from NSE or BSE bhavcopy.
    BSE instruments (SENSEX, BANKEX, SENSEX50) use BSE bhavcopy.
    Returns dict {open, high, low, close} or None.

    NOTE: BSE bhavcopy `ClsPric` holds the underlying settlement price, NOT the
    option close. The option's last-traded price is in `LastPric` on BSE.
    """
    if symbol in _BSE_SYMS:
        df = _fetch_bse_bhavcopy(run_date)
        if df is None:
            return None

        # BSE expiry date format is YYYY-MM-DD (same as NSE)
        mask = (
            (df["TckrSymb"]  == symbol)
            & (df["StrkPric"] == float(strike))
            & (df["OptnTp"]   == option_type)
            & (df["XpryDt"]   == expiry_date)
        )
        rows = df[mask]
        if rows.empty:
            logger.debug(f"[DATA] Not found in BSE bhavcopy: {symbol} {strike} {option_type} {expiry_date}")
            return None

        row = rows.iloc[0]
        # BSE: ClsPric is the underlying level — use LastPric for option close
        close_px = float(row.get("LastPric", 0) or 0)
        if close_px <= 0:
            close_px = float(row.get("ClsPric", 0) or 0)
        return {
            "open":  float(row["OpnPric"]),
            "high":  float(row["HghPric"]),
            "low":   float(row["LwPric"]),
            "close": close_px,
        }

    # NSE instruments
    df = _fetch_nse_bhavcopy(run_date)
    if df is None:
        return None

    mask = (
        (df["TckrSymb"]  == symbol)
        & (df["StrkPric"] == float(strike))
        & (df["OptnTp"]   == option_type)
        & (df["XpryDt"]   == expiry_date)
    )
    rows = df[mask]
    if rows.empty:
        logger.debug(f"[DATA] Not found in bhavcopy: {symbol} {strike} {option_type} {expiry_date}")
        return None

    row = rows.iloc[0]
    return {
        "open":  float(row["OpnPric"]),
        "high":  float(row["HghPric"]),
        "low":   float(row["LwPric"]),
        "close": float(row["ClsPric"]),
    }


def fetch_candles(
    symbol: str,
    strike: float,
    option_type: str,
    expiry_date: str,
    run_date: str,
) -> tuple[pd.DataFrame | None, str]:
    """
    Fetch 1-min candle data for a contract.  Priority:
        1. Kite API   — fetches live AND persists to kite_candles.db
        2. Local DB   — served from kite_candles.db if Kite unavailable
        3. Bhavcopy   — daily OHLC fallback (3 synthetic candles)

    data_quality values:
        'KITE_API'    — live from Kite (also saved locally)
        'LOCAL_DB'    — from local SQLite cache
        'DAILY_OHLC'  — bhavcopy synthetic fallback
        'NONE'        — no data anywhere
    """
    # ── 1 & 2. Candle store (Kite → local DB) ────────────────────────────────
    token, exch = _kite_token(symbol, float(strike), option_type, expiry_date)
    if token:
        tradingsym = f"{symbol}-{expiry_date[:7]}-{int(float(strike))}-{option_type}"
        df, source = _store_get(token, tradingsym, exch, run_date, interval="minute")
        if df is not None and not df.empty:
            logger.info(f"[DATA] {symbol} {strike} {option_type} → "
                        f"{len(df)} 1-min candles ({source})")
            return df, source

    # ── 3. Bhavcopy fallback (daily OHLC → 3 synthetic candles) ──────────────
    ohlc = _ohlc_from_bhavcopy(symbol, float(strike), option_type, expiry_date, run_date)
    if ohlc and ohlc["high"] > 0:
        market_open = pd.Timestamp(f"{run_date} 09:15:00").tz_localize(IST)
        df = pd.DataFrame([
            {"timestamp": market_open,
             "open":  ohlc["open"], "high": ohlc["open"],
             "low":   ohlc["open"], "close": ohlc["open"], "volume": 0},
            {"timestamp": market_open + pd.Timedelta(hours=3),
             "open":  ohlc["open"], "high": ohlc["high"],
             "low":   ohlc["low"],  "close": ohlc["close"], "volume": 0},
            {"timestamp": pd.Timestamp(f"{run_date} 15:25:00").tz_localize(IST),
             "open":  ohlc["close"], "high": ohlc["close"],
             "low":   ohlc["close"], "close": ohlc["close"], "volume": 0},
        ])
        logger.info(f"[DATA] {symbol} {strike} {option_type} → bhavcopy OHLC "
                    f"O={ohlc['open']} H={ohlc['high']} L={ohlc['low']} C={ohlc['close']}")
        return df, "DAILY_OHLC"

    logger.warning(f"[DATA] No data for {symbol} {strike} {option_type} {expiry_date}")
    return None, "NONE"


# ── SL simulation (mirrors dhan_sl_monitor ATR trail logic) ──────────────────

def _resample_5min(candles_1m: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-min option candles to 5-min for ATR calculation."""
    df = candles_1m.copy()
    df = df.set_index("timestamp").sort_index()
    r = df.resample("5min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    return r.reset_index()


def _atr_1min(candles_1m: pd.DataFrame, period: int = 14) -> float | None:
    """ATR from 1-min candles — matches live _compute_option_atr() in dhan_sl_monitor."""
    try:
        df = candles_1m.sort_values("timestamp").reset_index(drop=True)
        if len(df) < 3:
            return None
        trs = []
        for i in range(1, len(df)):
            h, l, pc = df.loc[i, "high"], df.loc[i, "low"], df.loc[i - 1, "close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        tail = trs[-period:] if len(trs) >= period else trs
        return round(sum(tail) / len(tail), 2) if tail else None
    except Exception:
        return None


def _atr_from_candles(candles_1m: pd.DataFrame, period: int = 14) -> float | None:
    """ATR(period) on 5-min resampled option candles (used for initial SL only)."""
    try:
        df = _resample_5min(candles_1m)
        if len(df) < period:
            return None
        hl  = df["high"] - df["low"]
        hpc = abs(df["high"] - df["close"].shift(1))
        lpc = abs(df["low"]  - df["close"].shift(1))
        tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
        return float(tr.tail(period).mean())
    except Exception:
        return None


def _compute_initial_sl(entry_price: float, action: str,
                        candles: pd.DataFrame, tradingsymbol: str,
                        cfg: dict) -> float:
    """
    ATR-based initial SL — mirrors EnhancedSLMonitor.calculate_initial_sl.

    Priority (same as v3):
      1. ATR × multiplier (wider stop on volatile days)
      2. Capped at atr_max_pct_options% for CE/PE options
      3. Floor: default_sl_pct% — used whichever is wider
    """
    is_long     = action == "BUY"
    floor_pct   = cfg["default_sl_pct"] / 100
    fixed_sl    = (entry_price * (1 - floor_pct) if is_long
                   else entry_price * (1 + floor_pct))

    atr = _atr_from_candles(candles, cfg.get("atr_period", 14))
    if atr and atr > 0:
        atr_sl = (entry_price - atr * cfg["atr_multiplier"] if is_long
                  else entry_price + atr * cfg["atr_multiplier"])

        # Cap ATR SL width for CE/PE — ATR is on option premium which can be tiny
        ts_upper  = tradingsymbol.upper()
        is_option = ts_upper.endswith("CE") or ts_upper.endswith("PE")
        max_pct   = cfg.get("atr_max_pct_options", 8.0)
        if is_option and max_pct > 0:
            if is_long:
                atr_sl = max(atr_sl, entry_price * (1 - max_pct / 100))
            else:
                atr_sl = min(atr_sl, entry_price * (1 + max_pct / 100))

        # Use ATR SL only if it's wider (more protective) than the fixed floor
        if is_long and atr_sl < fixed_sl:
            return atr_sl
        if not is_long and atr_sl > fixed_sl:
            return atr_sl

    return fixed_sl


def _update_sl_stage(
    peak: float, entry_price: float, sl_price: float,
    action: str, atr: float | None, cfg: dict, hour: int,
) -> float:
    """Thin wrapper — delegates to shared sl_engine.step_sl()."""
    new_sl, _stage = step_sl(entry_price, action, peak, sl_price, atr, cfg, hour)
    return new_sl


def simulate_sl(
    entry_price: float,
    action: str,
    entry_time: datetime,
    candles: pd.DataFrame,
    cfg: dict,
    tradingsymbol: str = "",
) -> dict:
    """
    Replay SL monitor logic on 1-minute candles.  Matches dhan_sl_monitor.py exactly:

    • ATR-based trail (primary): trail_dist = atr_trail_mult × 1-min ATR from PEAK
      — trail SL only fires when it would be above entry (no sub-entry trailing)
      — breakeven fires at atr_beven_mult × ATR gain
    • Percentage fallback: trail anchored to PEAK (bug-fixed; was anchored to LTP)

    Candle-direction heuristic (eliminates spurious breakeven-then-immediate-exit):
      Bullish candle (close ≥ open): assumed path = open → LOW → HIGH → close
        → for BUY: check SL against LOW first (before peak update), then update peak
      Bearish candle (close < open): assumed path = open → HIGH → LOW → close
        → for BUY: update peak with HIGH first, then check SL against LOW
    """
    is_long    = action == "BUY"
    cutoff_str = cfg.get("hard_cutoff_time", "15:25")
    cutoff_h, cutoff_m = map(int, cutoff_str.split(":"))

    initial_sl = _compute_initial_sl(entry_price, action, candles, tradingsymbol, cfg)
    sl_price   = initial_sl
    peak       = entry_price
    max_price  = entry_price
    min_price  = entry_price

    # 1-min ATR for trail (matches live monitor)
    atr = _atr_1min(candles, cfg.get("atr_period", 14))

    # Filter candles from entry_time onwards
    market_open = entry_time.replace(hour=9, minute=15, second=0, microsecond=0)
    start_time  = max(entry_time, market_open)
    df = candles[candles["timestamp"] >= start_time].copy()

    _empty = {
        "exit_time": entry_time.isoformat(), "exit_price": entry_price,
        "exit_reason": "NO_DATA", "sl_initial": round(initial_sl, 4),
        "pnl_per_unit": 0.0, "pnl_pct": 0.0,
        "max_price": entry_price, "min_price": entry_price,
    }
    if df.empty:
        return _empty

    time_sl_enabled  = cfg.get("time_sl_enabled", True)
    time_sl_min      = cfg.get("time_sl_minutes", 15)
    time_sl_move_pct = cfg.get("time_sl_min_move_pct", 1.0) / 100
    min_hold_min     = cfg.get("min_hold_candles", 10)   # mirrors live monitor hold period

    for _, candle in df.iterrows():
        ts:  datetime = candle["timestamp"].to_pydatetime()
        ltp: float    = candle["close"]
        is_bullish    = ltp >= candle["open"]

        max_price = max(max_price, candle["high"])
        min_price = min(min_price, candle["low"])

        # Hard cutoff
        if ts.hour > cutoff_h or (ts.hour == cutoff_h and ts.minute >= cutoff_m):
            return _make_result(ts, ltp, "CUTOFF", entry_price, action,
                                max_price, min_price, initial_sl)

        # Min-hold: update peak freely but skip all SL checks for first N minutes.
        # Mirrors dhan_sl_monitor hold_until logic — opening IV crush / price discovery.
        in_hold = (ts - start_time).total_seconds() < min_hold_min * 60
        if in_hold:
            peak = max(peak, candle["high"]) if is_long else min(peak, candle["low"])
            continue

        # ── Candle-direction heuristic ────────────────────────────────────────
        # Bullish candle: low arrives before high  → for BUY, check SL before peak moves
        # Bearish candle: high arrives before low  → for BUY, update peak then check SL
        if is_long:
            if is_bullish:
                # LOW first: check SL at current (pre-candle) level
                if candle["low"] <= sl_price:
                    reason = "TRAILING_SL" if sl_price > initial_sl else "INITIAL_SL"
                    return _make_result(ts, sl_price, reason, entry_price, action,
                                        max_price, min_price, initial_sl)
                # HIGH next: update peak, recompute trail
                peak     = max(peak, candle["high"])
                sl_price = _update_sl_stage(peak, entry_price, sl_price, action,
                                            atr, cfg, ts.hour)
            else:
                # HIGH first: update peak, recompute trail
                peak     = max(peak, candle["high"])
                sl_price = _update_sl_stage(peak, entry_price, sl_price, action,
                                            atr, cfg, ts.hour)
                # LOW next: check against updated SL
                if candle["low"] <= sl_price:
                    reason = "TRAILING_SL" if sl_price > initial_sl else "INITIAL_SL"
                    return _make_result(ts, sl_price, reason, entry_price, action,
                                        max_price, min_price, initial_sl)

        else:  # SELL — mirror logic
            if not is_bullish:
                # HIGH first: check SL before peak moves
                if candle["high"] >= sl_price:
                    reason = "TRAILING_SL" if sl_price < initial_sl else "INITIAL_SL"
                    return _make_result(ts, sl_price, reason, entry_price, action,
                                        max_price, min_price, initial_sl)
                peak     = min(peak, candle["low"])
                sl_price = _update_sl_stage(peak, entry_price, sl_price, action,
                                            atr, cfg, ts.hour)
            else:
                peak     = min(peak, candle["low"])
                sl_price = _update_sl_stage(peak, entry_price, sl_price, action,
                                            atr, cfg, ts.hour)
                if candle["high"] >= sl_price:
                    reason = "TRAILING_SL" if sl_price < initial_sl else "INITIAL_SL"
                    return _make_result(ts, sl_price, reason, entry_price, action,
                                        max_price, min_price, initial_sl)

        # Time SL: no meaningful move within N minutes → exit
        if time_sl_enabled:
            elapsed_min = (ts - start_time).total_seconds() / 60
            if elapsed_min >= time_sl_min:
                move = ((ltp - entry_price) / entry_price if is_long
                        else (entry_price - ltp) / entry_price) if entry_price else 0
                if move < time_sl_move_pct:
                    return _make_result(ts, ltp, "TIME_SL", entry_price, action,
                                        max_price, min_price, initial_sl)

    last = df.iloc[-1]
    return _make_result(
        last["timestamp"].to_pydatetime(), last["close"],
        "EOD", entry_price, action, max_price, min_price, initial_sl,
    )


def _make_result(ts, exit_price, reason, entry_price, action,
                 max_price, min_price, initial_sl=None) -> dict:
    pnl_per_unit = (exit_price - entry_price) if action == "BUY" else (entry_price - exit_price)
    pnl_pct = pnl_per_unit / entry_price * 100 if entry_price else 0
    return {
        "exit_time":    ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "exit_price":   round(exit_price, 4),
        "exit_reason":  reason,
        "sl_initial":   round(initial_sl, 4) if initial_sl is not None else None,
        "pnl_per_unit": round(pnl_per_unit, 4),
        "pnl_pct":      round(pnl_pct, 4),
        "max_price":    round(max_price, 4),
        "min_price":    round(min_price, 4),
    }


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_backtest(run_date: str | None = None):
    if run_date is None:
        run_date = date.today().strftime("%Y-%m-%d")

    logger.info("=" * 65)
    logger.info(f"  EOD WHAT-IF BACKTEST  —  {run_date}")
    logger.info("=" * 65)

    _ensure_whatif_table()

    tg_signals     = _get_today_signals(run_date)
    omni_signals   = _get_strategy_orders(run_date)
    signals        = tg_signals + omni_signals

    if not signals:
        logger.warning(f"[SKIP] No processed signals or strategy orders found for {run_date}")
        return

    logger.info(f"[INFO] {len(tg_signals)} TG signal(s) + {len(omni_signals)} strategy order(s) = {len(signals)} total")

    # Ensure candle DB tables exist
    _store_ensure()

    # Pre-fetch 1-min candles for ALL signals (TG + strategy) — stores permanently in kite_candles.db.
    # Must run the same trading day (token valid until 6 AM next day) to capture daily-expiry options.
    logger.info(f"[STORE] Pre-fetching 1-min candles for all {len(signals)} signals on {run_date}...")
    try:
        all_parsed = [s["parsed"] for s in signals]
        prefetch_signals_for_date(run_date, all_parsed)
        logger.info(f"[STORE] DB stats: {db_stats()}")
    except Exception as _e:
        logger.warning(f"[STORE] Prefetch failed (will try per-signal): {_e}")

    strike_lkp = StrikeLookup()

    results = []
    now_str  = datetime.now().isoformat()

    for sig in signals:
        sid       = sig["signal_id"]
        parsed    = sig["parsed"]
        symbol    = (parsed.get("symbol") or "?").upper()
        strike    = parsed.get("strike")
        opt_type  = (parsed.get("option_type") or "").upper()
        exp_date  = parsed.get("expiry_date")
        action    = (parsed.get("action") or "BUY").upper()
        entry_px  = parsed.get("entry_price") or parsed.get("cmp")
        channel   = sig["channel_name"]
        inst_type = parsed.get("instrument_type") or "OPTIONS"

        is_omni = parsed.get("_omni_order", False)
        logger.info(f"\n[{'OMNI' if is_omni else 'SIG'} #{sid}] {symbol} {strike} {opt_type} | entry={entry_px} | {channel}")

        # Skip if no entry price (TG signals only — omni orders resolve price from candles)
        _sig_source = sig.get("_source", "UNKNOWN")
        if not entry_px and not is_omni:
            logger.warning(f"  [SKIP] No entry price")
            _save_result(sid, symbol, None, channel, action, sig["timestamp"],
                         entry_px, run_date, now_str, data_available=0,
                         exit_reason="NO_ENTRY_PRICE", source=_sig_source)
            continue

        # Skip MCX
        _MCX = {"COPPER","CRUDEOIL","CRUDEOILM","GOLD","GOLDM","SILVER","SILVERM",
                 "NATURALGAS","ZINC","LEAD","NICKEL","ALUMINIUM"}
        if symbol in _MCX or parsed.get("exchange","").upper() == "MCX":
            logger.info(f"  [SKIP] MCX — no Dhan data")
            _save_result(sid, symbol, None, channel, action, sig["timestamp"],
                         entry_px, run_date, now_str, data_available=0,
                         exit_reason="MCX_SKIP", source=_sig_source)
            continue

        # Resolve security_id via StrikeLookup (skipped for OmniEngine orders)
        security_id      = None
        exchange_segment = None
        tradingsymbol    = None
        lot_size         = 1

        _INDEX_LOT_SIZES = {
            "NIFTY": 75, "BANKNIFTY": 35, "FINNIFTY": 65,
            "SENSEX": 20, "MIDCPNIFTY": 75, "BANKEX": 15, "SENSEX50": 50,
        }

        resolved_exp = exp_date  # may be updated below if expiry is rolled

        if is_omni:
            # OmniEngine orders: expiry already resolved from kite DB; skip Dhan StrikeLookup
            resolved_exp  = exp_date  # from _get_strategy_orders → _resolve_expiry_from_kite
            lot_size      = _INDEX_LOT_SIZES.get(symbol, 1)
            # If kite had no data for this strike/opt_type on run_date, try StrikeLookup expiry
            if not resolved_exp and symbol in _INDEX_SYMS:
                try:
                    resolved_exp = strike_lkp.get_nearest_expiry(symbol)
                    logger.info(f"  [OMNI] Expiry from StrikeLookup: {resolved_exp}")
                except Exception:
                    pass
            tradingsymbol = f"{symbol}_{int(float(strike))}_{opt_type}_{resolved_exp}" if resolved_exp else f"{symbol}_{int(float(strike))}_{opt_type}"
            security_id   = "OMNI"  # placeholder — not used in WhatIf
        elif inst_type == "FUTURES":
            FUTURES_MAP = {
                "NIFTY":     ("13",  "NSE_FNO", 75),
                "BANKNIFTY": ("25",  "NSE_FNO", 35),
                "FINNIFTY":  ("27",  "NSE_FNO", 65),
                "SENSEX":    ("51",  "BSE_FNO", 20),
                "MIDCPNIFTY":("26",  "NSE_FNO", 75),
            }
            info = FUTURES_MAP.get(symbol)
            if info:
                security_id, exchange_segment, lot_size = info
                tradingsymbol = f"{symbol} FUT"
        elif strike and opt_type:
            # Resolve expiry: roll if past
            resolved_exp = exp_date
            if exp_date:
                try:
                    exp_dt = date.fromisoformat(exp_date[:10])
                    if exp_dt < date.fromisoformat(run_date):
                        if symbol in _INDEX_SYMS:
                            resolved_exp = strike_lkp.get_nearest_expiry(symbol)
                        else:
                            resolved_exp = strike_lkp.get_nearest_stock_expiry(symbol)
                        logger.info(f"  [EXPIRY] Rolled {exp_date} → {resolved_exp}")
                except Exception:
                    pass

            if symbol in _INDEX_SYMS:
                res = None
                if resolved_exp:
                    from datetime import datetime as _dt
                    ed = _dt.strptime(resolved_exp[:10], "%Y-%m-%d")
                    dhan_sym = f"{symbol}-{ed.strftime('%b%Y')}-{int(float(strike))}-{opt_type}"
                    res = strike_lkp.get_by_trading_symbol(dhan_sym)
                if not res:
                    res = strike_lkp.get_atm_option(symbol, float(strike), opt_type,
                                                     resolved_exp, itm_shift=False)
            else:
                res = strike_lkp.get_stock_option(symbol, float(strike), opt_type, resolved_exp)

            if res:
                security_id      = res["security_id"]
                exchange_segment = res["exchange_segment"]
                tradingsymbol    = res["trading_symbol"]
                lot_size         = res["lot_size"]

        if not security_id:
            logger.warning(f"  [SKIP] Could not resolve security_id")
            _save_result(sid, symbol, tradingsymbol, channel, action,
                         sig["timestamp"], entry_px, run_date, now_str,
                         data_available=0, exit_reason="NO_SECURITY_ID",
                         lot_size=lot_size, expiry_date=resolved_exp,
                         source=_sig_source)
            continue

        # Parse entry time
        try:
            entry_time = datetime.fromisoformat(sig["timestamp"])
            if entry_time.tzinfo is None:
                entry_time = IST.localize(entry_time)
            else:
                entry_time = entry_time.astimezone(IST)
        except Exception:
            entry_time = IST.localize(
                datetime.strptime(run_date, "%Y-%m-%d").replace(hour=9, minute=15)
            )

        # Fetch candles — Kite 1-min primary, bhavcopy fallback
        candles, data_quality = fetch_candles(
            symbol=symbol, strike=float(strike) if strike else 0,
            option_type=opt_type, expiry_date=resolved_exp or "",
            run_date=run_date,
        )

        if candles is None or candles.empty:
            logger.warning(f"  [DATA] No candle data available")
            _save_result(sid, symbol, tradingsymbol, channel, action,
                         sig["timestamp"], float(entry_px) if entry_px else None,
                         run_date, now_str, data_available=0, exit_reason="NO_DATA",
                         lot_size=lot_size, expiry_date=resolved_exp,
                         source=_sig_source)
            continue

        # For OmniEngine orders: entry_price is None → use first candle close at/after signal time
        if entry_px is None:
            after = candles[candles["timestamp"] >= entry_time]
            if after.empty:
                after = candles  # fallback to first candle of the day
            entry_px = float(after.iloc[0]["close"])
            logger.info(f"  [OMNI] Entry price resolved from candles: {entry_px}")

        # Simulate SL logic (ATR-based, mirrors sl_monitor_with_trailing_ATR_v3)
        sim = simulate_sl(float(entry_px), action, entry_time, candles, SL_CFG,
                          tradingsymbol=tradingsymbol or "")

        pnl_total = round(sim["pnl_per_unit"] * lot_size, 2)

        # Apply realistic transaction costs (brokerage, STT, exchange charges, bid-ask spread)
        txn_entry = _compute_txn_cost(float(entry_px), lot_size, is_sell=False)
        txn_exit  = _compute_txn_cost(sim["exit_price"], lot_size, is_sell=True)
        txn_cost  = round(txn_entry + txn_exit, 2)
        pnl_total = round(pnl_total - txn_cost, 2)

        result_lbl = (
            "PROFIT"    if pnl_total > 0.01
            else "LOSS" if pnl_total < -0.01
            else "BREAKEVEN"
        )
        sl_initial = sim["sl_initial"] or float(entry_px) * (1 - SL_CFG["default_sl_pct"] / 100)

        row = {
            "run_date":      run_date,
            "signal_id":     sid,
            "symbol":        symbol,
            "tradingsymbol": tradingsymbol or "",
            "channel_name":  channel,
            "action":        action,
            "entry_time":    sig["timestamp"],
            "entry_price":   float(entry_px),
            "sl_initial":    round(sl_initial, 4),
            "exit_time":     sim["exit_time"],
            "exit_price":    sim["exit_price"],
            "exit_reason":   sim["exit_reason"],
            "pnl_per_unit":  sim["pnl_per_unit"],
            "pnl_pct":       sim["pnl_pct"],
            "max_price":     sim["max_price"],
            "min_price":     sim["min_price"],
            "lot_size":      lot_size,
            "pnl_total":     pnl_total,
            "result":        result_lbl,
            "data_available": 1 if data_quality != "NONE" else 0,
            "data_quality":   data_quality,
            "expiry_date":   resolved_exp,
            "created_at":    now_str,
            "source":        sig.get("_source", "UNKNOWN"),
            "txn_cost":      txn_cost,
        }
        results.append(row)

        icon = "+" if result_lbl == "PROFIT" else "-" if result_lbl == "LOSS" else "="
        logger.info(
            f"  [{icon}] {tradingsymbol} | entry={entry_px} "
            f"exit={sim['exit_price']} @ {sim['exit_time'][11:16]} "
            f"| reason={sim['exit_reason']} | PnL={sim['pnl_per_unit']:+.2f} "
            f"({sim['pnl_pct']:+.2f}%) | lot_PnL={pnl_total:+.2f} | txn={txn_cost:+.2f}"
        )

    # Persist to DB and write CSV
    _save_all_results(results)
    _write_csv(results, run_date)
    _print_summary(results, run_date)


def _save_result(signal_id, symbol, tradingsymbol, channel, action, entry_time,
                 entry_price, run_date, now_str, data_available=0,
                 exit_reason="UNKNOWN", lot_size=1, expiry_date=None, source="UNKNOWN"):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            INSERT OR REPLACE INTO whatif_trades
            (run_date, signal_id, symbol, tradingsymbol, channel_name, action,
             entry_time, entry_price, exit_reason, data_available, lot_size,
             expiry_date, created_at, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (run_date, signal_id, symbol, tradingsymbol or "", channel, action,
              entry_time, entry_price, exit_reason, data_available, lot_size,
              expiry_date, now_str, source))
        con.commit()
        con.close()
    except Exception as e:
        logger.error(f"DB save failed for signal {signal_id}: {e}")


def _save_all_results(results: list[dict]):
    if not results:
        return
    con = sqlite3.connect(DB_PATH)
    for r in results:
        try:
            con.execute("""
                INSERT OR REPLACE INTO whatif_trades
                (run_date, signal_id, symbol, tradingsymbol, channel_name, action,
                 entry_time, entry_price, sl_initial, exit_time, exit_price,
                 exit_reason, pnl_per_unit, pnl_pct, max_price, min_price,
                 lot_size, pnl_total, result, data_available, data_quality,
                 expiry_date, created_at, source, txn_cost)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, tuple(r.get(k) for k in [
                "run_date","signal_id","symbol","tradingsymbol","channel_name","action",
                "entry_time","entry_price","sl_initial","exit_time","exit_price",
                "exit_reason","pnl_per_unit","pnl_pct","max_price","min_price",
                "lot_size","pnl_total","result","data_available","data_quality",
                "expiry_date","created_at","source","txn_cost",
            ]))
        except Exception as e:
            logger.error(f"DB insert failed for signal {r.get('signal_id')}: {e}")
    con.commit()
    con.close()
    logger.info(f"[DB] {len(results)} what-if rows saved to whatif_trades")


def _write_csv(results: list[dict], run_date: str):
    if not results:
        return
    csv_path = REPORT_DIR / f"whatif_{run_date}.csv"
    df = pd.DataFrame(results)
    df.to_csv(csv_path, index=False)
    logger.info(f"[CSV] Report → {csv_path}")


def _print_summary(results: list[dict], run_date: str):
    if not results:
        logger.info("No what-if results to summarise.")
        return

    executed  = [r for r in results if r.get("source") == "EXECUTED"]
    filtered  = [r for r in results if r.get("source") == "FILTERED"]

    def _stats(subset):
        wins    = sum(1 for r in subset if r["result"] == "PROFIT")
        losses  = sum(1 for r in subset if r["result"] == "LOSS")
        pnl     = sum(r["pnl_total"] for r in subset)
        txn     = sum(r.get("txn_cost", 0) for r in subset)
        return len(subset), wins, losses, pnl, txn

    tot, tw, tl, total_pnl, total_txn = _stats(results)
    en,  ew, el, exec_pnl,  exec_txn  = _stats(executed)
    fn,  fw, fl, filt_pnl,  filt_txn  = _stats(filtered)

    reasons = {}
    for r in results:
        reasons[r["exit_reason"]] = reasons.get(r["exit_reason"], 0) + 1

    logger.info("")
    logger.info("=" * 65)
    logger.info(f"  WHAT-IF SUMMARY — {run_date}")
    logger.info("=" * 65)
    logger.info(f"  Total   : {tot:3d} signals | W={tw} L={tl} | Net PnL={total_pnl:+,.2f} | Txn={total_txn:+,.2f}")
    logger.info(f"  EXECUTED: {en:3d} trades   | W={ew} L={el} | PnL={exec_pnl:+,.2f}  Txn={exec_txn:+,.2f}  (actual fill, sim SL)")
    logger.info(f"  FILTERED: {fn:3d} signals  | W={fw} L={fl} | PnL={filt_pnl:+,.2f}  Txn={filt_txn:+,.2f}  (counterfactual)")
    logger.info(f"  Exit reasons : {reasons}")
    logger.info("")
    logger.info(f"  {'Src':<4} {'Signal':>6} {'Symbol':<16} {'Action':<5} {'Entry':>8} "
                f"{'Exit':>8} {'Reason':<14} {'PnL%':>7} {'Result'}")
    logger.info(f"  {'-'*4} {'-'*6} {'-'*16} {'-'*5} {'-'*8} {'-'*8} {'-'*14} {'-'*7} {'-'*8}")
    for r in results:
        src = (r.get("source") or "?")[0]   # E or F
        logger.info(
            f"  {src:<4} #{r['signal_id']:<5} {r['symbol']:<16} {r['action']:<5} "
            f"{r['entry_price']:>8.2f} {r['exit_price']:>8.2f} "
            f"{r['exit_reason']:<14} {r['pnl_pct']:>+7.2f}% {r['result']}"
        )
    logger.info("=" * 65)


# ── Scheduler: run every weekday at 17:00 IST ────────────────────────────────
RUN_HOUR   = 17   # 5 PM IST — all market data settled, Kite candles complete
RUN_MINUTE =  0

def run_scheduler():
    logger.info(f"[SCHEDULER] EOD What-If Backtest — will run daily at {RUN_HOUR:02d}:{RUN_MINUTE:02d} IST (weekdays)")
    while True:
        now = datetime.now(IST)

        # Skip weekends
        if now.weekday() >= 5:
            time.sleep(3600)
            continue

        # Next target = today at RUN_HOUR:RUN_MINUTE
        target = now.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
        if now >= target:
            # Past today's window — schedule for next weekday
            target += timedelta(days=1)
            while target.weekday() >= 5:
                target += timedelta(days=1)

        sleep_s = (target - now).total_seconds()

        if sleep_s > 120:
            logger.info(f"[SCHEDULER] Next run at {target.strftime('%Y-%m-%d %H:%M IST')} "
                        f"({sleep_s/3600:.1f}h away)")
            time.sleep(min(sleep_s - 60, 300))   # wake up 1 min before, sleep in ≤5-min chunks
            continue

        # Final stretch — wait out the last seconds then fire
        time.sleep(max(sleep_s, 0))

        run_date = datetime.now(IST).strftime("%Y-%m-%d")
        logger.info(f"[SCHEDULER] {RUN_HOUR:02d}:{RUN_MINUTE:02d} IST — running backtest for {run_date}")
        try:
            run_backtest(run_date)
        except Exception as e:
            logger.error(f"[SCHEDULER] Backtest failed: {e}", exc_info=True)

        time.sleep(120)   # prevent double-firing within the same minute


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "run"
    if mode == "schedule":
        run_scheduler()
    else:
        run_date_arg = sys.argv[2] if len(sys.argv) > 2 else None
        run_backtest(run_date_arg)
