"""
audit_v2_runtime.py
-------------------
Phase 4 — one-shot diagnostic for DhanOmniEngine v2.

Reads:
  • The most-recent v2 log file for the date            (engine alive? meta stats?)
  • strategy_signals table for the date                  (per-strategy fire counts)
  • orders table for the date filtered by strategy_name  (what reached execution)

Produces a 1-page markdown audit at:
  MasterConfiguration/reports/v2_audit_<DATE>.md

Usage:
    python audit_v2_runtime.py                # today
    python audit_v2_runtime.py 2026-04-24     # specific date
"""

import io
import os
import re
import sqlite3
import sys
from datetime import datetime, date
from glob import glob
from pathlib import Path

import pytz

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")

from master_resource import MasterResource

IST        = pytz.timezone("Asia/Kolkata")
LOG_DIR    = Path(MasterResource.MASTER_ROOT) / "logs"
REPORT_DIR = Path(MasterResource.MASTER_ROOT) / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# All strategies registered in DhanOmniEngine v2
_STRATEGIES = (
    "EMA_9_21", "OptionScalper_EMA44", "Supertrend_MACD",
    "EMA_VWAP_SR", "ORB_VWAP", "TriplePattern",
    "IndexMomentum", "BB_MeanReversion",
    "VWAPReclaim", "CPRBreakout", "PairLeadership",
)


# ── Log parsing ──────────────────────────────────────────────────────────────

def _find_v2_log(d: date) -> Path | None:
    """Return the most-recent v2 log file written on day `d`."""
    pattern = str(LOG_DIR / f"dhan_omni_engine_v2_{d.strftime('%d%b%Y').upper()}*.log")
    files = sorted(glob(pattern, recursive=False))
    if files:
        return Path(files[-1])
    # Fallback: case-insensitive month
    pattern = str(LOG_DIR / f"dhan_omni_engine_v2_{d.strftime('%d%b%Y')}*.log")
    files = sorted(glob(pattern, recursive=False))
    return Path(files[-1]) if files else None


def _parse_v2_log(log_path: Path) -> dict:
    """Extract liveness + MetaAgent stats from a v2 log file."""
    stats = {
        "log_file":     str(log_path),
        "log_age_sec":  None,
        "alive":        False,
        "last_line":    None,
        "meta_mode":    "UNKNOWN",
        "meta_total":   0,
        "meta_executed": 0,
        "meta_skipped": 0,
        "meta_filter":  0.0,
        "health_count": 0,
        "errors":       0,
    }
    try:
        stats["log_age_sec"] = int(datetime.now().timestamp() - os.path.getmtime(log_path))
        stats["alive"] = stats["log_age_sec"] < 600
    except Exception:
        return stats

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
    except Exception:
        return stats

    if not lines:
        return stats

    stats["last_line"] = lines[-1].split(" - ", 3)[-1] if " - " in lines[-1] else lines[-1]
    stats["health_count"] = sum(1 for l in lines if "Health |" in l)
    stats["errors"]       = sum(1 for l in lines if " - ERROR - " in l)

    # MetaAgent mode (from startup line)
    for line in lines:
        if "MetaAgent mode:" in line:
            stats["meta_mode"] = "RAG+LLM" if "RAG+LLM" in line else "PASS-THROUGH"
            break

    # Last Health line → MetaAgent counters
    for line in reversed(lines):
        if "MetaAgent:" in line and "total=" in line:
            m = re.search(r"total=(\d+)\s+executed=(\d+)\s+skipped=(\d+)\s+\(([0-9.]+)%", line)
            if m:
                stats["meta_total"]    = int(m.group(1))
                stats["meta_executed"] = int(m.group(2))
                stats["meta_skipped"]  = int(m.group(3))
                stats["meta_filter"]   = float(m.group(4))
            break

    return stats


# ── DB queries ───────────────────────────────────────────────────────────────

