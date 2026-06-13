"""
backfill_futures_candles.py
---------------------------
Fetch and store index futures 1m/5m/15m candles (real volume + OI) via Kite API.

Uses KiteConnect historical_data() which natively returns:
  open, high, low, close, volume, oi  — everything needed for blast analysis.

Supported indices:  NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX
Exchange:           NFO (NSE futures), BFO (BSE futures — SENSEX)

Usage:
    python backfill_futures_candles.py              # all indices, 60 days back
    python backfill_futures_candles.py --days 30    # last 30 calendar days
    python backfill_futures_candles.py --sym NIFTY  # single index

Rate limit: Kite historical API allows ~3 req/s. Script pauses 0.35 s between calls.
Re-running is safe — INSERT OR REPLACE deduplicates on (tradingsymbol, dt).
"""

import argparse
import logging
import sys
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_MASTER_LIB = _HERE.parent / "MasterConfiguration" / "lib"
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_MASTER_LIB))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_futures")

# ── Imports ───────────────────────────────────────────────────────────────────
import pandas as pd
import pytz

from futures_candle_store import (
    futures_db_stats, ensure_tables, FUTURES_CONFIG, store_dataframe
)

IST = pytz.timezone("Asia/Kolkata")

# Kite instrument type for index futures
_FUT_EXCHANGE = {
    "NIFTY":      "NFO",
    "BANKNIFTY":  "NFO",
    "FINNIFTY":   "NFO",
    "MIDCPNIFTY": "NFO",
    "SENSEX":     "BFO",
}

# How many calendar days Kite allows for 1-min historical (practical limit)
KITE_MAX_DAYS_1MIN = 60


def _resolve_futures_token(kite, symbol: str, expiry_offset: int = 0):
    """
    Returns (instrument_token, tradingsymbol, exchange) for near-month futures.
    Uses Kite instruments list.
    """
    exchange = _FUT_EXCHANGE.get(symbol.upper())
    if not exchange:
        raise ValueError(f"Unknown symbol: {symbol}")

    logger.debug(f"[KITE] Loading {exchange} instruments for {symbol}...")
    insts = pd.DataFrame(kite.instruments(exchange))
    insts["expiry"] = pd.to_datetime(insts["expiry"]).dt.date

    today = date.today()
    mask = (
        (insts["name"]            == symbol.upper()) &
        (insts["instrument_type"] == "FUT") &
        (insts["expiry"]          >= today)
    )
    subset = insts[mask].sort_values("expiry").reset_index(drop=True)

    if len(subset) <= expiry_offset:
        raise RuntimeError(
            f"Not enough expiries for {symbol} in {exchange} "
            f"(need offset {expiry_offset}, found {len(subset)})"
        )

    row = subset.iloc[expiry_offset]
    return int(row["instrument_token"]), str(row["tradingsymbol"]), exchange


