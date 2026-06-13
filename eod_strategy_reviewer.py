"""
eod_strategy_reviewer.py
-------------------------
End-of-Day Strategy Reviewer — honest daily health check before going live.

Reads whatif_trades from MySQL, computes per-strategy metrics, flags issues,
and writes a markdown report with a LIVE READINESS score per strategy.

Goal: Identify every leak in the system over the next 2 weeks so we can
      deploy real money with confidence.

Usage:
    python eod_strategy_reviewer.py              # today
    python eod_strategy_reviewer.py 2026-05-29   # specific past date
    python eod_strategy_reviewer.py --rolling 10 # 10-day aggregate view

Output:
    MasterConfiguration/reports/strategy_review_YYYY-MM-DD.md

Scheduled via Task Scheduler (DhanEODReview) at 4:15 PM weekdays.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# ── path setup ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "MasterConfiguration" / "lib"))

import mysql_sqlite_bridge   # noqa — patches sqlite3 transparently

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("eod_reviewer")

REPORTS_DIR = _HERE.parent / "MasterConfiguration" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Thresholds (what "healthy" looks like) ──────────────────────────────────────
WR_GOOD          = 52.0   # % — target win rate for live deployment
WR_WARN          = 42.0   # % — below this = watch closely
WR_DISABLE       = 35.0   # % — below this = disable candidate
RR_GOOD          = 1.40   # realized risk:reward
RR_WARN          = 1.00   # below this = SL tighter than target
MIN_SAMPLE_LIVE  = 25     # minimum trades before granting live readiness
MAX_CONSEC_LOSS  = 4      # max acceptable consecutive losses
CUTOFF_WARN_PCT  = 40.0   # % of exits being TIME_SL/CUTOFF = SL placement issue
EV_THRESHOLD     = 0      # expected value must be positive


# ═══════════════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════════════

def _conn():
    return sqlite3.connect("trading.db", timeout=15)


def load_trades(from_date: str, to_date: str) -> list[dict]:
    """Load whatif_trades for a date range. Returns list of dicts."""
    conn = _conn()
    cur = conn.execute("""
        SELECT run_date, channel_name, source, result,
               entry_price, sl_initial, exit_price, exit_reason,
               pnl_per_unit, pnl_pct, pnl_total, lot_size,
               symbol, tradingsymbol, action
        FROM   whatif_trades
        WHERE  run_date >= %s AND run_date <= %s
        ORDER  BY run_date, channel_name
    """, (from_date, to_date))
    rows = cur.fetchall()
    conn.close()
    cols = ["run_date","channel_name","source","result","entry_price","sl_initial",
            "exit_price","exit_reason","pnl_per_unit","pnl_pct","pnl_total","lot_size",
            "symbol","tradingsymbol","action"]
    return [dict(zip(cols, r)) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════════
# Per-strategy metrics
# ═══════════════════════════════════════════════════════════════════════════════

def _is_algo(channel_name: str) -> bool:
    """True if this is an OmniEngine algo strategy, not a TG channel."""
    algo_prefixes = [
        "EMA_", "ORB_", "VWAP", "BB_", "CPR", "Triple", "Index", "Ichimoku",
        "SMC", "Fib", "Stoch", "MACD", "Elliot", "Harmonic", "Candle",
        "Donchian", "Multi", "Power", "Scaler", "CEP_", "FlagBreakout",
        "TriangleBreakout", "HSPattern", "Supertrend", "PairLeadership",
        "OptionScalper",
    ]
    return any(channel_name.startswith(p) for p in algo_prefixes)


def compute_strategy_metrics(trades: list[dict]) -> dict:
    """
    Returns {strategy_name: metrics_dict} for all strategies in the trade list.
    """
    by_strat = defaultdict(list)
    for t in trades:
        by_strat[t["channel_name"]].append(t)

    metrics = {}
    for strat, rows in by_strat.items():
        executed  = [r for r in rows if r["source"] == "EXECUTED"]
        filtered  = [r for r in rows if r["source"] == "FILTERED"]
        sim_only  = [r for r in rows if not r["source"]]

        all_decided = executed + filtered
        total    = len(executed)
        if total == 0:
            continue

        wins   = [r for r in executed if r["result"] == "PROFIT"]
        losses = [r for r in executed if r["result"] == "LOSS"]
        be     = [r for r in executed if r["result"] not in ("PROFIT","LOSS")]

        wr = len(wins) / total * 100 if total else 0

        avg_win  = sum(r["pnl_total"] or 0 for r in wins)   / len(wins)   if wins   else 0
        avg_loss = sum(abs(r["pnl_total"] or 0) for r in losses) / len(losses) if losses else 0
        rr       = avg_win / avg_loss if avg_loss > 0 else 0

        total_pnl     = sum(r["pnl_total"] or 0 for r in executed)
        total_capital = sum((r["entry_price"] or 0) * (r["lot_size"] or 0)
                            for r in executed)

        ev = (wr/100) * avg_win - (1 - wr/100) * avg_loss

        # Exit quality: % of exits that were CUTOFF / TIME_SL (bad — means trade
        # lingered all day without hitting target or SL)
        bad_exits = sum(1 for r in executed
                        if (r["exit_reason"] or "").upper() in ("CUTOFF","TIME_SL","FORCE_CLOSE"))
        bad_exit_pct = bad_exits / total * 100 if total else 0

        # Max consecutive losses
        max_consec = _max_consecutive(executed, "LOSS")

        # Day-by-day P&L for trend detection
        by_day = defaultdict(float)
        for r in executed:
            by_day[r["run_date"]] += (r["pnl_total"] or 0)
        daily_pnl   = [v for _, v in sorted(by_day.items())]
        pnl_trend   = _trend_direction(daily_pnl)

        # MetaAgent filter rate
        meta_filter_pct = (len(filtered) / len(all_decided) * 100
                           if all_decided else 0)

        metrics[strat] = {
            "strategy":         strat,
            "is_algo":          _is_algo(strat),
            "total":            total,
            "wins":             len(wins),
            "losses":           len(losses),
            "breakeven":        len(be),
            "win_rate":         round(wr, 1),
            "avg_win":          round(avg_win, 0),
            "avg_loss":         round(avg_loss, 0),
            "rr":               round(rr, 2),
            "ev":               round(ev, 0),
            "total_pnl":        round(total_pnl, 0),
            "total_capital":    round(total_capital, 0),
            "capital_eff":      round(total_pnl / total_capital * 100, 2)
                                if total_capital > 0 else 0,
            "bad_exit_pct":     round(bad_exit_pct, 1),
            "max_consec_loss":  max_consec,
            "pnl_trend":        pnl_trend,
            "meta_filter_pct":  round(meta_filter_pct, 1),
            "filtered_count":   len(filtered),
            "sim_only_count":   len(sim_only),
            "daily_pnl":        dict(sorted(by_day.items())),
        }

    return metrics


def _max_consecutive(trades: list[dict], result: str) -> int:
    mx = cur = 0
    for t in trades:
        if t["result"] == result:
            cur += 1
            mx = max(mx, cur)
        else:
            cur = 0
    return mx


def _trend_direction(daily_pnl: list) -> str:
    if len(daily_pnl) < 3:
        return "INSUFFICIENT DATA"
    recent_half = daily_pnl[len(daily_pnl)//2:]
    early_half  = daily_pnl[:len(daily_pnl)//2]
    avg_r = sum(recent_half) / len(recent_half)
    avg_e = sum(early_half)  / len(early_half)
    if avg_r > avg_e * 1.1:   return "IMPROVING"
    if avg_r < avg_e * 0.9:   return "DECLINING"
    return "STABLE"


# ═══════════════════════════════════════════════════════════════════════════════
# Live readiness scoring  (0 – 10 per strategy)
# ═══════════════════════════════════════════════════════════════════════════════

def readiness_score(m: dict) -> tuple[float, str, list[str]]:
    """
    Returns (score_0_to_10, grade, [issues]).
    Honest — fails fast on any critical threshold breach.
    """
    score  = 0.0
    issues = []
    flags  = []

    total = m["total"]
    wr    = m["win_rate"]
    rr    = m["rr"]
    ev    = m["ev"]

    # ── Sample size gate (prerequisite) ──────────────────────────────────────
    if total < 10:
        return 0.0, "INSUFFICIENT DATA", [f"Only {total} executed trades — need ≥25 for live"]

    if total < MIN_SAMPLE_LIVE:
        score += 1.0
        flags.append(f"Sample size {total} < {MIN_SAMPLE_LIVE} — keep paper trading")
    else:
        score += 2.0

    # ── Expected value (must be positive) ────────────────────────────────────
    if ev <= EV_THRESHOLD:
        issues.append(f"Expected value = Rs{ev:,.0f} (NEGATIVE) — strategy loses money on expectation")
        score -= 3.0
    else:
        score += 2.0

    # ── Win rate ──────────────────────────────────────────────────────────────
    if wr >= WR_GOOD:
        score += 2.5
    elif wr >= WR_WARN:
        score += 1.5
        flags.append(f"Win rate {wr}% is acceptable but below target {WR_GOOD}%")
    elif wr >= WR_DISABLE:
        score += 0.5
        issues.append(f"Win rate {wr}% below warning threshold {WR_WARN}%")
    else:
        issues.append(f"Win rate {wr}% is CRITICAL — below disable threshold {WR_DISABLE}%")

    # ── Risk:Reward ───────────────────────────────────────────────────────────
    if rr >= RR_GOOD:
        score += 2.0
    elif rr >= RR_WARN:
        score += 1.0
        flags.append(f"R:R {rr} is borderline — target Rs{RR_GOOD}")
    else:
        issues.append(f"R:R {rr} < 1.0 — losing more on losses than winning on wins")

    # ── Consecutive losses ────────────────────────────────────────────────────
    if m["max_consec_loss"] <= 2:
        score += 1.0
    elif m["max_consec_loss"] <= MAX_CONSEC_LOSS:
        score += 0.5
        flags.append(f"Max consecutive losses = {m['max_consec_loss']} — watch closely")
    else:
        issues.append(f"Max consecutive losses = {m['max_consec_loss']} (> {MAX_CONSEC_LOSS}) — drawdown risk")

    # ── Exit quality ──────────────────────────────────────────────────────────
    if m["bad_exit_pct"] > CUTOFF_WARN_PCT:
        issues.append(
            f"{m['bad_exit_pct']}% of exits are CUTOFF/TIME_SL — "
            "trades not reaching target or SL before close; entry timing or SL levels need work"
        )
    else:
        score += 0.5

    # ── Trend ─────────────────────────────────────────────────────────────────
    if m["pnl_trend"] == "IMPROVING":
        score += 0.5
    elif m["pnl_trend"] == "DECLINING":
        flags.append("P&L trend is DECLINING over the review period")

    score = max(0.0, min(10.0, round(score, 1)))

    if issues:
        grade = "NOT READY"
    elif score >= 7.5:
        grade = "READY FOR LIVE"
    elif score >= 5.5:
        grade = "APPROACHING LIVE"
    else:
        grade = "NEEDS WORK"

    return score, grade, issues + flags


# ═══════════════════════════════════════════════════════════════════════════════
# Report generation
# ═══════════════════════════════════════════════════════════════════════════════

def generate_report(
    review_date: str,
    metrics: dict,
    from_date: str,
    to_date: str,
) -> str:
    total_trades = sum(m["total"] for m in metrics.values())
    total_pnl    = sum(m["total_pnl"] for m in metrics.values())
    total_cap    = sum(m["total_capital"] for m in metrics.values())
    total_wins   = sum(m["wins"] for m in metrics.values())
    overall_wr   = round(total_wins / total_trades * 100, 1) if total_trades else 0

    algo_metrics = {k: v for k, v in metrics.items() if v["is_algo"]}
    tg_metrics   = {k: v for k, v in metrics.items() if not v["is_algo"]}

    lines = []
    lines += [
        f"# Strategy Review — {review_date}",
        f"> Period: {from_date} to {to_date}  |  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}",
        "",
        "---",
        "",
        "## Executive Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total Executed Trades | {total_trades} |",
        f"| Overall Win Rate | {overall_wr}% |",
        f"| Total P&L | Rs{total_pnl:,.0f} |",
        f"| Capital Deployed | Rs{total_cap:,.0f} |",
        f"| Capital Efficiency | {round(total_pnl/total_cap*100,2) if total_cap else 0}% |",
        f"| Strategies Reviewed | {len(metrics)} |",
        "",
        "---",
        "",
        "## Algo Strategy Health",
        "",
        "| Strategy | Trades | WR% | R:R | EV (Rs) | Trend | Score | Grade |",
        "|----------|--------|-----|-----|---------|-------|-------|-------|",
    ]

    # Sort algo strategies by score
    scored_algo = []
    for name, m in algo_metrics.items():
        score, grade, _ = readiness_score(m)
        scored_algo.append((score, name, m, grade))
    scored_algo.sort(key=lambda x: -x[0])

    for score, name, m, grade in scored_algo:
        trend_icon = {"IMPROVING": "UP", "DECLINING": "DN", "STABLE": "--"}.get(m["pnl_trend"], "?")
        lines.append(
            f"| {name} | {m['total']} | {m['win_rate']}% | {m['rr']} "
            f"| {m['ev']:,.0f} | {trend_icon} {m['pnl_trend']} | {score}/10 | {grade} |"
        )

    lines += ["", "---", "", "## Detailed Strategy Analysis", ""]

    for score, name, m, grade in scored_algo:
        _, _, all_issues = readiness_score(m)
        critical = [i for i in all_issues if not i.startswith(f"Win rate") or float(i.split("%")[0].split()[-1]) < WR_WARN]
        warnings = [i for i in all_issues if i not in critical]

        status_icon = {
            "READY FOR LIVE":    "GREEN",
            "APPROACHING LIVE":  "YELLOW",
            "NEEDS WORK":        "ORANGE",
            "NOT READY":         "RED",
            "INSUFFICIENT DATA": "GREY",
        }.get(grade, "GREY")

        lines += [
            f"### [{status_icon}] {name}  —  {score}/10  ({grade})",
            "",
            f"- **Trades:** {m['total']} executed  |  {m['filtered_count']} filtered by MetaAgent  |  {m['sim_only_count']} sim-only",
            f"- **Win Rate:** {m['win_rate']}%  ({m['wins']}W / {m['losses']}L / {m['breakeven']}BE)",
            f"- **Avg Win:** Rs{m['avg_win']:,.0f}  |  **Avg Loss:** Rs{m['avg_loss']:,.0f}",
            f"- **Realized R:R:** {m['rr']}  |  **Expected Value/trade:** Rs{m['ev']:,.0f}",
            f"- **Total P&L:** Rs{m['total_pnl']:,.0f}  |  **Capital Deployed:** Rs{m['total_capital']:,.0f}",
            f"- **Capital Efficiency:** {m['capital_eff']}%",
            f"- **Max Consecutive Losses:** {m['max_consec_loss']}",
            f"- **Bad Exits (CUTOFF/TIME_SL):** {m['bad_exit_pct']}% of exits",
            f"- **P&L Trend:** {m['pnl_trend']}",
            f"- **MetaAgent Filter Rate:** {m['meta_filter_pct']}%",
            "",
        ]

        if critical:
            lines.append("**CRITICAL ISSUES:**")
            for issue in critical:
                lines.append(f"- CRITICAL: {issue}")
            lines.append("")

        if warnings:
            lines.append("**Warnings:**")
            for w in warnings:
                lines.append(f"- {w}")
            lines.append("")

        # Day-by-day P&L
        if m["daily_pnl"]:
            lines.append("**Daily P&L:**")
            for d, pnl in sorted(m["daily_pnl"].items()):
                bar = "+" if pnl >= 0 else ""
                lines.append(f"  - {d}: Rs{bar}{pnl:,.0f}")
            lines.append("")

        lines.append("---")
        lines.append("")

    # ── TG channel summary (brief) ─────────────────────────────────────────────
    if tg_metrics:
        lines += [
            "## TG Channel Summary",
            "",
            "| Channel | Trades | WR% | R:R | Total P&L |",
            "|---------|--------|-----|-----|-----------|",
        ]
        for name, m in sorted(tg_metrics.items(), key=lambda x: -x[1]["total"]):
            lines.append(
                f"| {name[:35]} | {m['total']} | {m['win_rate']}% "
                f"| {m['rr']} | Rs{m['total_pnl']:,.0f} |"
            )
        lines += ["", "---", ""]

    # ── System-level live readiness verdict ───────────────────────────────────
    algo_scores = [readiness_score(m)[0] for m in algo_metrics.values() if m["total"] >= 10]
    system_score = round(sum(algo_scores) / len(algo_scores), 1) if algo_scores else 0

    ready_count  = sum(1 for m in algo_metrics.values()
                       if m["total"] >= 10 and readiness_score(m)[1] == "READY FOR LIVE")
    total_algo   = sum(1 for m in algo_metrics.values() if m["total"] >= 10)
    not_ready    = [n for n, m in algo_metrics.items()
                    if m["total"] >= 10 and readiness_score(m)[1] in ("NOT READY", "NEEDS WORK")]
    disable_cands = [n for n, m in algo_metrics.items()
                     if m["win_rate"] < WR_DISABLE and m["total"] >= 10]

    if system_score >= 7.5:
        verdict = "SYSTEM IS APPROACHING LIVE READINESS"
    elif system_score >= 5.5:
        verdict = "SYSTEM NEEDS TARGETED FIXES BEFORE LIVE"
    else:
        verdict = "SYSTEM NOT READY FOR LIVE MONEY"

    lines += [
        "## LIVE READINESS VERDICT",
        "",
        f"### System Score: {system_score}/10 — {verdict}",
        "",
        f"- Strategies scoring READY: {ready_count}/{total_algo}",
        f"- Strategies needing work: {len(not_ready)}",
        "",
    ]

    if disable_cands:
        lines += [
            "### DISABLE CANDIDATES (WR < 35%)",
            "",
        ]
        for n in disable_cands:
            m = algo_metrics[n]
            lines.append(f"- **{n}**: {m['win_rate']}% WR, R:R {m['rr']}, EV Rs{m['ev']:,.0f}/trade — STOP TRADING LIVE")
        lines.append("")

    if not_ready:
        lines += [
            "### Strategies Requiring Fixes Before Live",
            "",
        ]
        for n in not_ready:
            m      = algo_metrics.get(n, {})
            score2, grade2, issues2 = readiness_score(m)
            crit = [i for i in issues2 if "CRITICAL" in i or "NEGATIVE" in i or "CRITICAL" in i]
            for c in crit[:2]:
                lines.append(f"- **{n}**: {c}")
        lines.append("")

    lines += [
        "### Top Strengths to Preserve",
        "",
    ]
    top = sorted(scored_algo, key=lambda x: -x[0])[:3]
    for score_v, name, m, grade in top:
        lines.append(f"- **{name}**: {score_v}/10  WR={m['win_rate']}%  R:R={m['rr']}  EV=Rs{m['ev']:,.0f}/trade")

    lines += [
        "",
        "---",
        f"*Report generated by eod_strategy_reviewer.py — {datetime.now().isoformat(timespec='seconds')}*",
        "",
    ]

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduler registration
# ═══════════════════════════════════════════════════════════════════════════════

def register_scheduler():
    """Register Windows Task Scheduler job for daily 4:15 PM run."""
    import subprocess
    py_exe = sys.executable
    script = str(Path(__file__).resolve())
    cmd = (
        f'schtasks /Create /TN "DhanEODReview" '
        f'/TR "\\"{py_exe}\\" \\"{script}\\"" '
        f'/SC WEEKLY /D MON,TUE,WED,THU,FRI '
        f'/ST 16:15 /F'
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        print("Task Scheduler: DhanEODReview registered — runs at 16:15 weekdays")
    else:
        print(f"Scheduler registration failed: {result.stderr}")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="EOD Strategy Reviewer")
    parser.add_argument("review_date", nargs="?", default=None,
                        help="Date to review YYYY-MM-DD (default: today)")
    parser.add_argument("--rolling", type=int, default=1,
                        help="Rolling N-day window (default: 1 = today only)")
    parser.add_argument("--schedule", action="store_true",
                        help="Register Windows Task Scheduler job and exit")
    args = parser.parse_args()

    if args.schedule:
        register_scheduler()
        return

    review_date = args.review_date or str(date.today())
    end_dt      = datetime.strptime(review_date, "%Y-%m-%d").date()
    start_dt    = end_dt - timedelta(days=args.rolling - 1)
    from_date   = str(start_dt)
    to_date     = str(end_dt)

    print()
    print("=" * 65)
    print(f"  EOD Strategy Review — {review_date}")
    print(f"  Period: {from_date} to {to_date}")
    print("=" * 65)
    print()

    logger.info("Loading trades %s → %s", from_date, to_date)
    trades = load_trades(from_date, to_date)
    if not trades:
        print(f"No whatif_trades found for {from_date} to {to_date}")
        print("Run eod_whatif_backtest.py first to populate trade data.")
        return

    logger.info("Loaded %d trade rows — computing metrics", len(trades))
    metrics = compute_strategy_metrics(trades)

    if not metrics:
        print("No executed trades found in this period.")
        return

    report = generate_report(review_date, metrics, from_date, to_date)
    report_path = REPORTS_DIR / f"strategy_review_{review_date}.md"
    report_path.write_text(report, encoding="utf-8")

    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(report)
    print()
    print(f"Report saved to: {report_path}")
    print()

    # Print a quick terminal summary
    algo_m = {k: v for k, v in metrics.items() if v["is_algo"]}
    print("Quick Terminal Summary:")
    print(f"{'Strategy':<28} {'Trades':>6} {'WR%':>6} {'R:R':>5} {'EV':>8}  Grade")
    print("-" * 70)
    scored = sorted(
        [(readiness_score(m)[0], readiness_score(m)[1], k, m) for k, m in algo_m.items()
         if m["total"] >= 5],
        key=lambda x: -x[0]
    )
    for sc, grade, name, m in scored:
        print(f"{name:<28} {m['total']:>6} {m['win_rate']:>5}% {m['rr']:>5}  {m['ev']:>7,.0f}  {sc}/10 {grade}")
    print()


if __name__ == "__main__":
    main()
