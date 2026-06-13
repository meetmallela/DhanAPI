"""
research_engine_v2.py
---------------------
Process 10 -- v2 strategy shadow engine with full paper trade tracking.
Uses the SAME 3-stage ATR trailing SL as the live OmniEngine (sl_engine.py).

SL stages (identical to live dhan_sl_monitor.py):
  INITIAL       -> SL = entry - 1.0 ATR  (initial hard stop)
  BREAKEVEN     -> gain >= 1.0 ATR  -> SL moves to entry
  PROFIT_LOCK   -> gain >= 1.5 ATR  -> SL locks at entry + 0.75 ATR
  ATR_TRAILING  -> gain >= 2.5 ATR  -> SL trails peak at peak - 1.5 ATR

Hard exits (strategy-specific):
  GammaSqueeze  : force-close at 15:12 IST
  ExpiryBlast   : force-close at 15:25 IST
  All trades    : force-close at 15:25 IST EOD

Tables (MySQL trading_live):
  strategy_signals_v2  -- one row per signal fired
  research_trades_v2   -- full trade lifecycle: entry, peak, trailing SL, exit, P&L

Run:
    python research_engine_v2.py
"""

import json
import logging
import signal as _signal
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz

_ROOT   = Path(__file__).parent
_MASTER = _ROOT.parent / "MasterConfiguration"
_LIB    = _MASTER / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from master_resource import MasterResource
from sl_engine import step_sl
from pattern_discovery_v2 import (
    add_indicators_v2,
    find_orb15_v2, find_gap_fill_v2, find_atr_squeeze_v2,
    find_vwap_reclaim_v2, find_expiry_blast_v2, find_gamma_squeeze,
)

IST       = pytz.timezone("Asia/Kolkata")
CANDLE_DB = Path(MasterResource.MASTER_ROOT) / "data" / "kite_candles.db"
SL_CFG_PATH = Path(MasterResource.MASTER_ROOT) / "config" / "sl_config.json"
LOG_DIR   = Path(MasterResource.MASTER_ROOT) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_ts  = datetime.now().strftime("%d%b%Y_%H_%M_%S")
_log = LOG_DIR / f"research_engine_v2_{_ts}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RESEARCH_v2] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(str(_log), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("research_v2")

INDEX_TOKENS = {
    "NIFTY":      256265,
    "BANKNIFTY":  260105,
    "SENSEX":     265,
}
EXPIRY_WEEKDAY = {"NIFTY": 1, "BANKNIFTY": 2, "SENSEX": 1}

MARKET_OPEN_MIN  = 9 * 60 + 15
MARKET_CLOSE_MIN = 15 * 60 + 30
EOD_FORCE_MIN    = 15 * 60 + 25
POLL_SECS        = 300

# Per-strategy hard time exits (strategy name -> "HH:MM" or None)
HARD_EXIT_TIMES = {
    "GammaSqueeze":   "15:12",
    "ExpiryBlast_v2": "15:25",
}


# ── SL config loader ─────────────────────────────────────────────────────────

def _load_sl_cfg() -> dict:
    """Load sl_config.json -- same file the live SL monitor uses."""
    try:
        return json.loads(SL_CFG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"sl_config.json load failed: {e} -- using defaults")
        return {
            "atr_trail_mult": 2.5, "atr_trail_dist": 1.5,
            "atr_lock_mult":  1.5, "atr_lock_dist":  0.75,
            "atr_beven_mult": 1.0,
        }

_SL_CFG = _load_sl_cfg()


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
        logger.warning(f"MySQL connect failed: {e}")
        return None


