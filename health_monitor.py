"""
health_monitor.py — Deep system health watchdog.

Complements the process watchdog in start_trading_system.py.
That watchdog detects dead processes; this one detects BROKEN systems:
  • Process alive but Kite token invalid  (today's issue)
  • Process alive but engine generating zero signals
  • Claude API key missing / invalid
  • Log files stale during market hours

OmniEngine-specific checks (added Phase 21):
  • PID check — reads lock file, verifies PID is running DhanOmniEngine_v2.py
  • Log staleness — OmniEngine v2 health log goes stale within 90s of death
  • Auto-restart — during market hours, dead engine is restarted automatically
    (15-min cooldown prevents restart loops on crash-on-boot bugs)

Sends Telegram alerts on state change (OK→FAIL) and recovery (FAIL→OK).
No spam — alert fires once per fault, clears on recovery.

Config:  MasterConfiguration/config/health_monitor_config.json
  {
    "bot_token":           "<dedicated alert bot token from @BotFather>",
    "alert_chat_id":       "<your Telegram user ID or group chat ID>",
    "check_interval_secs": 120,
    "stale_engine_mins":   5,
    "stale_log_mins":      15,
    "auto_restart_omni":   true,
    "restart_cooldown_mins": 15
  }

Setup (one-time):
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Send any message to your new bot to activate the chat
  3. Get your chat ID: message @userinfobot
  4. Fill in health_monitor_config.json with token + chat_id
  5. This script is auto-launched by start_trading_system.py
"""

import os
import sys
import json
import time
import sqlite3
import logging
import subprocess
import requests
from pathlib import Path
from datetime import datetime, timedelta
import pytz

# ── Path setup ────────────────────────────────────────────────────────────────
_MASTER_LIB = Path(__file__).parent.parent / "MasterConfiguration" / "lib"
sys.path.insert(0, str(_MASTER_LIB))
from master_resource import MasterResource

# ── Constants ─────────────────────────────────────────────────────────────────
IST         = pytz.timezone("Asia/Kolkata")
LOGS_DIR    = MasterResource.MASTER_ROOT / "logs"
TRADING_DB  = MasterResource.MASTER_ROOT / "data" / "trading.db"
CFG_PATH    = MasterResource.MASTER_ROOT / "config" / "health_monitor_config.json"
LOCK_PATH   = MasterResource.MASTER_ROOT / "data" / "dhan_omni_engine.lock"

OMNI_SCRIPT = Path(__file__).parent / "DhanOmniEngine_v2.py"
PYTHONW     = Path(r"C:\ProgramData\anaconda3\pythonw.exe")

_MARKET_START = (8, 45)    # check engine/log staleness only within these hours
_MARKET_END   = (15, 35)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [HealthMon] %(levelname)s %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("HealthMonitor")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    if CFG_PATH.exists():
        with open(CFG_PATH) as f:
            return json.load(f)
    return {}


def _is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return _MARKET_START <= t <= _MARKET_END


def _send_tg(bot_token: str, chat_id: str, text: str):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(
            url,
            json    = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout = 10,
        )
        if not resp.ok:
            logger.warning(f"TG send failed: {resp.status_code} {resp.text[:120]}")
    except Exception as e:
        logger.warning(f"TG send error: {e}")


# ── Individual health checks ──────────────────────────────────────────────────

def _check_kite() -> tuple[bool, str]:
    """Validate Kite access token by calling profile()."""
    try:
        from kite_candle_store import get_kite, reset_kite
        reset_kite()           # force re-init so stale cached instance doesn't hide a bad token
        kite = get_kite()
        if kite is None:
            return False, "Kite token INVALID — get_kite() returned None"
        return True, "Kite token OK"
    except Exception as e:
        return False, f"Kite token INVALID — {e}"


def _check_claude() -> tuple[bool, str]:
    """Check Claude API key exists and is non-empty (no API call — avoids credit burn)."""
    try:
        key = MasterResource.get_claude_key()
        if not key or len(key.strip()) < 20:
            return False, "Claude API key missing or too short"
        return True, "Claude API key present"
    except FileNotFoundError:
        return False, "Claude API key file not found"
    except Exception as e:
        return False, f"Claude API key error — {e}"


