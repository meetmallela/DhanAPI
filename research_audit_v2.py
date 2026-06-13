"""
research_audit_v2.py
--------------------
Weekly rolling audit for Research Engine v2 strategies.

Runs every Friday at 16:00 (or on demand):
  - Reads strategy_signals_v2 from MySQL for the past 7 days
  - Fetches actual candle outcomes from kite_candles.db
  - Computes live WR vs backtest WR per strategy per symbol
  - Flags if WR drifts >10% from backtest for 2 consecutive weeks
  - Saves weekly report to MasterConfiguration/reports/

Run on demand:
    python research_audit_v2.py
    python research_audit_v2.py --days 14      # look back 2 weeks
    python research_audit_v2.py --force-flag   # flag even if <2 consecutive weeks

Schedule via Task Scheduler (already set up pattern):
    schtasks /create /tn "ResearchAuditV2" /tr "python research_audit_v2.py"
             /sc weekly /d FRI /st 16:00 /f
"""

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

_ROOT   = Path(__file__).parent
_MASTER = _ROOT.parent / "MasterConfiguration"
_LIB    = _MASTER / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from master_resource import MasterResource

CANDLE_DB  = Path(MasterResource.MASTER_ROOT) / "data" / "kite_candles.db"
REPORT_DIR = Path(MasterResource.MASTER_ROOT) / "reports"
STATE_FILE = _ROOT / "backtest" / "research_audit_state.json"
REPORT_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Backtest baseline WR (from v105 test-window results) ──────────────────────
# These are the out-of-sample numbers we expect the live strategies to match.
BASELINE_WR = {
    ("ORB15_v2",       "NIFTY"):      67.4,
    ("ORB15_v2",       "BANKNIFTY"):  58.4,
    ("GapFill_v2",     "NIFTY"):      66.7,
    ("GapFill_v2",     "BANKNIFTY"):  36.4,   # CONDITIONAL -- watch closely
    ("GapFill_v2",     "SENSEX"):     63.2,
    ("ATRSqueeze_v2",  "NIFTY"):      54.7,
    ("ATRSqueeze_v2",  "BANKNIFTY"):  49.7,
    ("ATRSqueeze_v2",  "SENSEX"):     50.6,
    ("VWAPReclaim_v2", "NIFTY"):      49.1,
    ("VWAPReclaim_v2", "BANKNIFTY"):  49.0,
    ("VWAPReclaim_v2", "SENSEX"):     50.9,
    ("ExpiryBlast_v2", "BANKNIFTY"):  50.0,   # small N
    ("ExpiryBlast_v2", "SENSEX"):    100.0,   # 1 signal only
    ("GammaSqueeze",   "NIFTY"):      66.7,
    ("GammaSqueeze",   "BANKNIFTY"): 100.0,   # 2 signals only
    ("GammaSqueeze",   "SENSEX"):     57.1,
}

# Drift threshold: flag if live WR < baseline - DRIFT_PCT
DRIFT_PCT        = 10.0    # percentage points
MIN_SIGNALS      = 5       # minimum signals required before flagging
CONSEC_WEEKS_FLAG= 2       # flag after N consecutive weeks of drift

INDEX_TOKENS = {
    "NIFTY":      256265,
    "BANKNIFTY":  260105,
    "SENSEX":     265,
}


# ── MySQL helpers ─────────────────────────────────────────────────────────────

def _mysql_conn():
    try:
        import mysql.connector
        cfg = MasterResource.get_db_config()
        return mysql.connector.connect(
            host=cfg.get("host", "127.0.0.1"),
            port=cfg.get("port", 3306),
            user=cfg.get("user", "root"),
            password=cfg.get("password", ""),
            database=cfg.get("database", "trading_live"),
            connection_timeout=10,
        )
    except Exception as e:
        print(f"[MySQL] Connect failed: {e}")
        return None


