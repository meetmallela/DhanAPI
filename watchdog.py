"""
watchdog.py
-----------
Heartbeat-based process watchdog for the DhanAPI trading system.

Complements the PID-based watchdog in start_trading_system.py:
  • PID watchdog    — detects CRASHED processes (PID gone)
  • This watchdog   — detects HUNG processes (PID alive but loop is stuck)

How it works
------------
1. Every agent writes a heartbeat row to the system_status table (via
   core.watchdog_store.heartbeat) on each main-loop iteration.
2. This daemon reads that table every POLL_SECS (30).
3. If an agent's last_heartbeat is older than STALE_SECS (300) AND its
   status is not 'WARMUP', the watchdog:
     a. Sets the agent's status to WARMUP (prevents double-restart).
     b. Kills the owning process by PID.
     c. Launches a fresh copy via the PROCESSES map.
     d. Sends a Telegram alert.
4. On the next cycle after restart, the fresh agent writes ACTIVE and
   normal monitoring resumes.

WARMUP guard
------------
A process needs ~30–60s to load historical data and warm up. Writing
WARMUP immediately after triggering a restart prevents the watchdog from
firing a second restart before the first one finishes loading.

Run
---
    python watchdog.py                 # starts monitoring loop (foreground)
    python watchdog.py --once          # single check then exit (for testing)

Registered as the 9th process in start_trading_system.py PROCESSES.
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
DIR    = Path(__file__).parent
PYEXE  = Path(r"C:\ProgramData\anaconda3\pythonw.exe")
PIDFILE = DIR / ".trading_pids.json"

# ── MasterConfiguration lib path ──────────────────────────────────────────────
_MASTER_LIB = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
if str(_MASTER_LIB) not in sys.path:
    sys.path.insert(0, str(_MASTER_LIB))
from master_resource import MasterResource

# ── Logging ────────────────────────────────────────────────────────────────────
log_dir  = MasterResource.MASTER_ROOT / "logs"
log_dir.mkdir(exist_ok=True)
log_file = str(log_dir / f"watchdog_{datetime.now().strftime('%d%b%Y_%H_%M_%S')}.log")

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s - watchdog - %(levelname)s - %(message)s",
    handlers = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("watchdog")
logger.info(f"[LOG] Writing to: {log_file}")

# ── DhanAPI imports ────────────────────────────────────────────────────────────
sys.path.insert(0, str(DIR))
from core.watchdog_store import (
    ensure_table, get_all_statuses, set_warmup, set_stopped, heartbeat as _hb
)

# ── Tuning knobs ───────────────────────────────────────────────────────────────
POLL_SECS  = 30     # how often the watchdog checks the DB
STALE_SECS = 300    # heartbeat age that triggers a restart (5 min)

# ── Telegram helpers ───────────────────────────────────────────────────────────
_TG_TOKEN   = "8155923389:AAEIfjjaJNA_57zqn2czgZoTWpqcKKFxwTU"
_TG_CHAT_ID = "494844168"


def _tg(msg: str) -> None:
    try:
        url  = f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": _TG_CHAT_ID, "text": msg}).encode()
        urllib.request.urlopen(url, data=data, timeout=5)
    except Exception:
        pass


# ── Process definitions (mirrors start_trading_system.py PROCESSES) ────────────
# Maps process display-name → script (+ optional args) as launched by start_ts
PROCESS_SCRIPTS = {
    "TG Reader":      "telegram_reader_production.py",
    "Order Placer":   "order_placer_dhan_sandbox.py",
    "SL Monitor":     "dhan_sl_monitor.py",
    "OmniEngine":     "DhanOmniEngine_v2.py --force",
    "Dashboard":      "dhan_dashboard.py",
    "EOD WhatIf":     "eod_whatif_backtest.py schedule",
    "Health Monitor": "health_monitor.py",
    "Swing Agent":    "agents/swing_agent.py",
}

# Maps heartbeat agent_name → which process to restart
AGENT_TO_PROCESS = {
    "data_agent":      "OmniEngine",
    "execution_agent": "OmniEngine",
    "sl_monitor":      "SL Monitor",
    "order_placer":    "Order Placer",
}

# Warmup budget: how long (seconds) to wait before checking the restarted agent again
WARMUP_SECS = 90


# ── Process helpers ────────────────────────────────────────────────────────────

def _is_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except ImportError:
        pass
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}"], text=True, stderr=subprocess.DEVNULL
        )
        return str(pid) in out
    except Exception:
        return False


def _kill_pid(pid: int) -> None:
    """Best-effort kill of a PID."""
    try:
        import psutil
        try:
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=3)
            except psutil.TimeoutExpired:
                p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    except ImportError:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        except Exception:
            pass


def _launch_process(process_name: str) -> int | None:
    """Start a fresh copy of the given process. Returns new PID or None."""
    script_spec = PROCESS_SCRIPTS.get(process_name)
    if not script_spec:
        logger.error("[WATCHDOG] Unknown process name: %s", process_name)
        return None

    parts       = script_spec.split()
    script_path = DIR / parts[0]
    extra_args  = parts[1:]

    if not script_path.exists():
        logger.error("[WATCHDOG] Script not found: %s", script_path)
        return None

    DETACHED_PROCESS     = 0x00000008
    CREATE_NO_WINDOW     = 0x08000000
    CREATE_NEW_PROC_GRP  = 0x00000200

    try:
        ts         = datetime.now().strftime("%d%b%Y_%H_%M_%S")
        stderr_log = log_dir / f"{script_path.stem}_watchdog_restart_{ts}.log"
        fh = open(stderr_log, "w")
        proc = subprocess.Popen(
            [str(PYEXE), str(script_path)] + extra_args,
            cwd           = str(DIR),
            creationflags = DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROC_GRP,
            close_fds     = False,
            stdout        = fh,
            stderr        = fh,
        )
        logger.info("[WATCHDOG] Launched %s → PID=%d  log=%s", process_name, proc.pid, stderr_log.name)
        return proc.pid
    except Exception as e:
        logger.error("[WATCHDOG] Failed to launch %s: %s", process_name, e)
        return None


def _update_pidfile(process_name: str, new_pid: int) -> None:
    """Update .trading_pids.json with the new PID after a restart."""
    try:
        if PIDFILE.exists():
            data = json.loads(PIDFILE.read_text())
        else:
            data = {"started": datetime.now().isoformat(), "pids": {}}
        data["pids"][process_name] = new_pid
        PIDFILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning("[WATCHDOG] Could not update PIDFILE: %s", e)


# ── WARMUP cooldown tracker ────────────────────────────────────────────────────
# Tracks when a restart was triggered so we don't check again during warmup
_restart_times: dict[str, float] = {}   # agent_name → monotonic time of last restart


# ── Main check ────────────────────────────────────────────────────────────────

def _check_once() -> None:
    """
    Single pass: read system_status, identify stale agents, restart their processes.
    """
    statuses = get_all_statuses()
    if not statuses:
        logger.debug("[WATCHDOG] system_status table empty — no agents registered yet")
        return

    now      = datetime.now()
    restarts: set[str] = set()   # process names queued for restart this cycle

    for row in statuses:
        agent  = row["agent_name"]
        status = row.get("status", "ACTIVE")
        hb_str = row.get("last_heartbeat", "")
        pid    = row.get("pid")

        # Skip WARMUP and STOPPED agents
        if status in ("WARMUP", "STOPPED"):
            logger.debug("[WATCHDOG] %s is %s — skipping", agent, status)
            continue

        # Skip agents still within their post-restart warmup budget
        last_restart = _restart_times.get(agent, 0.0)
        if time.monotonic() - last_restart < WARMUP_SECS:
            logger.debug("[WATCHDOG] %s in post-restart cooldown", agent)
            continue

        # Parse heartbeat timestamp
        try:
            hb_dt   = datetime.fromisoformat(hb_str)
            age_sec = (now - hb_dt).total_seconds()
        except ValueError:
            logger.warning("[WATCHDOG] %s: unparseable heartbeat '%s'", agent, hb_str)
            continue

        if age_sec <= STALE_SECS:
            logger.debug("[WATCHDOG] %s alive — age=%.0fs", agent, age_sec)
            continue

        # Stale heartbeat detected
        process_name = AGENT_TO_PROCESS.get(agent)
        if process_name is None:
            logger.warning(
                "[WATCHDOG] %s heartbeat stale (%.0fs) — no process mapping, skipping",
                agent, age_sec
            )
            continue

        if process_name in restarts:
            # Already queued this process due to another agent in the same process
            set_warmup(agent, f"queued with {process_name} restart")
            _restart_times[agent] = time.monotonic()
            continue

        logger.warning(
            "[WATCHDOG] %s heartbeat STALE: %.0fs > %ds threshold — restarting %s (PID=%s)",
            agent, age_sec, STALE_SECS, process_name, pid
        )
        _tg(
            f"⚠️ WATCHDOG: {agent} heartbeat stale ({int(age_sec)}s).\n"
            f"Restarting process: {process_name}..."
        )

        # Step 1: Mark as WARMUP before kill to prevent cascade
        set_warmup(agent, f"restart triggered at {now.strftime('%H:%M:%S')}")
        _restart_times[agent] = time.monotonic()
        restarts.add(process_name)

        # Step 2: Kill the existing process
        if pid and _is_alive(pid):
            logger.info("[WATCHDOG] Killing PID=%d for %s", pid, process_name)
            _kill_pid(pid)
            time.sleep(2)   # brief pause to let OS reclaim the PID

        # Step 3: Launch fresh process
        new_pid = _launch_process(process_name)
        if new_pid:
            _update_pidfile(process_name, new_pid)
            msg = f"[WATCHDOG] {process_name} restarted → PID={new_pid}"
            logger.info(msg)
            _tg(f"✅ {msg}")
        else:
            msg = f"[WATCHDOG] FAILED to restart {process_name} — check logs"
            logger.error(msg)
            _tg(f"❌ {msg}")

    # Self-heartbeat so we're visible in the dashboard
    _hb("watchdog", f"checked {len(statuses)} agents")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    once = "--once" in sys.argv

    ensure_table()
    logger.info("[WATCHDOG] Started — poll=%ds stale_threshold=%ds", POLL_SECS, STALE_SECS)
    _tg(f"Heartbeat watchdog started. Monitoring {len(AGENT_TO_PROCESS)} agents every {POLL_SECS}s.")

    try:
        while True:
            try:
                _check_once()
            except Exception as e:
                logger.error("[WATCHDOG] Unexpected error in check: %s", e, exc_info=True)

            if once:
                break
            time.sleep(POLL_SECS)

    except KeyboardInterrupt:
        set_stopped("watchdog")
        logger.info("[WATCHDOG] Stopped (Ctrl+C).")
        _tg("Heartbeat watchdog stopped.")


if __name__ == "__main__":
    main()