def _pid_is_omniengine(pid: int) -> bool:
    """Return True only if PID exists AND its command line contains DhanOmniEngine_v2."""
    try:
        result = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
            capture_output=True, text=True, timeout=5,
        )
        cmd = result.stdout.lower()
        return "omniengine_v2" in cmd or "dhanomniengine_v2" in cmd
    except Exception:
        # Fallback: plain existence check
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _check_omniengine_process() -> tuple[bool, str]:
    """Verify OmniEngine v2 is alive using the lock-file PID."""
    if not _is_market_hours():
        return True, "OmniEngine process: outside market hours (skipped)"

    if not LOCK_PATH.exists():
        return False, "OmniEngine: lock file missing — engine not running"

    try:
        pid = int(LOCK_PATH.read_text().strip())
    except (ValueError, OSError):
        return False, "OmniEngine: lock file unreadable"

    if _pid_is_omniengine(pid):
        return True, f"OmniEngine: alive (PID {pid})"

    # PID exists but belongs to a different process (PID reuse) → treat as dead
    try:
        os.kill(pid, 0)
        return False, f"OmniEngine: PID {pid} exists but is NOT DhanOmniEngine_v2 (PID reused) — engine dead"
    except (OSError, ProcessLookupError):
        return False, f"OmniEngine: PID {pid} not found — process died"


def _check_omniengine_log(stale_mins: int) -> tuple[bool, str]:
    """OmniEngine v2 writes a health log line every 60s — stale log = dead engine."""
    if not _is_market_hours():
        return True, "OmniEngine log: outside market hours (skipped)"

    logs = sorted(LOGS_DIR.glob("dhan_omni_engine_v2_*.log"))
    if not logs:
        return False, "OmniEngine: no v2 log file found"

    latest  = max(logs, key=lambda f: f.stat().st_mtime)
    age_min = (time.time() - latest.stat().st_mtime) / 60
    if age_min > stale_mins:
        return False, (
            f"OmniEngine: health log stale {age_min:.0f}m "
            f"(threshold {stale_mins}m) — {latest.name}"
        )
    return True, f"OmniEngine: log fresh {age_min:.0f}m ago ({latest.name})"


