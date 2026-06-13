"""
generate_instruments_csv_dhan.py
---------------------------------
Generates valid_instruments.csv from the Dhan scrip master CSV.

Drop-in replacement for the Zerodha Kite version — produces the SAME
output columns so instrument_lookup.py and instrument_finder_FAST.py
work without any changes.

OUTPUT COLUMNS (unchanged from Kite version):
  symbol, tradingsymbol, strike, option_type, expiry_date,
  tick_size, lot_size, exchange, instrument_type

DATA SOURCE:
  https://images.dhan.co/api-data/api-scrip-master.csv
  Downloaded fresh on each run (cached locally if download fails).

COVERAGE:
  OPTIDX  (NSE/BSE)  → index options     → NFO / BFO
  OPTSTK  (NSE/BSE)  → stock options     → NFO / BFO
  FUTIDX  (NSE/BSE)  → index futures     → NFO / BFO
  FUTSTK  (NSE/BSE)  → stock futures     → NFO / BFO
  FUTCUR  (BSE/NSE)  → currency futures  → CDS
  OPTCUR  (BSE/NSE)  → currency options  → CDS
  FUTCOM  (MCX)      → commodity futures → MCX
  OPTFUT  (MCX)      → commodity options → MCX

RUN DAILY at market open (before TG reader starts):
  python generate_instruments_csv_dhan.py

"""

import csv
import io
import logging
import shutil
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

import requests
import pandas as pd