def _ensure_tables():
    conn = _mysql_conn()
    if conn is None:
        return
    cur = conn.cursor()

    # Signal log
    cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_signals_v2 (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            ts              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            date            DATE NOT NULL,
            strategy_name   VARCHAR(64) NOT NULL,
            index_name      VARCHAR(32) NOT NULL,
            signal          VARCHAR(16) NOT NULL,
            spot_price      DECIMAL(10,2),
            bar_time        TIME,
            atr             DECIMAL(10,2),
            extra_json      TEXT,
            UNIQUE KEY uq_sig (date, strategy_name, index_name, bar_time)
        ) ENGINE=InnoDB
    """)

    # Full paper trade lifecycle -- mirrors live dhan_sl_monitor columns
    cur.execute("""
        CREATE TABLE IF NOT EXISTS research_trades_v2 (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            signal_id       INT,
            date            DATE NOT NULL,
            strategy_name   VARCHAR(64) NOT NULL,
            index_name      VARCHAR(32) NOT NULL,
            action          VARCHAR(8)  NOT NULL,   -- BUY or SELL
            direction       VARCHAR(16),
            entry_time      DATETIME NOT NULL,
            entry_price     DECIMAL(10,2) NOT NULL,
            entry_atr       DECIMAL(10,2),          -- ATR at entry (drives all SL stages)
            initial_sl      DECIMAL(10,2) NOT NULL, -- entry +/- 1 ATR
            peak_price      DECIMAL(10,2),          -- best price seen (max BUY / min SELL)
            current_sl      DECIMAL(10,2),          -- trailing SL (updated each poll)
            sl_stage        VARCHAR(32) DEFAULT 'INITIAL',
            hard_exit_time  VARCHAR(8),
            exit_time       DATETIME,
            exit_price      DECIMAL(10,2),
            exit_reason     VARCHAR(32),            -- SL | EOD | TIME_EXIT_HH:MM
            outcome_pts     DECIMAL(10,2),          -- spot pts gained/lost
            win             TINYINT,
            status          VARCHAR(8) DEFAULT 'OPEN',
            INDEX idx_rt_date   (date),
            INDEX idx_rt_status (status),
            UNIQUE KEY uq_trade (date, strategy_name, index_name, entry_time)
        ) ENGINE=InnoDB
    """)

    # Add columns if upgrading from older schema (ignore errors if already exist)
    for alter in [
        "ALTER TABLE research_trades_v2 ADD COLUMN entry_atr DECIMAL(10,2)",
        "ALTER TABLE research_trades_v2 ADD COLUMN peak_price DECIMAL(10,2)",
        "ALTER TABLE research_trades_v2 ADD COLUMN current_sl DECIMAL(10,2)",
        "ALTER TABLE research_trades_v2 ADD COLUMN sl_stage VARCHAR(32) DEFAULT 'INITIAL'",
        "ALTER TABLE research_trades_v2 ADD COLUMN action VARCHAR(8)",
    ]:
        try:
            cur.execute(alter)
        except Exception:
            pass   # column already exists

    conn.commit()
    conn.close()
    logger.info("Tables ready: strategy_signals_v2, research_trades_v2")


def _insert_signal(strategy, index, signal, spot, bar_time, atr, extra) -> int | None:
    conn = _mysql_conn()
    if conn is None:
        return None
    today = date.today().isoformat()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT IGNORE INTO strategy_signals_v2
               (date, strategy_name, index_name, signal, spot_price, bar_time, atr, extra_json)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (today, strategy, index, signal, spot, bar_time, atr, json.dumps(extra or {}))
        )
        conn.commit()
        sig_id = cur.lastrowid if cur.rowcount else None
        conn.close()
        return sig_id
    except Exception as e:
        logger.warning(f"Signal insert: {e}")
        conn.close()
        return None


def _open_trade(signal_id, strategy, index, action, direction,
                entry_price, atr, initial_sl, hard_exit) -> int | None:
    """
    Open a paper trade with initial SL = entry +/- 1 ATR.
    peak_price starts at entry_price; current_sl = initial_sl.
    """
    conn = _mysql_conn()
    if conn is None:
        return None
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = date.today().isoformat()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT IGNORE INTO research_trades_v2
               (signal_id, date, strategy_name, index_name, action, direction,
                entry_time, entry_price, entry_atr, initial_sl,
                peak_price, current_sl, sl_stage, hard_exit_time)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'INITIAL',%s)""",
            (signal_id, today, strategy, index, action, direction,
             now, entry_price, atr, initial_sl,
             entry_price,    # peak starts at entry
             initial_sl,     # current_sl starts at initial_sl
             hard_exit or "")
        )
        conn.commit()
        trade_id = cur.lastrowid if cur.rowcount else None
        conn.close()
        if trade_id:
            logger.info(
                f"[OPEN ] {strategy:<20} | {index:<10} | {action} | "
                f"entry={entry_price:.1f} | initial_SL={initial_sl:.1f} | ATR={atr:.2f}"
            )
        return trade_id
    except Exception as e:
        logger.warning(f"Trade open: {e}")
        conn.close()
        return None


