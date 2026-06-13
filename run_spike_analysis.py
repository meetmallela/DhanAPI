"""
run_spike_analysis.py
─────────────────────
End-to-end spike pattern analysis pipeline.

Steps:
  1. fetch    — pull 1M candles from Kite + Dhan (requires valid Kite token)
  2. detect   — find spikes and extract pre-spike feature fingerprints
  3. hypothesis — build pattern library from training set (last 6M)
  4. backtest — validate library against test set (months 7-12)
  5. all      — run steps 1-4 in sequence

Usage:
  python run_spike_analysis.py fetch --months 12
  python run_spike_analysis.py detect
  python run_spike_analysis.py hypothesis
  python run_spike_analysis.py backtest
  python run_spike_analysis.py all --months 12

  # Quick demo using only locally cached kite_candles.db data:
  python run_spike_analysis.py migrate   (copy from kite_candles.db)
  python run_spike_analysis.py detect
  python run_spike_analysis.py hypothesis
"""

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)

sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")

from spike_analysis.db       import ensure_tables
from spike_analysis.fetcher  import (fetch_kite, fetch_dhan,
                                     migrate_from_kite_candles_db,
                                     coverage_report)
from spike_analysis.detector  import run_detection
from spike_analysis.hypothesis import run_hypothesis
from spike_analysis.backtest   import run_backtest


def main():
    ap = argparse.ArgumentParser(description="Spike pattern analysis pipeline")
    ap.add_argument("step", choices=["fetch","detect","hypothesis","backtest","all","migrate","coverage"])
    ap.add_argument("--months", type=int, default=12, help="Months of history")
    args = ap.parse_args()

    ensure_tables()

    if args.step == "coverage":
        coverage_report()

    elif args.step == "migrate":
        print("Migrating from kite_candles.db...")
        t = migrate_from_kite_candles_db()
        print("Migrated:", t)
        coverage_report()

    elif args.step == "fetch":
        print(f"Fetching {args.months}M from Kite...")
        kt = fetch_kite(args.months)
        print("Kite:", kt)
        print(f"Fetching {args.months}M from Dhan...")
        dt = fetch_dhan(args.months)
        print("Dhan:", dt)
        coverage_report()

    elif args.step == "detect":
        run_detection()

    elif args.step == "hypothesis":
        run_hypothesis()

    elif args.step == "backtest":
        run_backtest()

    elif args.step == "all":
        print(f"=== Step 1: Fetch {args.months}M ===")
        fetch_kite(args.months)
        fetch_dhan(args.months)
        coverage_report()
        print("=== Step 2: Detect spikes ===")
        run_detection()
        print("=== Step 3: Hypothesis ===")
        run_hypothesis()
        print("=== Step 4: Backtest ===")
        run_backtest()


if __name__ == "__main__":
    main()
