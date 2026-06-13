"""
lot_cache.py — O(1) lot-size lookup backed by valid_instruments.parquet (or CSV).

Loaded once at process start.  generate_instruments_csv_dhan.py writes the
parquet daily before trading begins, so lot sizes are always current
(SEBI revises them each expiry series).

Usage:
    from lot_cache import get_lot_size
    qty = get_lot_size("NIFTY")   # e.g. 65
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PARQUET = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\valid_instruments.parquet")
_CSV     = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\valid_instruments.csv")

_cache: dict[str, int] = {}


def _load() -> None:
    global _cache
    try:
        import pandas as pd
        if _PARQUET.exists():
            df  = pd.read_parquet(_PARQUET, columns=["symbol", "lot_size"])
            src = "parquet"
        elif _CSV.exists():
            df  = pd.read_csv(_CSV, usecols=["symbol", "lot_size"])
            src = "csv"
        else:
            logger.error("[LOT_CACHE] Neither %s nor %s found", _PARQUET, _CSV)
            return
        _cache = (
            df.groupby("symbol")["lot_size"]
            .first()
            .astype(int)
            .to_dict()
        )
        logger.info("[LOT_CACHE] Loaded %d symbols from %s", len(_cache), src)
    except Exception as exc:
        logger.error("[LOT_CACHE] Load error: %s", exc)


def get_lot_size(symbol: str, default: int = 1) -> int:
    """Return 1-lot unit count for symbol.  Loads cache lazily on first call."""
    if not _cache:
        _load()
    return _cache.get(symbol.upper(), default)


def reload() -> None:
    """Force a fresh reload from disk (call after generate_instruments runs)."""
    global _cache
    _cache = {}
    _load()
