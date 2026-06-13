"""
export_signals_and_candles.py
------------------------------
Export signal data and 1-min candle data from local DBs to CSV files.

Output files (written to OUT_DIR):
  signals.csv           — all signals with parsed_data columns expanded
  candles_1min.csv      — 1-min OHLCV for every instrument in kite_candles.db
  signals_with_candles/ — one CSV per signal: signal row + its option's candles

Usage:
  python export_signals_and_candles.py                     # all data
  python export_signals_and_candles.py --date 2026-05-14   # single day
  python export_signals_and_candles.py --from 2026-05-12 --to 2026-05-14
  python export_signals_and_candles.py --date 2026-05-14 --no-per-signal
"""

import argparse
import csv
import io
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

# ── Windows UTF-8 ─────────────────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Paths ──────────────────────────────────────────────────────────────────────
TRADING_DB  = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\trading.db")
CANDLES_DB  = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db")
OUT_DIR     = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI\exports")

# ── Parsed-data field names to expand from JSON ────────────────────────────────
_PARSED_FIELDS = [
    "action", "symbol", "strike", "option_type", "expiry_date",
    "entry_price", "stop_loss", "target", "instrument_type",
    "exchange", "lot_size", "expiry_auto_added",
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _date_clause(col: str, from_date: str | None, to_date: str | None) -> tuple[str, list]:
    """Build a WHERE date() clause and params list."""
    parts, params = [], []
    if from_date:
        parts.append(f"date({col}) >= ?")
        params.append(from_date)
    if to_date:
        parts.append(f"date({col}) <= ?")
        params.append(to_date)
    return (" AND " + " AND ".join(parts)) if parts else "", params


def _expand_parsed(raw_json: str | None) -> dict:
    """Extract known fields from the parsed_data JSON blob."""
    try:
        pd = json.loads(raw_json or "{}")
    except Exception:
        pd = {}
    return {f: pd.get(f, "") for f in _PARSED_FIELDS}


def _candle_sym_for_signal(parsed: dict) -> str | None:
    """
    Build the LIKE pattern to find option candles in kite_candles.db.
    Dhan tradingsymbol format: NIFTY-May2026-23700-CE
    Kite candle format: NIFTY_23700_CE_2026-05-15
    """
    sym  = str(parsed.get("symbol") or "").upper()
    stk  = parsed.get("strike")
    ot   = str(parsed.get("option_type") or "").upper() \
               .replace("CALL", "CE").replace("PUT", "PE")
    if sym and stk and ot in ("CE", "PE"):
        return f"{sym}_{int(float(stk))}_{ot}%"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Export 1: signals.csv
# ─────────────────────────────────────────────────────────────────────────────

def export_signals(from_date: str | None, to_date: str | None) -> Path:
    clause, params = _date_clause("timestamp", from_date, to_date)
    con = sqlite3.connect(TRADING_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT * FROM signals WHERE 1=1 {clause} ORDER BY id", params
    ).fetchall()
    con.close()

    out = OUT_DIR / "signals.csv"
    base_cols = ["id", "channel_id", "channel_name", "message_id",
                 "timestamp", "processed", "instrument_type",
                 "order_id", "order_status", "raw_text"]
    all_cols  = base_cols + _PARSED_FIELDS

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row_dict = dict(r)
            row_dict.update(_expand_parsed(r["parsed_data"]))
            w.writerow({c: row_dict.get(c, "") for c in all_cols})

    print(f"[signals]  {len(rows):,} rows → {out}")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Export 2: candles_1min.csv
# ─────────────────────────────────────────────────────────────────────────────

def export_candles(from_date: str | None, to_date: str | None) -> Path:
    clause, params = _date_clause("dt", from_date, to_date)
    con = sqlite3.connect(CANDLES_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT instrument_token, tradingsymbol, exchange, dt, "
        f"open, high, low, close, volume "
        f"FROM candles_1min WHERE 1=1 {clause} ORDER BY tradingsymbol, dt",
        params,
    ).fetchall()
    con.close()

    out = OUT_DIR / "candles_1min.csv"
    cols = ["instrument_token", "tradingsymbol", "exchange",
            "dt", "open", "high", "low", "close", "volume"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows([dict(r) for r in rows])

    # Summary by symbol
    syms: dict[str, int] = {}
    for r in rows:
        syms[r["tradingsymbol"]] = syms.get(r["tradingsymbol"], 0) + 1
    print(f"[candles]  {len(rows):,} rows, {len(syms)} instruments → {out}")
    for sym, cnt in sorted(syms.items(), key=lambda x: -x[1])[:15]:
        print(f"           {sym:<40} {cnt:>6} bars")
    if len(syms) > 15:
        print(f"           … and {len(syms)-15} more")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Export 3: per-signal CSVs (signals_with_candles/<signal_id>_<sym>.csv)
# ─────────────────────────────────────────────────────────────────────────────

def export_per_signal(from_date: str | None, to_date: str | None) -> None:
    clause, params = _date_clause("timestamp", from_date, to_date)
    tcon = sqlite3.connect(TRADING_DB)
    tcon.row_factory = sqlite3.Row
    signals = tcon.execute(
        f"SELECT * FROM signals WHERE instrument_type IS NOT NULL {clause} ORDER BY id",
        params,
    ).fetchall()
    tcon.close()

    ccon = sqlite3.connect(CANDLES_DB)
    ccon.row_factory = sqlite3.Row

    per_dir = OUT_DIR / "signals_with_candles"
    per_dir.mkdir(parents=True, exist_ok=True)

    written = skipped = 0
    for sig in signals:
        parsed = _expand_parsed(sig["parsed_data"])
        pat    = _candle_sym_for_signal(parsed)
        if not pat:
            skipped += 1
            continue

        sig_date = str(sig["timestamp"])[:10]
        candles  = ccon.execute(
            "SELECT instrument_token, tradingsymbol, exchange, dt, "
            "open, high, low, close, volume "
            "FROM candles_1min "
            "WHERE tradingsymbol LIKE ? AND date(dt) = ? ORDER BY dt",
            (pat, sig_date),
        ).fetchall()

        if not candles:
            skipped += 1
            continue

        sym_tag = f"{parsed.get('symbol','')}_{parsed.get('strike','')}_{parsed.get('option_type','')}"
        fname   = per_dir / f"{sig['id']}_{sym_tag}_{sig_date}.csv"

        with open(fname, "w", newline="", encoding="utf-8") as f:
            # Header block: signal metadata
            f.write("# SIGNAL METADATA\n")
            f.write(f"# signal_id,{sig['id']}\n")
            f.write(f"# channel,{sig['channel_name']}\n")
            f.write(f"# timestamp,{sig['timestamp']}\n")
            f.write(f"# processed,{sig['processed']}\n")
            for k, v in parsed.items():
                f.write(f"# {k},{v}\n")
            f.write("#\n")
            # Candle data
            w = csv.DictWriter(
                f,
                fieldnames=["instrument_token", "tradingsymbol", "exchange",
                            "dt", "open", "high", "low", "close", "volume"],
            )
            w.writeheader()
            w.writerows([dict(r) for r in candles])

        written += 1

    ccon.close()
    print(f"[per-sig]  {written} files written, {skipped} skipped (no candle data)"
          f" → {per_dir}")


# ─────────────────────────────────────────────────────────────────────────────
# Quick DB stats
# ─────────────────────────────────────────────────────────────────────────────

def print_stats() -> None:
    con = sqlite3.connect(TRADING_DB)
    sig_count = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    sig_range = con.execute("SELECT MIN(date(timestamp)), MAX(date(timestamp)) FROM signals").fetchone()
    con.close()

    con2 = sqlite3.connect(CANDLES_DB)
    can_count = con2.execute("SELECT COUNT(*) FROM candles_1min").fetchone()[0]
    can_syms  = con2.execute("SELECT COUNT(DISTINCT tradingsymbol) FROM candles_1min").fetchone()[0]
    can_range = con2.execute("SELECT MIN(date(dt)), MAX(date(dt)) FROM candles_1min").fetchone()
    con2.close()

    print("─" * 60)
    print(f"  trading.db   signals : {sig_count:,} rows  [{sig_range[0]} → {sig_range[1]}]")
    print(f"  kite_candles 1-min   : {can_count:,} rows  {can_syms} instruments")
    print(f"                         [{can_range[0]} → {can_range[1]}]")
    print("─" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Export signals + 1-min candles to CSV")
    ap.add_argument("--date",        help="Single date YYYY-MM-DD (shorthand for --from + --to)")
    ap.add_argument("--from",        dest="from_date", help="Start date YYYY-MM-DD (inclusive)")
    ap.add_argument("--to",          dest="to_date",   help="End date YYYY-MM-DD (inclusive)")
    ap.add_argument("--no-per-signal", action="store_true",
                    help="Skip per-signal CSVs (faster for large date ranges)")
    args = ap.parse_args()

    from_date = args.date or args.from_date
    to_date   = args.date or args.to_date

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    label = f" ({from_date} → {to_date})" if (from_date or to_date) else " (all dates)"
    print(f"\nExporting{label}")
    print_stats()

    export_signals(from_date, to_date)
    export_candles(from_date, to_date)
    if not args.no_per_signal:
        export_per_signal(from_date, to_date)

    print(f"\nDone. Files in: {OUT_DIR}")


if __name__ == "__main__":
    main()