def _check_engine_signals(stale_mins: int) -> tuple[bool, str]:
    """Check that OmniEngine is generating strategy signals (not just alive)."""
    if not _is_market_hours():
        return True, "OmniEngine signals: outside market hours (skipped)"
    try:
        conn = sqlite3.connect(str(TRADING_DB), timeout=5)
        cur  = conn.cursor()
        cur.execute("SELECT ts FROM strategy_signals ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if not row:
            return False, "OmniEngine: strategy_signals table empty — workers not logging signals yet"
        last_ts = datetime.fromisoformat(row[0])
        if last_ts.tzinfo is None:
            last_ts = IST.localize(last_ts)
        age_min = (datetime.now(IST) - last_ts).total_seconds() / 60
        if age_min > stale_mins:
            return False, f"OmniEngine: no new signals for {age_min:.0f}m (threshold {stale_mins}m)"
        return True, f"OmniEngine: last signal {age_min:.0f}m ago — OK"
    except Exception as e:
        return False, f"OmniEngine signals DB check failed — {e}"


def _check_log(name: str, glob_pattern: str, stale_mins: int) -> tuple[bool, str]:
    """Check that a component's log file was updated recently."""
    if not _is_market_hours():
        return True, f"{name}: outside market hours (skipped)"
    files = sorted(LOGS_DIR.glob(glob_pattern))
    if not files:
        return False, f"{name}: no log file found in {LOGS_DIR}"
    latest  = max(files, key=lambda f: f.stat().st_mtime)
    age_min = (time.time() - latest.stat().st_mtime) / 60
    if age_min > stale_mins:
        return False, f"{name}: log stale {age_min:.0f}m — process may be frozen ({latest.name})"
    return True, f"{name}: alive (log updated {age_min:.0f}m ago)"


# ── Auto-restart ──────────────────────────────────────────────────────────────

def _restart_omniengine(bot_token: str, chat_id: str) -> bool:
    """
    Restart OmniEngine v2. Called only during market hours after process-dead confirmed.
    Returns True if the restart was launched successfully.
    """
    logger.warning("[RESTART] OmniEngine dead — attempting auto-restart...")
    try:
        # Remove stale lock so --force isn't needed (cleaner startup)
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
            logger.info("[RESTART] Stale lock cleared")

        proc = subprocess.Popen(
            [str(PYTHONW), str(OMNI_SCRIPT)],
            cwd            = str(OMNI_SCRIPT.parent),
            creationflags  = subprocess.CREATE_NO_WINDOW,
        )
        now_str = datetime.now(IST).strftime("%H:%M IST")
        msg = (
            f"🔄 <b>OmniEngine AUTO-RESTARTED</b>\n"
            f"⏰ {now_str}\n"
            f"PID {proc.pid} — monitor next health check for confirmation"
        )
        logger.info(f"[RESTART] Launched PID {proc.pid}")
        if bot_token:
            _send_tg(bot_token, chat_id, msg)
        return True
    except Exception as e:
        logger.error(f"[RESTART] Failed: {e}")
        if bot_token:
            _send_tg(bot_token, chat_id,
                     f"❌ <b>OmniEngine restart FAILED</b>\n{e}\nManual intervention needed.")
        return False


# ── Alert formatter ───────────────────────────────────────────────────────────

def _format_alert(checks: dict[str, tuple[bool, str]]) -> str:
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    lines   = [f"⚠️ <b>TRADING SYSTEM ALERT</b>", f"🕐 {now_str}", ""]
    for name, (ok, msg) in checks.items():
        icon = "✅" if ok else "❌"
        lines.append(f"{icon} {msg}")
    lines += ["", "👉 Check logs and fix before market opens."]
    return "\n".join(lines)


def _format_recovery(checks: dict[str, tuple[bool, str]]) -> str:
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    lines   = [f"✅ <b>ALL SYSTEMS RECOVERED</b>", f"🕐 {now_str}", ""]
    for name, (ok, msg) in checks.items():
        lines.append(f"✅ {msg}")
    return "\n".join(lines)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    cfg              = _load_cfg()
    bot_token        = cfg.get("bot_token", "").strip()
    chat_id          = cfg.get("alert_chat_id", "").strip()
    interval         = int(cfg.get("check_interval_secs",  120))   # default 2 min
    stale_eng        = int(cfg.get("stale_engine_mins",      5))    # log stale threshold
    stale_log        = int(cfg.get("stale_log_mins",         15))
    auto_restart     = cfg.get("auto_restart_omni", True)
    cooldown_mins    = int(cfg.get("restart_cooldown_mins",  15))

    if not bot_token or bot_token.startswith("<"):
        logger.error(
            f"health_monitor_config.json not configured — "
            f"set bot_token and alert_chat_id in {CFG_PATH}"
        )
        return

    logger.info(f"Health monitor started — checking every {interval}s")
    logger.info(f"OmniEngine: process check + log stale>{stale_eng}m  |  auto_restart={auto_restart}  |  cooldown={cooldown_mins}m")
    _send_tg(bot_token, chat_id,
             f"🟢 <b>Health Monitor started</b>\n"
             f"🕐 {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}\n"
             f"Watching: Kite · Claude · OmniEngine (PID+log) · SL Monitor · Dashboard · TG Reader\n"
             f"Auto-restart OmniEngine during market hours: {'✅' if auto_restart else '❌'}")

    prev_failures:  set[str] = set()
    last_restart_at: datetime | None = None

    while True:
        try:
            proc_ok, proc_msg = _check_omniengine_process()
            log_ok,  log_msg  = _check_omniengine_log(stale_eng)

            # OmniEngine is healthy only if BOTH process AND log are OK
            omni_ok  = proc_ok and log_ok
            omni_msg = proc_msg if not proc_ok else (log_msg if not log_ok else proc_msg)

            checks: dict[str, tuple[bool, str]] = {
                "Kite token":       _check_kite(),
                "Claude API":       _check_claude(),
                "OmniEngine":       (omni_ok, omni_msg),
                "OmniEngine signals": _check_engine_signals(stale_eng * 6),  # looser — 30m
                "SL Monitor":       _check_log("SL Monitor", "dhan_sl_monitor*.log",          stale_log),
                "Dashboard":        _check_log("Dashboard",  "dhan_dashboard*.log",            stale_log),
                "TG Reader":        _check_log("TG Reader",  "telegram_reader_production*.log", stale_log),
            }

            failures       = {k for k, (ok, _) in checks.items() if not ok}
            new_failures   = failures - prev_failures
            new_recoveries = prev_failures - failures

            if new_failures:
                logger.warning(f"New failures: {new_failures}")
                _send_tg(bot_token, chat_id, _format_alert(checks))

            elif new_recoveries and not failures:
                logger.info("All systems recovered")
                _send_tg(bot_token, chat_id, _format_recovery(checks))

            prev_failures = failures

            for name, (ok, msg) in checks.items():
                lvl = logging.INFO if ok else logging.WARNING
                logger.log(lvl, msg)

            # ── Auto-restart OmniEngine if dead during market hours ────────────
            if (
                "OmniEngine" in failures
                and auto_restart
                and _is_market_hours()
                and not proc_ok          # only restart if the process is actually dead
            ):
                now = datetime.now(IST)
                cooldown_ok = (
                    last_restart_at is None
                    or (now - last_restart_at).total_seconds() > cooldown_mins * 60
                )
                if cooldown_ok:
                    if _restart_omniengine(bot_token, chat_id):
                        last_restart_at = now
                else:
                    elapsed = (now - last_restart_at).total_seconds() / 60
                    logger.warning(
                        f"[RESTART] OmniEngine dead but cooldown active "
                        f"({elapsed:.0f}m elapsed, need {cooldown_mins}m) — skipping"
                    )

        except Exception as e:
            logger.error(f"Check cycle error: {e}")

        time.sleep(interval)


if __name__ == "__main__":
    run()