def _fetch_kite_futures(kite, symbol: str, from_date: str, to_date: str) -> int:
    """
    Fetch 1m futures candles from Kite and persist to DB.
    Automatically chunks the request into 60-day windows if needed.

    Returns total 1m rows saved.
    """
    try:
        token, trading_sym, exchange = _resolve_futures_token(kite, symbol)
    except Exception as e:
        logger.warning(f"[KITE] Token resolution failed for {symbol}: {e}")
        return 0

    sym_store = FUTURES_CONFIG[symbol.upper()]["sym"]
    logger.info(
        f"[KITE] {sym_store}  ({trading_sym}  token={token})  "
        f"{from_date} → {to_date}"
    )

    # Kite 1-min data: chunk into ≤60-day windows to avoid API limits
    all_candles = []
    chunk_start = datetime.strptime(from_date, "%Y-%m-%d")
    chunk_end   = datetime.strptime(to_date,   "%Y-%m-%d")
    MAX_DELTA   = timedelta(days=58)  # conservative

    ptr = chunk_start
    while ptr <= chunk_end:
        win_end = min(ptr + MAX_DELTA, chunk_end)
        try:
            candles = kite.historical_data(
                instrument_token = token,
                from_date        = ptr.strftime("%Y-%m-%d 09:15:00"),
                to_date          = win_end.strftime("%Y-%m-%d 15:30:00"),
                interval         = "minute",
                oi               = True,
            )
            if candles:
                all_candles.extend(candles)
                logger.info(
                    f"[KITE]   window {ptr.date()}→{win_end.date()}: "
                    f"{len(candles)} candles"
                )
            else:
                logger.debug(f"[KITE]   window {ptr.date()}→{win_end.date()}: empty")
        except Exception as e:
            logger.warning(f"[KITE]   window {ptr.date()}→{win_end.date()} error: {e}")
        ptr = win_end + timedelta(days=1)
        time.sleep(0.35)   # Kite rate limit

    if not all_candles:
        logger.warning(f"[KITE] No candles returned for {symbol}")
        return 0

    df = pd.DataFrame(all_candles).rename(columns={"date": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Convert to naive IST (drop tz)
    if df["timestamp"].dt.tz is not None:
        df["timestamp"] = (df["timestamp"]
                           .dt.tz_convert(IST)
                           .dt.tz_localize(None))

    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].fillna(0).astype(int)
    df["oi"]     = df.get("oi", pd.Series(0, index=df.index)).fillna(0).astype(int)

    df = (df[["timestamp", "open", "high", "low", "close", "volume", "oi"]]
            .drop_duplicates(subset=["timestamp"])
            .sort_values("timestamp")
            .reset_index(drop=True))

    return store_dataframe(symbol, df)


def main():
    parser = argparse.ArgumentParser(
        description="Backfill index futures candles via Kite historical_data"
    )
    parser.add_argument("--days", type=int, default=60,
                        help="Calendar days to go back (default: 60, max: 60 for 1m)")
    parser.add_argument("--sym",  type=str, default=None,
                        help="Single index to fetch (e.g. NIFTY). Default: all.")
    args = parser.parse_args()

    symbols = [args.sym.upper()] if args.sym else list(FUTURES_CONFIG.keys())
    days    = min(args.days, KITE_MAX_DAYS_1MIN)

    print()
    print("=" * 65)
    print(f"  Futures Candle Backfill via Kite — {date.today()}")
    print(f"  Indices : {', '.join(symbols)}")
    print(f"  Lookback: {days} calendar days (Kite 1m limit: 60 days)")
    print("=" * 65)
    print()

    # ── Init Kite client ──────────────────────────────────────────────────────
    try:
        from kite_candle_store import get_kite
        kite = get_kite()
        if kite is None:
            raise RuntimeError("Kite client returned None — check kite_config.json token")
        logger.info("Kite client ready")
    except Exception as e:
        logger.error(f"Kite init failed: {e}")
        sys.exit(1)

    # ── Existing DB state ─────────────────────────────────────────────────────
    ensure_tables()
    before = futures_db_stats()
    if before:
        print("  Existing futures data (before backfill):")
        for k, v in sorted(before.items()):
            if k.endswith("_1m"):
                print(f"    {k:25s}  {v['rows']:6d} rows  "
                      f"{v['from'][:10]} → {v['to'][:10]}")
    else:
        print("  No existing futures data — starting fresh.")
    print()

    # ── Fetch via Kite ────────────────────────────────────────────────────────
    to_date   = date.today().strftime("%Y-%m-%d")
    from_date = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    results = {}
    for sym in symbols:
        results[sym] = _fetch_kite_futures(kite, sym, from_date, to_date)
        time.sleep(0.5)   # extra pause between symbols

    # ── Summary ───────────────────────────────────────────────────────────────
    after = futures_db_stats()
    print()
    print("=" * 65)
    print("  Backfill complete")
    print("=" * 65)
    print(f"  {'Symbol':<15}  {'1m rows fetched':>16}")
    print("  " + "-" * 35)
    for sym, n in results.items():
        print(f"  {sym:<15}  {n:>16,}")

    print()
    print("  Futures DB (1m) after backfill:")
    for k, v in sorted(after.items()):
        if k.endswith("_1m"):
            added = v["rows"] - before.get(k, {}).get("rows", 0)
            print(f"    {k:25s}  {v['rows']:6d} rows  "
                  f"{v['from'][:10]} → {v['to'][:10]}  (+{added})")

    print()
    print("  Real volume + OI now available in candles_futures_1min / 5min / 15min.")
    print("  Re-run anytime — safe to re-run (INSERT OR REPLACE).")
    print()


if __name__ == "__main__":
    main()
