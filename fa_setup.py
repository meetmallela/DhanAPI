"""
fa_setup.py
-----------
One-time setup for the Fundamental Analysis Paper Portfolio Agent.

  1. Creates all FA tables in trading.db
  2. Seeds default metric weights into fa_score_weights
  3. Runs an initial full universe scan (optional — pass --no-scan to skip)

Run once before starting the FA agent:
    python fa_setup.py            # setup + initial scan
    python fa_setup.py --no-scan  # setup only (tables + weights)
"""

import sys
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fa_setup")

from agents.fundamental_agent import ensure_fa_tables, _run_scan, _load_universe
from core.fa_scorer import FAScorer, DEFAULT_WEIGHTS
from master_resource import MasterResource

DB_PATH = Path(MasterResource.MASTER_ROOT) / "data" / "trading.db"


def seed_weights(scorer: FAScorer):
    """Seed default weights into fa_score_weights (only if table is empty)."""
    import sqlite3
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    existing = con.execute("SELECT COUNT(*) FROM fa_score_weights").fetchone()[0]
    con.close()

    if existing:
        logger.info(f"[FA] fa_score_weights already has {existing} rows — skipping seed")
        return

    scorer.save_weights(DEFAULT_WEIGHTS)
    logger.info(f"[FA] Default weights seeded: {DEFAULT_WEIGHTS}")


def main():
    no_scan = "--no-scan" in sys.argv

    logger.info("=" * 55)
    logger.info("  FA Agent Setup")
    logger.info(f"  DB : {DB_PATH}")
    logger.info("=" * 55)

    # 1. Tables
    ensure_fa_tables()

    # 2. Weights
    scorer = FAScorer(str(DB_PATH))
    seed_weights(scorer)

    if no_scan:
        logger.info("[FA] --no-scan flag set: skipping initial universe scan")
        print("\nSetup complete (tables + weights). Skipped initial scan.")
        return

    # 3. Initial scan
    universe = _load_universe()
    logger.info(f"[FA] Starting initial scan of {len(universe)} symbols ...")
    print(f"\nScanning {len(universe)} symbols — this will take ~{len(universe)*1.5/60:.0f} minutes.\n")

    added = _run_scan(scorer)

    print(f"\nFA Setup complete.")
    print(f"  Tables  : created in {DB_PATH}")
    print(f"  Weights : seeded (7 metrics)")
    print(f"  Scan    : {added} picks added to fa_portfolio")
    print(f"\nNext: run 'python fa_setup.py' again on Saturday morning")
    print(f"      or start DhanOmniEngine_v2.py — the FAAgent daemon handles weekly scans.")


if __name__ == "__main__":
    main()
