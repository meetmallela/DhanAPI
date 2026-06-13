"""
instrument_lookup.py
====================
Resolves tradingsymbols by looking up valid_instruments.csv directly.

THE ONLY LOGIC:
    1. Filter CSV rows where symbol      == given symbol   (e.g. 'SENSEX')
    2. Filter further where  strike      == given strike   (e.g. 83100)
    3. Filter further where  option_type == given type     (e.g. 'CE')
    4. From matching rows, pick the one with LOWEST expiry_date
    5. Return that row's tradingsymbol verbatim — no date math, no format construction

USAGE in signal_parser_with_futures.py
---------------------------------------
    from instrument_lookup import InstrumentLookup

    # At class init (load once):
    self.instrument_lookup = InstrumentLookup(
        r'C:\\Users\\meetm\\OneDrive\\Desktop\\GCPPythonCode\\TGAPI\\TB_12Dec25\\valid_instruments.csv'
    )

    # Replace entire INDEX ENRICH block with:
    result = self.instrument_lookup.resolve(symbol, strike, option_type)
    if result is None:
        self.logger.error(f"[LOOKUP FAILED] {symbol} {strike} {option_type} not in CSV")
        return None

    tradingsymbol = result['tradingsymbol']   # e.g. 'SENSEX2630583100CE'
    expiry_date   = result['expiry_date']     # e.g. date(2026, 3, 5)
    lot_size      = result['lot_size']        # e.g. 20
    exchange      = result['exchange']        # e.g. 'BFO'

USAGE in order_placer_db_production.py (pre-order guard)
---------------------------------------------------------
    # Before kite.place_order():
    is_valid, reason = instrument_lookup.validate_tradingsymbol(tradingsymbol)
    if not is_valid:
        logger.error(f"[PRE-ORDER BLOCK] {reason}")
        return
"""

import csv
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class InstrumentLookup:

    def __init__(self, csv_path: str = None):
        import sys
        from pathlib import Path
        sys.path.append(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
        from master_resource import get_instruments_path

        if csv_path is None:
            try:
                self.csv_path = get_instruments_path()
            except:
                self.csv_path = 'valid_instruments.csv'
        else:
            self.csv_path = csv_path
        # index: (symbol_upper, strike_int, option_type_upper) -> list of row dicts
        self._index: dict = {}
        self._all_tradingsymbols: set = set()
        self._loaded = False
        self._load()

    def _load(self):
        path = Path(self.csv_path)
        if not path.exists():
            logger.error(f"[INSTRUMENT_LOOKUP] CSV not found: {self.csv_path}")
            return

        row_count = 0
        skipped   = 0

        try:
            with open(path, newline='', encoding='utf-8-sig') as f:
                sample = f.read(4096)
                f.seek(0)
                delimiter = '\t' if '\t' in sample else ','
                reader = csv.DictReader(f, delimiter=delimiter)

                for raw in reader:
                    row = {k.strip(): v.strip() for k, v in raw.items() if k}
                    try:
                        symbol      = row['symbol'].upper()
                        tradingsym  = row['tradingsymbol'].strip()
                        strike      = int(float(row['strike']))
                        opt_type    = row['option_type'].upper()
                        expiry_date = self._parse_date(row.get('expiry_date', ''))
                        lot_size    = int(float(row.get('lot_size', 1)))
                        exchange    = row.get('exchange', '').strip().upper()
                    except (KeyError, ValueError, TypeError):
                        skipped += 1
                        continue

                    if expiry_date is None:
                        skipped += 1
                        continue

                    record = {
                        'symbol':        symbol,
                        'tradingsymbol': tradingsym,
                        'strike':        strike,
                        'option_type':   opt_type,
                        'expiry_date':   expiry_date,
                        'lot_size':      lot_size,
                        'exchange':      exchange,
                    }

                    key = (symbol, strike, opt_type)
                    self._index.setdefault(key, []).append(record)
                    self._all_tradingsymbols.add(tradingsym)
                    row_count += 1

            self._loaded = True
            logger.info(
                f"[INSTRUMENT_LOOKUP] Loaded {row_count} rows "
                f"({skipped} skipped) | {len(self._index)} unique combos | "
                f"{self.csv_path}"
            )

        except Exception as e:
            logger.error(f"[INSTRUMENT_LOOKUP] Load failed: {e}")

    @staticmethod
    def _parse_date(s: str) -> Optional[date]:
        for fmt in ('%d-%m-%Y', '%Y-%m-%d', '%d/%m/%Y', '%Y/%m/%d', '%d-%b-%Y', '%d-%b-%y'):
            try:
                return datetime.strptime(s.strip(), fmt).date()
            except ValueError:
                continue
        return None

    def resolve(self, symbol: str, strike: int, option_type: str) -> Optional[dict]:
        """
        Step 1: Filter where symbol      == symbol
        Step 2: Filter where strike      == strike
        Step 3: Filter where option_type == option_type
        Step 4: Pick row with LOWEST expiry_date
        Step 5: Return that row (tradingsymbol verbatim from CSV)

        Returns None if no match.
        """
        if not self._loaded:
            logger.error("[INSTRUMENT_LOOKUP] CSV not loaded")
            return None

        key = (symbol.upper(), int(strike), option_type.upper())
        candidates = self._index.get(key)

        if not candidates:
            logger.warning(
                f"[INSTRUMENT_LOOKUP] No match: "
                f"symbol={symbol} strike={strike} type={option_type}"
            )
            return None

        # Pick the nearest (lowest) expiry
        best = min(candidates, key=lambda r: r['expiry_date'])

        logger.info(
            f"[INSTRUMENT_LOOKUP] {symbol} {strike} {option_type} -> "
            f"{best['tradingsymbol']} | Expiry: {best['expiry_date']} | "
            f"Lot: {best['lot_size']} | Exch: {best['exchange']}"
        )
        return best

    def validate_tradingsymbol(self, tradingsymbol: str) -> tuple:
        """
        Returns (True, 'OK') if tradingsymbol is in CSV.
        Returns (False, reason) if not — block the order.
        """
        if not self._loaded:
            return False, "Instrument CSV not loaded"

        if tradingsymbol in self._all_tradingsymbols:
            return True, "OK"

        return False, (
            f"'{tradingsymbol}' NOT in valid_instruments.csv — "
            f"order blocked (bad expiry encoding or wrong instrument)"
        )

    def is_loaded(self) -> bool:
        return self._loaded

    def is_stale(self) -> bool:
        if not self._index:
            return True
        today = date.today()
        return all(
            r['expiry_date'] < today
            for rows in self._index.values()
            for r in rows
        )

    def summary(self) -> str:
        if not self._loaded:
            return "InstrumentLookup: NOT LOADED"
        today = date.today()
        symbols = sorted({k[0] for k in self._index})
        upcoming = sum(
            1 for rows in self._index.values()
            for r in rows if r['expiry_date'] >= today
        )
        total = sum(len(v) for v in self._index.values())
        return (
            f"InstrumentLookup | {total} rows | {upcoming} upcoming | "
            f"Symbols: {', '.join(symbols)}"
        )