# ── Windows encoding fix ──────────────────────────────────────────────────────
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── Master Config Hub ─────────────────────────────────────────────────────────
sys.path.append(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
from master_resource import MasterResource, get_instruments_path

# ── Logging ───────────────────────────────────────────────────────────────────
log_ts   = datetime.now().strftime('%d%b%Y_%H_%M_%S').upper()
log_dir  = Path(MasterResource.MASTER_ROOT) / 'logs'
log_dir.mkdir(parents=True, exist_ok=True)
log_file = str(log_dir / f"generate_instruments_dhan_{log_ts}.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - INSTRUMENTS - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("GEN_INSTRUMENTS")
logger.info(f"[LOG] Writing to: {log_file}")

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIP_MASTER_URL  = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_CACHE_PATH  = Path(MasterResource.MASTER_ROOT) / "data" / "dhan_scrip_master.csv"
OUTPUT_FILE       = Path(get_instruments_path())

# ── Expiry windows (days from today) ─────────────────────────────────────────
EXPIRY_WINDOW = {
    "NFO": 45,   # index + stock options/futures on NSE
    "BFO": 45,   # index + stock options/futures on BSE
    "CDS": 60,   # currency
    "MCX": 90,   # commodities
}

# ── Instrument name → (exchange_key, instrument_type_output) ─────────────────
# exchange_key is looked up further using SEM_EXM_EXCH_ID
INSTRUMENT_MAP = {
    # name          : (exchange by exch_id,              opt/fut flag)
    "OPTIDX":        ({"NSE": "NFO", "BSE": "BFO"},      "OPT"),
    "OPTSTK":        ({"NSE": "NFO", "BSE": "BFO"},      "OPT"),
    "FUTIDX":        ({"NSE": "NFO", "BSE": "BFO"},      "FUT"),
    "FUTSTK":        ({"NSE": "NFO", "BSE": "BFO"},      "FUT"),
    "FUTCUR":        ({"BSE": "CDS", "NSE": "CDS"},      "FUT"),
    "OPTCUR":        ({"BSE": "CDS", "NSE": "CDS"},      "OPT"),
    "FUTCOM":        ({"MCX": "MCX"},                    "FUT"),
    "OPTFUT":        ({"MCX": "MCX"},                    "OPT"),
}


# ── Step 1: Download scrip master ─────────────────────────────────────────────

def download_scrip_master() -> Path:
    logger.info(f"[DOWNLOAD] Fetching Dhan scrip master from {SCRIP_MASTER_URL} ...")
    try:
        resp = requests.get(SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        SCRIP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCRIP_CACHE_PATH.write_text(resp.text, encoding="utf-8")
        size_kb = len(resp.content) // 1024
        logger.info(f"[DOWNLOAD] Saved → {SCRIP_CACHE_PATH}  ({size_kb} KB)")
    except Exception as e:
        logger.error(f"[DOWNLOAD] Failed: {e}")
        if SCRIP_CACHE_PATH.exists():
            age_h = (datetime.now().timestamp() - SCRIP_CACHE_PATH.stat().st_mtime) / 3600
            logger.warning(f"[DOWNLOAD] Using stale cache ({age_h:.1f}h old)")
        else:
            raise RuntimeError("No local cache and download failed — cannot proceed.") from e
    return SCRIP_CACHE_PATH


# ── Step 2: Parse and filter ──────────────────────────────────────────────────

def build_rows(scrip_path: Path) -> list[dict]:
    logger.info(f"[PARSE] Loading {scrip_path} ...")
    df = pd.read_csv(scrip_path, low_memory=False)
    logger.info(f"[PARSE] Total rows in scrip master: {len(df):,}")

    # Parse expiry date
    df["_expiry_date"] = pd.to_datetime(
        df["SEM_EXPIRY_DATE"], errors="coerce"
    ).dt.date

    today  = date.today()
    rows   = []
    counts = {}

    for inst_name, (exch_map, flag) in INSTRUMENT_MAP.items():
        subset = df[df["SEM_INSTRUMENT_NAME"] == inst_name].copy()
        if subset.empty:
            logger.info(f"[{inst_name}] No rows in scrip master — skipping")
            continue

        added = 0
        for _, r in subset.iterrows():
            exch_id     = str(r.get("SEM_EXM_EXCH_ID", "")).strip().upper()
            exchange    = exch_map.get(exch_id)
            if exchange is None:
                continue  # unknown exchange for this instrument type

            expiry_date = r["_expiry_date"]
            if pd.isna(expiry_date) or expiry_date is None:
                continue

            window_days = EXPIRY_WINDOW.get(exchange, 45)
            if not (today <= expiry_date <= today + timedelta(days=window_days)):
                continue

            # Resolve option_type / instrument_type column
            if flag == "OPT":
                opt_type = str(r.get("SEM_OPTION_TYPE", "")).strip().upper()
                if opt_type not in ("CE", "PE"):
                    continue   # skip XX rows (futures disguised as options)
                instrument_type_out = opt_type
            else:
                opt_type            = "FUT"
                instrument_type_out = "FUT"

            # Symbol: extract from SEM_TRADING_SYMBOL prefix (most reliable across NSE/BSE/MCX)
            # e.g. "NIFTY-Apr2026-24000-CE" → "NIFTY"
            # Fall back to SM_SYMBOL_NAME only if trading symbol is missing
            ts = str(r.get("SEM_TRADING_SYMBOL", "")).strip()
            if ts and "-" in ts:
                symbol = ts.split("-")[0].upper()
            else:
                symbol = str(r.get("SM_SYMBOL_NAME", "")).strip().upper()
            if not symbol or symbol == "NAN":
                continue  # skip rows with no resolvable symbol

            strike = r.get("SEM_STRIKE_PRICE", 0)
            try:
                strike = float(strike)
            except (TypeError, ValueError):
                strike = 0.0

            tick_size = r.get("SEM_TICK_SIZE", 0.05)
            try:
                tick_size = float(tick_size)
            except (TypeError, ValueError):
                tick_size = 0.05

            lot_size = r.get("SEM_LOT_UNITS", 1)
            try:
                lot_size = int(float(lot_size))
            except (TypeError, ValueError):
                lot_size = 1

            rows.append({
                "symbol":          symbol,
                "tradingsymbol":   str(r.get("SEM_TRADING_SYMBOL", "")).strip(),
                "strike":          strike,
                "option_type":     opt_type,
                "expiry_date":     expiry_date.strftime("%Y-%m-%d"),
                "tick_size":       tick_size,
                "lot_size":        lot_size,
                "exchange":        exchange,
                "instrument_type": instrument_type_out,
            })
            added += 1

        counts[inst_name] = added
        logger.info(f"[{inst_name:8}] {added:6,} rows added  (exchange map: {exch_map})")

    # Log per-exchange unique symbols (helpful for sanity check)
    for exch in ("NFO", "BFO", "CDS", "MCX"):
        syms = sorted({r["symbol"] for r in rows if r["exchange"] == exch})
        if syms:
            preview = ", ".join(syms[:15]) + ("..." if len(syms) > 15 else "")
            logger.info(f"[{exch}] {len(syms)} unique symbols: {preview}")

    return rows


# ── Step 3: Write CSV ─────────────────────────────────────────────────────────

FIELDNAMES = [
    "symbol", "tradingsymbol", "strike", "option_type",
    "expiry_date", "tick_size", "lot_size", "exchange", "instrument_type",
]

def write_csv(rows: list[dict], output_path: Path):
    # Backup existing file
    if output_path.exists():
        backup = output_path.with_suffix(".csv.bak")
        shutil.copy2(output_path, backup)
        logger.info(f"[BACKUP] Old CSV backed up → {backup}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"[WRITE] {len(rows):,} rows written → {output_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print()
    print("=" * 70)
    print("  DHAN INSTRUMENTS CSV GENERATOR")
    print(f"  Date  : {date.today()}")
    print(f"  Output: {OUTPUT_FILE}")
    print("=" * 70)

    scrip_path = download_scrip_master()
    rows       = build_rows(scrip_path)

    if not rows:
        logger.error("[ABORT] No rows generated — output file NOT updated.")
        sys.exit(1)

    write_csv(rows, OUTPUT_FILE)

    # Also write Parquet for O(1) lot-size lookup by lot_cache.py
    try:
        import pandas as pd
        parquet_path = OUTPUT_FILE.with_suffix(".parquet")
        pd.read_csv(OUTPUT_FILE).to_parquet(parquet_path, index=False)
        logger.info(f"[WRITE] Parquet saved → {parquet_path}")
    except Exception as e:
        logger.warning(f"[WRITE] Parquet write failed (non-fatal): {e}")

    print()
    print("=" * 70)
    print(f"  DONE — {len(rows):,} instruments written to valid_instruments.csv")
    print(f"  Breakdown by exchange:")
    for exch in ("NFO", "BFO", "CDS", "MCX"):
        n = sum(1 for r in rows if r["exchange"] == exch)
        if n:
            print(f"    {exch}: {n:,}")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
