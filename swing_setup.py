"""
swing_setup.py
--------------
One-time setup for the swing trading universe.

Step 1: Resolves Kite instrument tokens for all symbols in config/swing_universe.json
Step 2: Seeds EOD history (default: last 120 days) for all resolved symbols

Usage:
    python swing_setup.py                    # resolve tokens + seed 120 days
    python swing_setup.py --seed-days 200    # longer history
    python swing_setup.py --tokens-only      # only resolve tokens, skip data fetch
    python swing_setup.py --stats            # print current DB stats and exit

Run this once before starting swing_agent.py for the first time.
Re-run with --tokens-only after editing config/swing_universe.json.
"""

import argparse
import logging
import sys
from datetime import date, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("swing_setup")


def main():
    parser = argparse.ArgumentParser(description="Swing trading universe setup")
    parser.add_argument("--seed-days",   type=int, default=365,
                        help="Days of EOD history to seed (default: 365)")
    parser.add_argument("--tokens-only", action="store_true",
                        help="Only resolve/refresh tokens, skip data seed")
    parser.add_argument("--stats",       action="store_true",
                        help="Print DB stats and exit")
    args = parser.parse_args()

    # Late import so path setup (sys.path) is already done
    from core.swing_eod_store import (
        ensure_tables,
        load_universe,
        resolve_and_cache_tokens,
        fetch_and_store_eod,
        get_universe_stats,
        get_tokens,
    )
    from agents.swing_agent import ensure_swing_tables

    # ── Stats-only mode ───────────────────────────────────────────────────────
    if args.stats:
        ensure_tables()
        ensure_swing_tables()
        stats = get_universe_stats()
        print("\n=== Swing EOD Store Stats ===")
        for k, v in stats.items():
            print(f"  {k:<20}: {v}")
        tokens = get_tokens()
        print(f"  {'tokens_cached':<20}: {len(tokens)}")
        print()
        return

    # ── Normal setup ──────────────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print("  SWING TRADING UNIVERSE SETUP")
    print("=" * 55)

    # 1. Create tables
    ensure_tables()
    ensure_swing_tables()
    logger.info("Tables ready")

    # 2. Resolve tokens
    symbols = load_universe()
    logger.info(f"Universe: {len(symbols)} symbols in config/swing_universe.json")

    token_map = resolve_and_cache_tokens(symbols)
    if not token_map:
        logger.error(
            "\nNo tokens resolved — Kite token likely expired.\n"
            "Refresh your access_token in:\n"
            r"  C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\config\kite_config.json"
            "\nThen re-run: python swing_setup.py --tokens-only"
        )
        sys.exit(1)

    resolved_pct = len(token_map) / len(symbols) * 100
    logger.info(f"Tokens resolved: {len(token_map)}/{len(symbols)} ({resolved_pct:.0f}%)")

    if args.tokens_only:
        logger.info("--tokens-only: skipping data seed")
        return

    # 3. Seed EOD history
    today     = date.today()
    from_date = (today - timedelta(days=args.seed_days)).isoformat()
    to_date   = today.isoformat()

    print()
    logger.info(f"Seeding EOD history: {from_date} → {to_date} ({args.seed_days} days)")
    logger.info(f"Fetching {len(token_map)} symbols — approx {len(token_map) * 0.12 / 60:.1f} min")
    logger.info("(Kite rate-limited to ~8 calls/sec; grab a coffee...)")
    print()

    n = fetch_and_store_eod(list(token_map.keys()), from_date=from_date, to_date=to_date)

    # 4. Summary
    stats = get_universe_stats()
    print()
    print("=" * 55)
    print("  SETUP COMPLETE")
    print("=" * 55)
    print(f"  Symbols with data : {stats['total_symbols']}")
    print(f"  Total EOD rows    : {stats['total_rows']:,}")
    print(f"  Date range        : {stats['oldest_date']}  to  {stats['latest_date']}")
    print()
    print("  Next steps:")
    print("    1. Add 'swing_agent.py' to start_trading_system.py PROCESSES")
    print("    OR run standalone: python agents/swing_agent.py")
    print("    2. Dashboard swing tab: http://127.0.0.1:5050 → Swing")
    print()


if __name__ == "__main__":
    main()