def _update_trade_sl(trade_id: int, peak: float, new_sl: float, stage: str):
    conn = _mysql_conn()
    if conn is None:
        return
    try:
        conn.cursor().execute(
            """UPDATE research_trades_v2
               SET peak_price=%s, current_sl=%s, sl_stage=%s
               WHERE id=%s""",
            (peak, new_sl, stage, trade_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"SL update: {e}")
        conn.close()


def _close_trade(trade_id: int, exit_price: float, exit_reason: str,
                 action: str, entry_price: float):
    outcome_pts = (exit_price - entry_price if action == "BUY"
                   else entry_price - exit_price)
    win = 1 if outcome_pts > 0 else 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _mysql_conn()
    if conn is None:
        return
    try:
        conn.cursor().execute(
            """UPDATE research_trades_v2
               SET exit_time=%s, exit_price=%s, exit_reason=%s,
                   outcome_pts=%s, win=%s, status='CLOSED'
               WHERE id=%s""",
            (now, exit_price, exit_reason, round(outcome_pts, 2), win, trade_id)
        )
        conn.commit()
        conn.close()
        logger.info(
            f"[CLOSE] id={trade_id:<4} | {exit_reason:<20} | "
            f"exit={exit_price:.1f} | P&L={outcome_pts:+.1f}pts | "
            f"{'WIN' if win else 'LOSS'}"
        )
    except Exception as e:
        logger.warning(f"Trade close: {e}")
        conn.close()


def _load_open_trades() -> list[dict]:
    conn = _mysql_conn()
    if conn is None:
        return []
    today = date.today().isoformat()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, strategy_name, index_name, action,
                      entry_price, entry_atr,
                      peak_price, current_sl, sl_stage, hard_exit_time
               FROM research_trades_v2
               WHERE date=%s AND status='OPEN'""",
            (today,)
        )
        cols = ["id", "strategy", "index", "action",
                "entry_price", "atr",
                "peak", "current_sl", "sl_stage", "hard_exit"]
        trades = [dict(zip(cols, row)) for row in cur.fetchall()]
        conn.close()
        return trades
    except Exception as e:
        logger.warning(f"Load open trades: {e}")
        conn.close()
        return []


# ── Candle helpers ────────────────────────────────────────────────────────────

def _load_candles(symbol: str, hist_days: int = 7) -> pd.DataFrame:
    token = INDEX_TOKENS.get(symbol)
    if token is None:
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(str(CANDLE_DB), timeout=10)
        since = (date.today() - timedelta(days=hist_days * 2)).isoformat()
        rows = conn.execute(
            """SELECT dt, open, high, low, close, volume
               FROM candles_5min
               WHERE instrument_token=? AND dt >= ?
               ORDER BY dt""",
            (token, since + " 00:00:00"),
        ).fetchall()
        conn.close()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        if df["timestamp"].dt.tz is not None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(None)
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        logger.warning(f"Candle load {symbol}: {e}")
        return pd.DataFrame()


def _latest_bar(symbol: str) -> dict | None:
    df = _load_candles(symbol, hist_days=1)
    if df.empty:
        return None
    today_df = df[df["timestamp"].dt.date == date.today()]
    if today_df.empty:
        return None
    bar = today_df.iloc[-1]
    return {
        "high":  float(bar["high"]),
        "low":   float(bar["low"]),
        "close": float(bar["close"]),
        "time":  bar["timestamp"],
    }


# ── Unified SL monitor (uses step_sl from sl_engine.py) ──────────────────────

def monitor_open_trades():
    """
    For each open paper trade:
    1. Fetch latest 5m bar for the index.
    2. Check SL breach against CURRENT (pre-update) SL first -- conservative.
    3. If not breached: update peak, call step_sl(), update trailing SL in DB.
    4. Check hard time exits and EOD force-close.
    """
    trades = _load_open_trades()
    if not trades:
        return

    now_ist  = datetime.now(IST)
    now_time = now_ist.strftime("%H:%M")
    now_mins = now_ist.hour * 60 + now_ist.minute
    hour_ist = now_ist.hour

    # Cache latest bars per symbol (one DB read per symbol, not per trade)
    bar_cache: dict[str, dict | None] = {}

    for t in trades:
        sym = t["index"]
        if sym not in bar_cache:
            bar_cache[sym] = _latest_bar(sym)
        bar = bar_cache[sym]
        if bar is None:
            continue

        bar_high  = bar["high"]
        bar_low   = bar["low"]
        bar_close = bar["close"]

        action     = t["action"] or "BUY"
        entry      = float(t["entry_price"])
        atr        = float(t["atr"]) if t["atr"] else 0.0
        peak       = float(t["peak"]) if t["peak"] else entry
        current_sl = float(t["current_sl"]) if t["current_sl"] else float(t.get("atr", 0))
        hard_exit  = (t["hard_exit"] or "").strip()
        is_long    = action == "BUY"

        exit_price  = None
        exit_reason = None

        # ── Step 1: Check SL breach against CURRENT SL (before any update) ──
        if is_long and bar_low <= current_sl:
            exit_price  = current_sl
            exit_reason = "SL"
        elif not is_long and bar_high >= current_sl:
            exit_price  = current_sl
            exit_reason = "SL"

        # ── Step 2: If no SL hit, update peak and advance trailing SL ────────
        if exit_price is None and atr > 0:
            # Update peak: max for BUY, min for SELL
            new_peak = max(peak, bar_high) if is_long else min(peak, bar_low)

            # Call the same step_sl used by live SL monitor
            new_sl, stage = step_sl(
                entry=entry,
                action=action,
                peak=new_peak,
                current_sl=current_sl,
                atr=atr,
                cfg=_SL_CFG,
                hour=hour_ist,
            )

            # Log stage transitions
            if stage != t.get("sl_stage", "INITIAL"):
                logger.info(
                    f"[SL STAGE] id={t['id']} {t['strategy']}/{sym} "
                    f"{t.get('sl_stage','?')} -> {stage} | "
                    f"peak={new_peak:.1f} new_sl={new_sl:.1f}"
                )

            _update_trade_sl(t["id"], new_peak, new_sl, stage)

        # ── Step 3: Hard time exit (GammaSqueeze 15:12, ExpiryBlast 15:25) ──
        if exit_price is None and hard_exit and now_time >= hard_exit:
            exit_price  = bar_close
            exit_reason = f"TIME_EXIT_{hard_exit}"

        # ── Step 4: EOD force-close all remaining trades at 15:25 ────────────
        if exit_price is None and now_mins >= EOD_FORCE_MIN:
            exit_price  = bar_close
            exit_reason = "EOD"

        if exit_price is not None:
            _close_trade(t["id"], exit_price, exit_reason, action, entry)


# ── Signal cache ──────────────────────────────────────────────────────────────

class _SignalCache:
    def __init__(self):
        self._seen: set = set()
        self._day = date.today()

    def _check_day(self):
        if date.today() != self._day:
            self._seen.clear()
            self._day = date.today()

    def is_new(self, strategy: str, index: str, bar_time: str) -> bool:
        self._check_day()
        key = (strategy, index, str(bar_time)[:8])
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


_CACHE = _SignalCache()


# ── Signal handler: new signal -> open paper trade ────────────────────────────

def _handle_signals(df_signals: pd.DataFrame, strategy: str, symbol: str):
    if df_signals is None or df_signals.empty:
        return
    today_str = date.today().isoformat()
    today_sigs = df_signals[df_signals["date"] == today_str]

    for _, row in today_sigs.iterrows():
        bar_time  = str(row.get("time", "00:00:00"))[:8]
        if not _CACHE.is_new(strategy, symbol, bar_time):
            continue

        direction   = row.get("direction", "BULLISH")
        entry_price = float(row.get("entry_price", 0))
        atr         = float(row.get("atr", 0))
        if entry_price == 0 or atr == 0:
            logger.warning(f"[SKIP] {strategy}/{symbol}: entry={entry_price} atr={atr}")
            continue

        # action for sl_engine (BUY = long, SELL = short)
        action = "BUY" if direction in ("BULLISH", "LONG") else "SELL"

        # Initial SL = entry ± 1 ATR (INITIAL stage, same as live)
        initial_sl = (round(entry_price - atr * _SL_CFG.get("atr_beven_mult", 1.0), 2)
                      if action == "BUY"
                      else round(entry_price + atr * _SL_CFG.get("atr_beven_mult", 1.0), 2))

        hard_exit = HARD_EXIT_TIMES.get(strategy, "")

        extra = {k: str(v) for k, v in row.items()
                 if k not in ("date", "pattern", "symbol", "direction",
                               "time", "entry_price", "win", "skipped", "atr")}

        sig_id = _insert_signal(
            strategy, symbol, direction,
            entry_price, bar_time, atr, extra
        )
        if sig_id is None:
            continue   # duplicate signal already logged today

        _open_trade(
            sig_id, strategy, symbol, action, direction,
            entry_price, atr, initial_sl, hard_exit
        )


# ── Symbol scan ───────────────────────────────────────────────────────────────

def scan_symbol(symbol: str, df: pd.DataFrame):
    today   = date.today()
    is_exp  = today.weekday() == EXPIRY_WEEKDAY.get(symbol, -1)
    is_gamma= today.weekday() in (0, 2, 4)

    def _active(d):
        if d.empty or "skipped" not in d.columns:
            return d
        return d[d["skipped"] == False]

    if symbol in ("NIFTY", "BANKNIFTY"):
        _handle_signals(_active(find_orb15_v2(df, symbol)),    "ORB15_v2",       symbol)

    _handle_signals(_active(find_gap_fill_v2(df, symbol)),     "GapFill_v2",     symbol)
    _handle_signals(_active(find_atr_squeeze_v2(df, symbol)),  "ATRSqueeze_v2",  symbol)
    _handle_signals(find_vwap_reclaim_v2(df, symbol),          "VWAPReclaim_v2", symbol)

    if is_exp and symbol in ("BANKNIFTY", "SENSEX"):
        _handle_signals(find_expiry_blast_v2(df, symbol),      "ExpiryBlast_v2", symbol)

    if is_gamma:
        _handle_signals(find_gamma_squeeze(df, symbol),        "GammaSqueeze",   symbol)


# ── Market hours ──────────────────────────────────────────────────────────────

def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return MARKET_OPEN_MIN <= mins <= MARKET_CLOSE_MIN


def _mins_to_open() -> int:
    now  = datetime.now(IST)
    mins = now.hour * 60 + now.minute
    return max(0, MARKET_OPEN_MIN - mins)


# ── EOD summary ───────────────────────────────────────────────────────────────

def _daily_summary():
    conn = _mysql_conn()
    if conn is None:
        return
    today = date.today().isoformat()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT strategy_name, index_name,
                      COUNT(*) AS n,
                      SUM(win)  AS wins,
                      SUM(outcome_pts) AS total_pts,
                      AVG(outcome_pts) AS avg_pts
               FROM research_trades_v2
               WHERE date=%s AND status='CLOSED'
               GROUP BY strategy_name, index_name
               ORDER BY strategy_name, index_name""",
            (today,)
        )
        rows = cur.fetchall()
        conn.close()
        logger.info(f"[EOD SUMMARY] {today}")
        if not rows:
            logger.info("  No closed paper trades today")
            return
        logger.info(f"  {'Strategy':<22} {'Index':<12} {'N':>3} "
                    f"{'WR%':>6} {'Total pts':>10} {'Avg pts':>8}")
        logger.info(f"  {'-'*65}")
        for strat, idx, n, wins, total, avg in rows:
            wr = (wins / n * 100) if n else 0
            logger.info(f"  {strat:<22} {idx:<12} {n:>3} "
                        f"{wr:>5.1f}% {total:>+10.1f} {avg:>+8.1f}")
    except Exception as e:
        logger.warning(f"Daily summary: {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────

_RUNNING = True

def _shutdown(sig, frame):
    global _RUNNING
    logger.info("Shutdown signal -- stopping research engine.")
    _RUNNING = False

_signal.signal(_signal.SIGINT,  _shutdown)
_signal.signal(_signal.SIGTERM, _shutdown)


def main():
    logger.info("=" * 65)
    logger.info("  Research Engine v2 -- PAPER TRADE SHADOW MODE")
    logger.info("  SL logic: IDENTICAL to live OmniEngine (sl_engine.py)")
    logger.info("  Stages: INITIAL -> BREAKEVEN -> PROFIT_LOCK -> ATR_TRAILING")
    logger.info(f"  Config: {SL_CFG_PATH.name} (atr_beven={_SL_CFG.get('atr_beven_mult')} "
                f"atr_lock={_SL_CFG.get('atr_lock_mult')} "
                f"atr_trail={_SL_CFG.get('atr_trail_mult')})")
    logger.info("=" * 65)

    _ensure_tables()

    symbols      = ["NIFTY", "BANKNIFTY", "SENSEX"]
    cycle        = 0
    eod_done_day = None

    while _RUNNING:
        now_ist = datetime.now(IST)

        if not _is_market_open():
            wait = _mins_to_open()
            if wait > 0:
                logger.info(f"Market opens in {wait} min -- sleeping 60s")
                time.sleep(60)
            else:
                today_str = date.today().isoformat()
                if eod_done_day != today_str and now_ist.weekday() < 5:
                    _daily_summary()
                    eod_done_day = today_str
                time.sleep(300)
            continue

        cycle += 1
        logger.info(
            f"[Cycle {cycle}] {now_ist.strftime('%H:%M:%S')} IST -- "
            f"Monitor -> Detect -> Update SL"
        )

        # Monitor FIRST (check SL/exits before detecting new signals)
        monitor_open_trades()

        # Detect new signals and open paper trades
        for sym in symbols:
            try:
                df = _load_candles(sym, hist_days=7)
                if df.empty or len(df) < 20:
                    continue
                df = add_indicators_v2(df)
                scan_symbol(sym, df)
            except Exception as e:
                logger.error(f"Scan error {sym}: {e}", exc_info=True)

        if cycle % 10 == 0:
            open_count = len(_load_open_trades())
            logger.info(f"[HEARTBEAT] cycle={cycle} | open_paper_trades={open_count}")

        time.sleep(POLL_SECS)

    logger.info("Research engine stopped.")


if __name__ == "__main__":
    main()
