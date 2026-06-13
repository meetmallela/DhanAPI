"""
prefetch_option_candles.py
--------------------------
Backfill NIFTY option candles for Apr 16-24 using tokens already in kite_candles.db.

Strategy:
  - Expired contracts are NOT in the current Kite instruments list, so we cannot
    use resolve_option_token() for them. Instead we read the instrument_token
    values already stored in kite_candles.db from prior trading-day fetches.
  - For each known (token, tradingsymbol, expiry) we fetch 1-min candles for
    every trading day in the expiry's active window where candles are missing.
  - Apr-21-expiry contracts: active Apr 16-21
  - Apr-28-expiry contracts: active Apr 22-28 (we have Apr 22-24 data)

Usage:
    python prefetch_option_candles.py            # Apr 16-24 default
    python prefetch_option_candles.py --dry-run  # show what would be fetched
"""

import logging
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - PREFETCH - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("prefetch_option_candles")

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")

from master_resource import MasterResource
import kite_candle_store as kcs

KITE_DB = str(Path(MasterResource.MASTER_ROOT) / "data" / "kite_candles.db")


def _known_nifty_option_tokens() -> list[dict]:
    """Read all distinct NIFTY option (token, tradingsymbol, exchange) from DB."""
    conn = sqlite3.connect(KITE_DB)
    rows = conn.execute("""
        SELECT DISTINCT instrument_token, tradingsymbol, exchange
        FROM candles_1min
        WHERE tradingsymbol LIKE 'NIFTY_%_CE_%'
           OR tradingsymbol LIKE 'NIFTY_%_PE_%'
        ORDER BY tradingsymbol
    """).fetchall()
    conn.close()
    out = []
    for token, sym, exch in rows:
        # sym format: NIFTY_{strike}_{opt_type}_{expiry}  e.g. NIFTY_24350_CE_2026-04-28
        parts = sym.split("_")
        expiry_str = parts[3] if len(parts) >= 4 else None
        out.append({
            "token":    token,
            "sym":      sym,
            "exchange": exch or "NFO",
            "expiry":   date.fromisoformat(expiry_str) if expiry_str else None,
        })
    return out


def _has_candles(token: int, d: date) -> bool:
    """True if DB already has a full day of 1-min candles for (token, date)."""
    conn = sqlite3.connect(KITE_DB)
    n = conn.execute(
        "SELECT COUNT(*) FROM candles_1min WHERE instrument_token=? AND DATE(dt)=?",
        (token, d.isoformat()),
    ).fetchone()[0]
    conn.close()
    return n >= 300   # 375 expected; accept 300+ as complete enough


def _count_nifty_option_rows(d: date) -> int:
    conn = sqlite3.connect(KITE_DB)
    n = conn.execute(
        "SELECT COUNT(*) FROM candles_1min "
        "WHERE DATE(dt)=? AND (tradingsymbol LIKE 'NIFTY_%_CE_%' OR tradingsymbol LIKE 'NIFTY_%_PE_%')",
        (d.isoformat(),),
    ).fetchone()[0]
    conn.close()
    return n


def _active_dates(expiry: date, window_start: date, window_end: date) -> list[date]:
    """Trading days in [window_start .. min(expiry, window_end)]."""
    end = min(expiry, window_end)
    days = []
    cur = window_start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def run(start_d: date, end_d: date, dry_run: bool = False):
    logger.info(f"Prefetch NIFTY option candles  {start_d} -> {end_d}")
    if dry_run:
        logger.info("DRY RUN -- no data will be fetched")

    kcs.ensure_tables()

    contracts = _known_nifty_option_tokens()
    logger.info(f"Known NIFTY option contracts in DB: {len(contracts)}")
    for c in contracts:
        logger.info(f"  token={c['token']:12d}  {c['sym']}")

    total_fetched = 0
    total_skipped = 0
    total_missing = 0

    for c in contracts:
        token  = c["token"]
        sym    = c["sym"]
        exch   = c["exchange"]
        expiry = c["expiry"]
        if expiry is None:
            logger.warning(f"  {sym}: cannot parse expiry, skipping")
            continue

        active = _active_dates(expiry, start_d, end_d)
        logger.info(f"{sym}  (expiry {expiry})  -- {len(active)} trading days in window")

        for d in active:
            if _has_candles(token, d):
                logger.info(f"  {d}  already cached -- skip")
                total_skipped += 1
                continue

            if dry_run:
                logger.info(f"  {d}  [DRY RUN] would fetch")
                total_missing += 1
                continue

            df = kcs._fetch_from_kite(token, d.isoformat(), interval="minute")
            if df is not None and not df.empty:
                kcs.save_to_db(token, sym, exch, df, "minute")
                logger.info(f"  {d}  fetched {len(df)} candles  OK")
                total_fetched += 1
            else:
                logger.warning(f"  {d}  Kite returned no data (contract may not have traded)")
                total_missing += 1

            time.sleep(0.35)   # ~3 req/s Kite limit

    logger.info("=" * 60)
    if dry_run:
        logger.info(f"DRY RUN done. cached={total_skipped}  would_fetch={total_missing}")
    else:
        logger.info(f"Done. fetched={total_fetched}  already_cached={total_skipped}  no_data={total_missing}")

    logger.info("Per-day option row counts after prefetch:")
    cur = start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            n = _count_nifty_option_rows(cur)
            contracts_approx = n // 375
            logger.info(f"  {cur}  rows={n:6d}  (~{contracts_approx} full-day contracts)")
        cur += timedelta(days=1)

    logger.info("")
    logger.info("Run backtest with:")
    logger.info(f"  python -m backtest.multi_strike_backtest {start_d} {end_d}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    args    = [a for a in sys.argv[1:] if not a.startswith("--")]

    if len(args) == 2:
        start_d = date.fromisoformat(args[0])
        end_d   = date.fromisoformat(args[1])
    elif len(args) == 1:
        start_d = end_d = date.fromisoformat(args[0])
    else:
        start_d = date(2026, 4, 16)
        end_d   = date(2026, 4, 24)

    run(start_d, end_d, dry_run=dry_run)
