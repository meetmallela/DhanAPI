"""
eod_report.py
-------------
End-of-Day Strategy P&L Report

Reads today's CLOSED orders from trading.db and prints a formatted
per-strategy summary table.  Also saves the report to the logs directory.

Usage:
    python eod_report.py            # today's report
    python eod_report.py 2026-04-12 # specific date
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from master_resource import MasterResource


def build_report(date_str: str) -> list[dict]:
    db_path = MasterResource.get_trading_db_path()
    conn    = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Closed trades for the day, grouped by strategy
    cur.execute("""
        SELECT
            COALESCE(strategy_name, 'Unknown')          AS strategy,
            COUNT(*)                                     AS trades,
            SUM(CASE WHEN pnl > 0  THEN 1 ELSE 0 END)  AS wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)  AS losses,
            ROUND(SUM(pnl),  2)                         AS total_pnl,
            ROUND(MAX(pnl),  2)                         AS best_trade,
            ROUND(MIN(pnl),  2)                         AS worst_trade
        FROM orders
        WHERE status = 'CLOSED'
          AND DATE(created_at) = ?
          AND COALESCE(exchange_segment, '') != 'MCX-OPT'
        GROUP BY strategy_name
        ORDER BY total_pnl DESC
    """, (date_str,))
    rows = cur.fetchall()

    # Open positions left at EOD per strategy
    cur.execute("""
        SELECT COALESCE(strategy_name, 'Unknown') AS strategy, COUNT(*) AS open
        FROM orders
        WHERE status = 'OPEN'
          AND DATE(created_at) = ?
          AND COALESCE(exchange_segment, '') != 'MCX-OPT'
        GROUP BY strategy_name
    """, (date_str,))
    open_map = {r["strategy"]: r["open"] for r in cur.fetchall()}
    conn.close()

    result = []
    for r in rows:
        strat    = r["strategy"]
        trades   = r["trades"]  or 0
        wins     = r["wins"]    or 0
        losses   = r["losses"]  or 0
        pnl      = r["total_pnl"] or 0.0
        best     = r["best_trade"]  or 0.0
        worst    = r["worst_trade"] or 0.0
        avg_pnl  = round(pnl / trades, 2) if trades else 0.0
        win_pct  = round(wins / trades * 100, 1) if trades else 0.0
        result.append({
            "strategy":  strat,
            "trades":    trades,
            "wins":      wins,
            "losses":    losses,
            "win_pct":   win_pct,
            "total_pnl": pnl,
            "avg_pnl":   avg_pnl,
            "best":      best,
            "worst":     worst,
            "open":      open_map.get(strat, 0),
        })

    # Append strategies with only open positions (no closed trades yet)
    for strat, open_count in open_map.items():
        if not any(r["strategy"] == strat for r in result):
            result.append({
                "strategy": strat, "trades": 0, "wins": 0, "losses": 0,
                "win_pct": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                "best": 0.0, "worst": 0.0, "open": open_count,
            })

    return result


def format_inr(n) -> str:
    if n is None:
        return "   —   "
    sign = "+" if n > 0 else ("-" if n < 0 else " ")
    return f"{sign}₹{abs(n):>10,.2f}"


def print_report(date_str: str, rows: list[dict]) -> str:
    lines = []
    lines.append("")
    lines.append("=" * 80)
    lines.append(f"  DHAN EOD STRATEGY P&L REPORT  —  {date_str}")
    lines.append("=" * 80)

    if not rows:
        lines.append("  No closed trades found for this date.")
        lines.append("=" * 80)
        report = "\n".join(lines)
        print(report)
        return report

    # Header
    col_w = 22
    lines.append(
        f"  {'Strategy':<{col_w}}  {'Trades':>6}  {'Wins':>5}  {'Loss':>5}"
        f"  {'Win%':>6}  {'Total P&L':>14}  {'Avg P&L':>13}  {'Best':>13}  {'Worst':>13}  {'Open':>4}"
    )
    lines.append("  " + "-" * 76)

    tot_trades = tot_wins = tot_losses = tot_open = 0
    tot_pnl = 0.0

    for r in rows:
        wp_str = f"{r['win_pct']:>5.1f}%"
        lines.append(
            f"  {r['strategy']:<{col_w}}  {r['trades']:>6}  {r['wins']:>5}  {r['losses']:>5}"
            f"  {wp_str}  {format_inr(r['total_pnl'])}  {format_inr(r['avg_pnl'])}"
            f"  {format_inr(r['best'])}  {format_inr(r['worst'])}  {r['open']:>4}"
        )
        tot_trades  += r["trades"]
        tot_wins    += r["wins"]
        tot_losses  += r["losses"]
        tot_pnl     += r["total_pnl"]
        tot_open    += r["open"]

    lines.append("  " + "-" * 76)
    tot_wp = f"{(tot_wins/tot_trades*100):.1f}%" if tot_trades else " 0.0%"
    avg_all = round(tot_pnl / tot_trades, 2) if tot_trades else 0.0
    lines.append(
        f"  {'TOTAL':<{col_w}}  {tot_trades:>6}  {tot_wins:>5}  {tot_losses:>5}"
        f"  {tot_wp:>6}  {format_inr(round(tot_pnl,2))}  {format_inr(avg_all)}"
        f"  {'':>13}  {'':>13}  {tot_open:>4}"
    )
    lines.append("=" * 80)

    if tot_open:
        lines.append(f"  ⚠  {tot_open} position(s) still OPEN — P&L above is for CLOSED trades only.")
        lines.append("=" * 80)

    lines.append("")
    report = "\n".join(lines)
    print(report)
    return report


def save_report(date_str: str, report_text: str) -> Path:
    log_dir  = MasterResource.MASTER_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    # Include time so scheduled 8 PM run never overwrites a manual afternoon run
    ts       = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = log_dir / f"eod_report_{ts}.txt"
    filename.write_text(report_text, encoding="utf-8")
    print(f"  Report saved → {filename}")
    return filename


if __name__ == "__main__":
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    rows     = build_report(date_str)
    report   = print_report(date_str, rows)
    save_report(date_str, report)
