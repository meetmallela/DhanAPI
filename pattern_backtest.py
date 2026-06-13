"""
pattern_backtest.py
-------------------
Validates patterns discovered in the training window (months 1-6) against
out-of-sample test data (months 7-12, older historical data).

Requires:
  - hist_cache.db populated by historical_data_fetch.py
  - backtest/pattern_stats.json from pattern_discovery.py

Run:
    python pattern_backtest.py
    python pattern_backtest.py --symbol BANKNIFTY --train-months 6 --test-months 6

Output:
    MasterConfiguration/reports/pattern_backtest_<SYMBOL>_<DATE>.md
    Prints hold-out performance vs training performance for each pattern
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

_ROOT   = Path(__file__).parent
_MASTER = _ROOT.parent / "MasterConfiguration"
_LIB    = _MASTER / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from historical_data_fetch import load
from pattern_discovery import (
    add_indicators,
    find_expiry_blast, find_orb15, find_gap_fill,
    find_atr_squeeze, find_vwap_reclaim,
    summarise,
)

REPORT_DIR = _MASTER / "reports"
STATS_FILE = _ROOT / "backtest" / "pattern_stats.json"


# -- Comparison formatter ------------------------------------------------------

def compare_stats(train: dict, test: dict) -> str:
    """Build a one-line comparison string: train vs test."""
    def _arrow(t_val, te_val, higher_better=True):
        if higher_better:
            return "^" if te_val > t_val * 1.05 else ("v" if te_val < t_val * 0.95 else "~=")
        else:
            return "v" if te_val < t_val * 0.95 else ("^" if te_val > t_val * 1.05 else "~=")

    wr_arrow  = _arrow(train["win_rate"],  test["win_rate"])
    exp_arrow = _arrow(train["expectancy_pts"], test["expectancy_pts"])
    return (
        f"  WR: {train['win_rate']:5.1f}% -> {test['win_rate']:5.1f}% {wr_arrow}  |  "
        f"Exp: {train['expectancy_pts']:+5.1f} -> {test['expectancy_pts']:+5.1f} pts {exp_arrow}  |  "
        f"N: {train['count']} -> {test['count']}"
    )


def grade(test: dict) -> str:
    wr  = test["win_rate"]
    exp = test["expectancy_pts"]
    cnt = test["count"]
    if cnt < 5:
        return "INSUFFICIENT DATA"
    if exp > 10 and wr >= 55:
        return "STRONG EDGE"
    if exp > 5 and wr >= 50:
        return "EDGE"
    if exp > 0 and wr >= 45:
        return "MARGINAL"
    return "NO EDGE"


# -- Report writer -------------------------------------------------------------

def write_report(symbol: str, train_window: tuple[str, str],
                 test_window: tuple[str, str],
                 results: list[dict]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    path = REPORT_DIR / f"pattern_backtest_{symbol}_{today_str}.md"

    lines = [
        f"# Pattern Backtest -- {symbol}",
        f"**Training window:** {train_window[0]} -> {train_window[1]}",
        f"**Test window    :** {test_window[0]} -> {test_window[1]}",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M IST')}",
        "",
        "## Results",
        "",
        "| Pattern | Train WR | Test WR | Train Exp | Test Exp | Train N | Test N | Grade |",
        "|---------|----------|---------|-----------|----------|---------|--------|-------|",
    ]

    for r in results:
        tr = r["train"]
        te = r["test"]
        lines.append(
            f"| {r['pattern']:<14} | {tr['win_rate']:5.1f}% | {te['win_rate']:5.1f}% "
            f"| {tr['expectancy_pts']:+5.1f} | {te['expectancy_pts']:+5.1f} "
            f"| {tr['count']:>7} | {te['count']:>6} | {r['grade']} |"
        )

    lines += ["", "## Details", ""]

    for r in results:
        lines += [
            f"### {r['pattern']}  --  {r['grade']}",
            compare_stats(r["train"], r["test"]),
            "",
        ]
        te_df = r.get("test_trades")
        if te_df is not None and not te_df.empty:
            cols = [c for c in ["date", "time", "direction", "entry_price",
                                 "outcome_pts", "win"] if c in te_df.columns]
            # Day-of-week breakdown
            te_df = te_df.copy()
            te_df["dow"] = pd.to_datetime(te_df["date"]).dt.day_name()
            dow_wr = te_df.groupby("dow")["win"].agg(["mean", "count"]).round(2)
            if not dow_wr.empty:
                lines.append("**Day-of-week win rates (test window):**")
                lines.append("```")
                lines.append(dow_wr.to_string())
                lines.append("```")

            # Time-of-day breakdown
            if "time" in te_df.columns:
                te_df["hour"] = pd.to_datetime(te_df["time"], format="%H:%M:%S", errors="coerce").dt.hour
                hour_wr = te_df.groupby("hour")["win"].agg(["mean", "count"]).round(2)
                if not hour_wr.empty:
                    lines.append("**Hour-of-day win rates (test window):**")
                    lines.append("```")
                    lines.append(hour_wr.to_string())
                    lines.append("```")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# -- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Backtest patterns on out-of-sample data")
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--train-months", type=int, default=6,
                        help="Training window in months (most recent; default: 6)")
    parser.add_argument("--test-months", type=int, default=6,
                        help="Test window in months (older data; default: 6)")
    args = parser.parse_args()

    today = datetime.now()

    # Training: months 1-6 (recent)
    train_to   = today.strftime("%Y-%m-%d")
    train_from = (today - timedelta(days=args.train_months * 31)).strftime("%Y-%m-%d")

    # Test: months 7-12 (older, out-of-sample)
    test_to   = train_from
    test_from = (today - timedelta(days=(args.train_months + args.test_months) * 31)).strftime("%Y-%m-%d")

    print(f"=== Pattern Backtest: {args.symbol} ===")
    print(f"Training (pattern discovery): {train_from} -> {train_to}")
    print(f"Test     (out-of-sample)    : {test_from} -> {test_to}")
    print()

    # Load training data
    df_train = load(args.symbol, "5min", train_from, train_to)
    if df_train.empty:
        print("No training 5m data. Run historical_data_fetch.py first.")
        sys.exit(1)

    # Load test data
    df_test = load(args.symbol, "5min", test_from, test_to)
    if df_test.empty:
        print("No test 5m data. Run historical_data_fetch.py --months 12 first.")
        sys.exit(1)

    print(f"Training data: {len(df_train)} bars | Test data: {len(df_test)} bars")

    # Compute indicators on both windows
    print("Computing indicators...")
    df_train = add_indicators(df_train.sort_values("timestamp").reset_index(drop=True))
    df_test  = add_indicators(df_test.sort_values("timestamp").reset_index(drop=True))
    print("Done.\n")

    pattern_fns = [
        ("ExpiryBlast",  find_expiry_blast),
        ("ORB15",        find_orb15),
        ("GapFill",      find_gap_fill),
        ("ATRSqueeze",   find_atr_squeeze),
        ("VWAPReclaim",  find_vwap_reclaim),
    ]

    results = []
    print(f"{'Pattern':<15} {'Train WR':>8} {'Test WR':>8}  {'Train Exp':>10} {'Test Exp':>10}  Grade")
    print("-" * 70)

    for name, fn in pattern_fns:
        tr_trades = fn(df_train, args.symbol)
        te_trades = fn(df_test,  args.symbol)

        tr_stats = summarise(tr_trades)
        te_stats = summarise(te_trades)
        g        = grade(te_stats)

        indicator = "OK" if g in ("STRONG EDGE", "EDGE") else \
                    "~" if g == "MARGINAL" else "X"

        print(
            f"  {name:<13} {tr_stats['win_rate']:7.1f}%  {te_stats['win_rate']:7.1f}%  "
            f"{tr_stats['expectancy_pts']:+9.1f}  {te_stats['expectancy_pts']:+9.1f}  "
            f"{indicator} {g}"
        )

        results.append({
            "pattern":     name,
            "train":       tr_stats,
            "test":        te_stats,
            "grade":       g,
            "train_trades": tr_trades,
            "test_trades":  te_trades,
        })

    print()
    report_path = write_report(
        args.symbol,
        train_window=(train_from, train_to),
        test_window=(test_from, test_to),
        results=results,
    )
    print(f"Report saved: {report_path}")

    # Print top patterns worth deploying
    worthy = [r for r in results if r["grade"] in ("STRONG EDGE", "EDGE")]
    if worthy:
        print(f"\n=== Patterns Worth Deploying ({len(worthy)}) ===")
        for r in worthy:
            print(f"  OK {r['pattern']}: WR={r['test']['win_rate']:.1f}%  "
                  f"Exp={r['test']['expectancy_pts']:+.1f} pts  "
                  f"(test N={r['test']['count']})")
    else:
        print("\nNo patterns passed the out-of-sample edge test. Consider:")
        print("  - Fetching more data (--months 12 for training)")
        print("  - Relaxing pattern conditions")
        print("  - Checking if pattern count is sufficient (N < 10 = noisy)")


if __name__ == "__main__":
    main()