def _signal_funnel(d: date) -> list[dict]:
    """Per-strategy fire counts from strategy_signals on date d."""
    conn = sqlite3.connect(MasterResource.get_trading_db_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT strategy,
               SUM(CASE WHEN signal='BULLISH'  THEN 1 ELSE 0 END) AS bullish,
               SUM(CASE WHEN signal='BEARISH'  THEN 1 ELSE 0 END) AS bearish,
               SUM(CASE WHEN signal='NEUTRAL'  THEN 1 ELSE 0 END) AS neutral,
               COUNT(*) AS total
          FROM strategy_signals
         WHERE DATE(ts) = ?
      GROUP BY strategy
        """,
        (d.isoformat(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _orders_for_date(d: date) -> list[dict]:
    """Per-strategy_name order counts on date d."""
    conn = sqlite3.connect(MasterResource.get_trading_db_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT COALESCE(strategy_name, '(none)') AS strategy_name,
               COUNT(*) AS n,
               SUM(CASE WHEN status='OPEN'   THEN 1 ELSE 0 END) AS open_n,
               SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed_n
          FROM orders
         WHERE DATE(created_at) = ?
      GROUP BY strategy_name
        """,
        (d.isoformat(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Report ───────────────────────────────────────────────────────────────────

def _build_report(d: date, log_stats: dict, signals: list[dict], orders: list[dict]) -> str:
    sig_by   = {r["strategy"]: r for r in signals}
    ord_by   = {r["strategy_name"]: r for r in orders}
    total_signals_fired = sum(r["bullish"] + r["bearish"] for r in signals)

    lines = [
        f"# DhanOmniEngine v2 Runtime Audit — {d.isoformat()}",
        "",
        f"_Generated {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S %Z')}_",
        "",
        "## 1. Engine Liveness",
        "",
    ]
    if log_stats["log_file"] is None:
        lines.append(f"❌ **No v2 log file found for {d}.** v2 was not started that day.")
    else:
        age = log_stats["log_age_sec"]
        age_human = f"{age}s" if age < 600 else f"{age//60}m"
        lines += [
            f"- Log: `{log_stats['log_file']}`",
            f"- Last write: **{age_human} ago** → {'🟢 ALIVE' if log_stats['alive'] else '🔴 STALE (>10 min)'}",
            f"- Last line: `{log_stats['last_line'] or '(empty)'}`",
            f"- Health-line count: **{log_stats['health_count']}**  ·  ERROR lines: **{log_stats['errors']}**",
        ]
    lines += [
        "",
        "## 2. MetaAgent",
        "",
        f"- Mode: **{log_stats['meta_mode']}**",
        f"- Total reviewed: **{log_stats['meta_total']}**",
        f"- Executed: **{log_stats['meta_executed']}**",
        f"- Skipped: **{log_stats['meta_skipped']}**",
        f"- Filter rate: **{log_stats['meta_filter']:.1f}%**",
        "",
        "## 3. Strategy Signal Funnel",
        "",
        f"_Total non-NEUTRAL evaluations across all strategies: **{total_signals_fired}**_",
        "",
        "| Strategy | Bullish | Bearish | Neutral | Fired (B+B) | Orders Placed | Funnel |",
        "|---|---:|---:|---:|---:|---:|:---:|",
    ]
    for sid in _STRATEGIES:
        s = sig_by.get(sid)
        o = ord_by.get(sid)
        b   = s["bullish"] if s else 0
        br  = s["bearish"] if s else 0
        nu  = s["neutral"] if s else 0
        fired = b + br
        n_ord = o["n"] if o else 0
        if fired == 0 and n_ord == 0:
            funnel = "—"
        elif fired > 0 and n_ord == 0:
            funnel = "🚨 fires but no orders"
        elif n_ord > 0 and fired == 0:
            funnel = "⚠ orders but no signals logged"
        else:
            ratio = n_ord / fired
            funnel = f"{ratio*100:.2f}% throughput"
        lines.append(
            f"| {sid} | {b} | {br} | {nu} | **{fired}** | {n_ord} | {funnel} |"
        )
    lines += [
        "",
        "## 4. Orders by Source (all `strategy_name` values)",
        "",
        "| strategy_name | Total | OPEN | CLOSED |",
        "|---|---:|---:|---:|",
    ]
    if not orders:
        lines.append("| _(no orders this date)_ | — | — | — |")
    else:
        for r in sorted(orders, key=lambda x: -x["n"]):
            lines.append(
                f"| {r['strategy_name']} | {r['n']} | {r['open_n']} | {r['closed_n']} |"
            )
    lines += [
        "",
        "## 5. Diagnostics",
        "",
    ]

    diags = []

    # No engine ran
    if log_stats["log_file"] is None:
        diags.append("- 🚨 **v2 was not started.** No log file present for this date — every other metric should be read against this fact.")
    elif not log_stats["alive"]:
        diags.append("- 🚨 **v2 log stale.** Last write > 10 min ago. Engine likely crashed or stopped before EOD.")

    # No OmniEngine orders despite signals
    omni_orders = sum(o["n"] for o in orders if o["strategy_name"] in _STRATEGIES)
    if total_signals_fired > 0 and omni_orders == 0:
        diags.append(
            f"- 🚨 **{total_signals_fired:,} non-NEUTRAL signals fired, but ZERO orders placed by any OmniEngine strategy.** "
            "Funnel is broken — check the path strategy_worker → signal_queue → MetaAgent → ExecutionAgent."
        )

    # MetaAgent never reviewed anything
    if log_stats["log_file"] and total_signals_fired > 100 and log_stats["meta_total"] == 0:
        diags.append(
            "- 🚨 **MetaAgent has total=0 reviewed despite many strategy signals.** "
            "Either signal_queue isn't being consumed, or the Health-line parser missed the latest count."
        )

    # OptionScalper firing wildly
    os_sig = sig_by.get("OptionScalper_EMA44")
    if os_sig and (os_sig["bullish"] + os_sig["bearish"]) > 5000:
        diags.append(
            f"- ⚠ **OptionScalper_EMA44 fired {os_sig['bullish']+os_sig['bearish']:,} non-NEUTRAL signals.** "
            "Per-index dedup is supposed to suppress repeats. Verify `_last_signal` is updated in `option_scalper.py`."
        )

    # Strategies that never fired at all
    silent = [s for s in _STRATEGIES if s not in sig_by]
    if silent and log_stats["log_file"]:
        diags.append(
            f"- ℹ️ **Silent strategies ({len(silent)}/{len(_STRATEGIES)}):** {', '.join(silent)}. "
            "Could be regime-gated, candle-warmup-gated, or genuinely broken — cross-check against expected gates."
        )

    if not diags:
        diags.append("- ✅ No critical anomalies detected.")

    lines += diags + [""]
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        d = date.fromisoformat(sys.argv[1])
    else:
        d = datetime.now(IST).date()

    print(f"[AUDIT] Auditing v2 runtime for {d} ...")

    log_path  = _find_v2_log(d)
    log_stats = _parse_v2_log(log_path) if log_path else {
        "log_file": None, "log_age_sec": None, "alive": False, "last_line": None,
        "meta_mode": "UNKNOWN", "meta_total": 0, "meta_executed": 0,
        "meta_skipped": 0, "meta_filter": 0.0, "health_count": 0, "errors": 0,
    }
    signals = _signal_funnel(d)
    orders  = _orders_for_date(d)
    report  = _build_report(d, log_stats, signals, orders)

    out_path = REPORT_DIR / f"v2_audit_{d}.md"
    out_path.write_text(report, encoding="utf-8")

    # Console summary
    print(f"[AUDIT] Report: {out_path}")
    print()
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