def fetch_trades(days_back: int) -> pd.DataFrame:
    """
    Load all CLOSED paper trades from research_trades_v2 for the past N days.
    All outcome data (entry_price, exit_price, outcome_pts, win) is already computed
    by the research engine's SL monitor -- no inference needed.
    """
    since = (date.today() - timedelta(days=days_back)).isoformat()
    conn  = _mysql_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT date, strategy_name, index_name, direction,
                      entry_time, entry_price, sl_price, tp_price,
                      exit_time, exit_price, exit_reason,
                      outcome_pts, win
               FROM research_trades_v2
               WHERE date >= %s AND status = 'CLOSED'
               ORDER BY date, entry_time""",
            (since,)
        )
        cols = ["date", "strategy", "index", "direction",
                "entry_time", "entry_price", "sl_price", "tp_price",
                "exit_time", "exit_price", "exit_reason",
                "outcome_pts", "win"]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
        conn.close()
        return df
    except Exception as e:
        print(f"[MySQL] Trade fetch failed: {e}")
        conn.close()
        return pd.DataFrame()


# Outcome computation is NOT needed here.
# research_engine_v2.py tracks every trade entry and exit in real time --
# outcome_pts and win are already written to research_trades_v2 by the engine's
# SL monitor when SL/TP/EOD fires. The audit just reads the completed records.


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"consecutive_drift_weeks": {}, "last_run": None}


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── Report writer ─────────────────────────────────────────────────────────────

def write_report(rows: list[dict], flags: list[dict],
                 from_date: str, to_date: str) -> Path:
    today_str = datetime.now().strftime("%Y-%m-%d")
    path = REPORT_DIR / f"research_audit_v2_{today_str}.md"

    lines = [
        f"# Research Engine v2 -- Weekly Audit",
        f"**Period:** {from_date} to {to_date}",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M IST')}",
        f"**Drift threshold:** >{DRIFT_PCT}% below backtest baseline",
        "",
        "## Performance vs Backtest Baseline",
        "",
        "| Strategy | Index | Live N | Live WR% | Baseline WR% | Delta | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        delta = r["live_wr"] - r["baseline_wr"] if r["live_n"] >= MIN_SIGNALS else None
        delta_str = f"{delta:+.1f}%" if delta is not None else "N/A (low N)"
        status = ("OK" if delta is None or delta >= -DRIFT_PCT else
                  "FLAG" if delta < -DRIFT_PCT else "OK")
        lines.append(
            f"| {r['strategy']} | {r['index']} | {r['live_n']} "
            f"| {r['live_wr']:.1f}% | {r['baseline_wr']:.1f}% "
            f"| {delta_str} | {status} |"
        )

    if flags:
        lines += ["", "## PARAMETER ADJUSTMENT FLAGS", ""]
        for f in flags:
            lines.append(
                f"**{f['strategy']} / {f['index']}** -- "
                f"Consecutive drift weeks: {f['consec']}/{CONSEC_WEEKS_FLAG}. "
                f"Live WR={f['live_wr']:.1f}% vs baseline {f['baseline_wr']:.1f}%. "
                f"**Recommended action: review parameters.**"
            )
    else:
        lines += ["", "## No flags this week -- all strategies within drift tolerance.", ""]

    lines += [
        "",
        "## Deployment Decision",
        "",
        "| Strategy | Index | Signals Accumulated | Promote to Live? |",
        "|---|---|---|---|",
    ]
    for r in rows:
        promote = ("YES -- WR stable" if r["live_n"] >= 30 and
                   (r["live_wr"] >= r["baseline_wr"] - DRIFT_PCT) else
                   f"NOT YET ({r['live_n']}/30 signals)" if r["live_n"] < 30 else
                   "HOLD -- WR drifting")
        lines.append(f"| {r['strategy']} | {r['index']} | {r['live_n']} | {promote} |")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Research Engine v2 weekly audit")
    parser.add_argument("--days",        type=int, default=7, help="Look-back days (default 7)")
    parser.add_argument("--force-flag",  action="store_true", help="Flag even if <2 consecutive weeks")
    args = parser.parse_args()

    today     = date.today()
    from_date = (today - timedelta(days=args.days)).isoformat()
    to_date   = today.isoformat()

    print(f"=== Research Engine v2 -- Weekly Audit ===")
    print(f"Period: {from_date} to {to_date}")
    print()

    # Load completed paper trades (entry + exit already tracked by engine)
    df = fetch_trades(args.days)
    if df.empty:
        print("No closed trades in research_trades_v2.")
        print("Is research_engine_v2.py running? Has the market been open this week?")
        df = pd.DataFrame(columns=["date","strategy","index","direction",
                                    "entry_time","entry_price","sl_price","tp_price",
                                    "exit_time","exit_price","exit_reason",
                                    "outcome_pts","win"])

    print(f"Loaded {len(df)} closed trade rows (entry + exit tracked by engine).")

    # Summarise per strategy x index
    state = _load_state()
    rows  = []
    flags = []

    for (strat, idx), baseline_wr in BASELINE_WR.items():
        sub = df[(df["strategy"] == strat) & (df["index"] == idx)]
        sub = sub[sub["win"].notna()]
        n    = len(sub)
        live_wr = (sub["win"].sum() / n * 100) if n > 0 else 0.0

        row = {
            "strategy":    strat,
            "index":       idx,
            "live_n":      n,
            "live_wr":     round(live_wr, 1),
            "baseline_wr": baseline_wr,
        }
        rows.append(row)

        # Drift check
        key = f"{strat}/{idx}"
        if n >= MIN_SIGNALS and live_wr < baseline_wr - DRIFT_PCT:
            consec = state["consecutive_drift_weeks"].get(key, 0) + 1
            state["consecutive_drift_weeks"][key] = consec
            if consec >= CONSEC_WEEKS_FLAG or args.force_flag:
                flags.append({**row, "consec": consec})
                print(f"  FLAG: {strat}/{idx} -- Live WR={live_wr:.1f}% "
                      f"vs baseline {baseline_wr:.1f}% ({consec} weeks drift)")
        else:
            state["consecutive_drift_weeks"][key] = 0   # reset streak

        if n > 0:
            avg_pts = float(sub["outcome_pts"].mean()) if n > 0 else 0
            sl_hits = int((sub["exit_reason"] == "SL").sum()) if "exit_reason" in sub.columns else 0
            tp_hits = int((sub["exit_reason"] == "TP").sum()) if "exit_reason" in sub.columns else 0
            status  = "OK" if live_wr >= baseline_wr - DRIFT_PCT else "DRIFT"
            print(f"  {strat:20s} | {idx:10s} | N={n:3d} "
                  f"| Live WR={live_wr:5.1f}% | Base={baseline_wr:5.1f}% "
                  f"| Avg={avg_pts:+.1f}pts | TP={tp_hits} SL={sl_hits} | {status}")

    state["last_run"] = today.isoformat()
    _save_state(state)

    report = write_report(rows, flags, from_date, to_date)
    print(f"\nReport: {report}")

    if flags:
        print(f"\n{'='*50}")
        print(f"  {len(flags)} PARAMETER ADJUSTMENT FLAG(S) RAISED")
        print(f"  Review the strategies above and update pattern_discovery_v2.py")
        print(f"{'='*50}")
    else:
        print("\nAll strategies within drift tolerance. No action needed.")


if __name__ == "__main__":
    main()
