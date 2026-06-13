"""
start_trading_system.py
-----------------------
Starts all 5 trading system components as silent background processes.
No terminal windows — all output goes to log files.

Usage:
    python start_trading_system.py          # start all
    python start_trading_system.py status   # check what's running
    python start_trading_system.py stop     # stop all
"""

import sys
import os
import time
import json
import subprocess
import signal
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime

# ── Paths ──────────────────────────────────────────────────────────────────────
DIR     = Path(__file__).parent
PYEXE   = Path(r"C:\ProgramData\anaconda3\pythonw.exe")   # windowless Python
PIDFILE = DIR / ".trading_pids.json"

# ── Process definitions (name, script, startup delay seconds) ─────────────────
PROCESSES = [
    ("TG Reader",       "telegram_reader_production.py",  3),
    ("Order Placer",    "order_placer_dhan_sandbox.py",   2),
    ("SL Monitor",      "dhan_sl_monitor.py",             2),
    ("OmniEngine",      "DhanOmniEngine_v2.py --force",   3),   # --force kills any zombie lock holder
    ("Dashboard",       "dhan_dashboard.py",              2),
    ("EOD WhatIf",      "eod_whatif_backtest.py schedule",1),  # runs at 16:00 IST daily
    ("Health Monitor",  "health_monitor.py",              2),   # deep checks: Kite/Claude/signals
    ("Swing Agent",     "agents/swing_agent.py",          2),   # daily EOD scan + paper positions
    ("Watchdog",        "watchdog.py",                    4),   # heartbeat-based hung-process detector
    ("Strangle Bot",   "kite_strangle/main.py --lots 1", 2),  # Kite NIFTY delta-neutral strangle (daemon)
]
# Note: research_engine_v2.py is launched manually or from the dashboard
# (not part of the auto-start system -- keeps research separate from live trading)

# Script stems used by _kill_all_trading_processes() to identify our processes
_TRADING_SCRIPT_STEMS = [
    "telegram_reader_production",
    "order_placer_dhan_sandbox",
    "dhan_sl_monitor",
    "DhanOmniEngine_v2",
    "DhanOmniEngine",
    "dhan_dashboard",
    "eod_whatif_backtest",
    "health_monitor",
    "swing_agent",
    "watchdog",
    "kite_strangle",
]

ENGINE_LOCK = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\dhan_omni_engine.lock")

# ── Windows process creation flags ────────────────────────────────────────────
DETACHED_PROCESS    = 0x00000008
CREATE_NO_WINDOW    = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _kill_all_trading_processes():
    """
    Forcefully kill every running instance of every trading script,
    regardless of whether it appears in the PID file.
    Also cleans up the PID file and engine lock file.
    """
    print()
    print("=" * 55)
    print("  FORCE STOP — hunting all trading processes...")
    print("=" * 55)

    my_pid  = os.getpid()
    killed  = []
    failed  = []

    try:
        import psutil

        # Pass 1: terminate (SIGTERM)
        targets = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.pid == my_pid:
                    continue
                cmdline = " ".join(proc.info.get('cmdline') or [])
                if any(s.lower() in cmdline.lower() for s in _TRADING_SCRIPT_STEMS):
                    targets.append(proc)
                    try:
                        proc.terminate()
                        killed.append((proc.pid, cmdline[:70]))
                    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                        failed.append((proc.pid, str(e)))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        if targets:
            try:
                gone, alive = psutil.wait_procs(targets, timeout=3)
            except psutil.AccessDenied:
                # Some elevated processes can't be waited on — skip them
                alive = [p for p in targets if p.is_running()]
            # Pass 2: force-kill anything still alive
            for proc in alive:
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    except ImportError:
        # psutil not available — fall back to wmic + taskkill
        seen_pids: set = set()
        for stem in _TRADING_SCRIPT_STEMS:
            try:
                result = subprocess.run(
                    ["wmic", "process", "where",
                     f"CommandLine like '%{stem}%'",
                     "get", "ProcessId"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if not line.isdigit():
                        continue
                    pid = int(line)
                    if pid == my_pid or pid in seen_pids:
                        continue
                    seen_pids.add(pid)
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/PID", str(pid)],
                            capture_output=True, timeout=3,
                        )
                        killed.append((pid, stem))
                    except Exception as e:
                        failed.append((pid, str(e)))
            except Exception:
                pass
        time.sleep(2)

    # Report
    if killed:
        for pid, cmd in killed:
            print(f"  [KILL]  PID={pid}  {cmd.strip()}")
    else:
        print("  [OK]    No orphan trading processes found")

    if failed:
        for pid, err in failed:
            print(f"  [WARN]  PID={pid}  could not kill: {err}")

    # Clean up PID file and engine lock
    if PIDFILE.exists():
        PIDFILE.unlink(missing_ok=True)
        print("  [CLEAN] PID file removed")

    if ENGINE_LOCK.exists():
        ENGINE_LOCK.unlink(missing_ok=True)
        print("  [CLEAN] Engine lock file removed")

    print("=" * 55)
    print()


def _start_all():
    # Always do a clean kill of any prior instances before starting fresh
    _kill_all_trading_processes()

    pids = {}
    print()
    print("=" * 55)
    print("  TRADING SYSTEM — starting background processes")
    print("=" * 55)

    for name, script, delay in PROCESSES:
        # script may include args, e.g. "eod_whatif_backtest.py schedule"
        parts       = script.split()
        script_path = DIR / parts[0]
        extra_args  = parts[1:]

        if not script_path.exists():
            print(f"  [SKIP] {name} — {parts[0]} not found")
            continue

        try:
            log_dir = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            stderr_log = log_dir / f"{script_path.stem}_stderr_{datetime.now().strftime('%d%b%Y_%H_%M_%S')}.log"
            stderr_fh  = open(stderr_log, "w")
            proc = subprocess.Popen(
                [str(PYEXE), str(script_path)] + extra_args,
                cwd        = str(DIR),
                creationflags = DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                close_fds  = False,
                stdout     = stderr_fh,
                stderr     = stderr_fh,
            )
            pids[name] = proc.pid
            print(f"  [OK]   {name:<16} PID={proc.pid}")
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")

        time.sleep(delay)

    # Save PIDs
    PIDFILE.write_text(json.dumps({"started": datetime.now().isoformat(), "pids": pids}, indent=2))

    print()
    print("=" * 55)
    print(f"  {len(pids)} processes running in background")
    print(f"  Logs  : MasterConfiguration/logs/")
    print(f"  Dashboard: http://127.0.0.1:5050")
    print(f"  Stop  : python start_trading_system.py stop")
    print("=" * 55)
    print()


def _status():
    if not PIDFILE.exists():
        print("\n  No PID file found — system not started via this launcher.\n")
        return

    data  = json.loads(PIDFILE.read_text())
    pids  = data.get("pids", {})
    start = data.get("started", "?")

    print()
    print("=" * 55)
    print(f"  TRADING SYSTEM STATUS  (started {start[:19]})")
    print("=" * 55)

    all_alive = True
    for name, pid in pids.items():
        alive = _is_alive(pid)
        dot   = "[*]" if alive else "[ ]"
        state = "RUNNING" if alive else "STOPPED"
        print(f"  {dot}  {name:<16} PID={pid}  {state}")
        if not alive:
            all_alive = False

    print()
    if all_alive:
        print("  All processes running.")
    else:
        print("  Some processes have stopped — check logs for errors.")
    print(f"  Dashboard: http://127.0.0.1:5050")
    print("=" * 55)
    print()


def _stop():
    # Deep kill: catches zombies from old sessions that aren't in the PID file
    _kill_all_trading_processes()
    print("  All trading processes stopped.")
    print()


def _is_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        pass
    # Fallback: use tasklist on Windows
    try:
        import subprocess
        out = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], text=True, stderr=subprocess.DEVNULL)
        return str(pid) in out
    except Exception:
        return False


_TG_TOKEN   = "8155923389:AAEIfjjaJNA_57zqn2czgZoTWpqcKKFxwTU"
_TG_CHAT_ID = "494844168"


def _tg(msg: str):
    """Send a Telegram message. Silently swallows failures."""
    try:
        url  = f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": _TG_CHAT_ID, "text": msg}).encode()
        urllib.request.urlopen(url, data=data, timeout=5)
    except Exception:
        pass   # never let a notification failure break the watchdog


def _market_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    return (9, 15) <= (h, m) <= (15, 30)


def _launch_one(name, script):
    """Launch a single process, return (pid, log_path) or (None, None) on failure."""
    parts       = script.split()
    script_path = DIR / parts[0]
    extra_args  = parts[1:]
    if not script_path.exists():
        return None, None
    try:
        log_dir    = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        stderr_log = log_dir / f"{script_path.stem}_stderr_{datetime.now().strftime('%d%b%Y_%H_%M_%S')}.log"
        fh = open(stderr_log, "w")
        proc = subprocess.Popen(
            [str(PYEXE), str(script_path)] + extra_args,
            cwd=str(DIR),
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=False, stdout=fh, stderr=fh,
        )
        return proc.pid, stderr_log
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        return None, None


def _watchdog():
    """
    Foreground watchdog — checks every 30s and restarts any dead process.
    Run:  python start_trading_system.py watchdog
    Stop: Ctrl+C
    """
    print()
    print("=" * 55)
    print("  WATCHDOG — monitoring + auto-restart (Ctrl+C to stop)")
    print("=" * 55)

    # Bootstrap: start everything if not running
    if not PIDFILE.exists():
        _start_all()

    # Read PIDFILE — guard against empty file (race condition on first write)
    try:
        raw = PIDFILE.read_text().strip()
        data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    if not data:
        # PIDFILE missing or empty — start fresh
        _start_all()
        try:
            raw = PIDFILE.read_text().strip()
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
    pids = data.get("pids", {})

    script_map = {name: script for name, script, _ in PROCESSES}
    delay_map  = {name: delay  for name, script, delay in PROCESSES}

    _tg(f"Trading system watchdog started. All {len(PROCESSES)} processes running.")
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 5 * 60    # 5 min

    try:
        while True:
            changed   = False
            now_str   = datetime.now().strftime("%H:%M:%S")
            dead_list = []

            for name, script in script_map.items():
                pid = pids.get(name)
                if pid and _is_alive(pid):
                    continue  # healthy
                dead_list.append(name)

            if dead_list:
                alert = "ALERT: Process(es) crashed: " + ", ".join(dead_list)
                print(f"  [{now_str}] {alert}")
                _tg(alert + "\nAttempting restart...")

                for name in dead_list:
                    time.sleep(delay_map.get(name, 2))
                    new_pid, _ = _launch_one(name, script_map[name])
                    if new_pid:
                        pids[name] = new_pid
                        changed = True
                        msg = f"Restarted {name} -> PID={new_pid}"
                        print(f"  [{now_str}]  {msg}")
                        _tg(msg)
                    else:
                        _tg(f"FAILED to restart {name}. Check logs.")

            if changed:
                data["pids"] = pids
                PIDFILE.write_text(json.dumps(data, indent=2))

            # Heartbeat during market hours every 30 min
            if _market_hours() and (time.time() - last_heartbeat) >= HEARTBEAT_INTERVAL:
                alive = [n for n, p in pids.items() if _is_alive(p)]
                _tg(f"Watchdog heartbeat {datetime.now().strftime('%H:%M')}: {len(alive)}/{len(PROCESSES)} processes running. All clear.")
                last_heartbeat = time.time()

            time.sleep(30)

    except KeyboardInterrupt:
        print("\n  Watchdog stopped.")
        _tg("Trading system watchdog stopped (Ctrl+C).")


_TASK_NAME = "TradingSystemAutoStart"
_START_TIME = "09:05"   # IST — system clock must be in IST


def _install_scheduler():
    """Register a Windows Task Scheduler job to start the watchdog at 09:05 Mon-Fri."""
    import subprocess
    pyexe  = str(PYEXE)
    script = str(DIR / "start_trading_system.py")
    cmd = [
        "schtasks", "/Create",
        "/TN", _TASK_NAME,
        "/TR", f'"{pyexe}" "{script}" watchdog',
        "/SC", "WEEKLY",
        "/D", "MON,TUE,WED,THU,FRI",
        "/ST", _START_TIME,
        "/RL", "HIGHEST",
        "/F",   # overwrite if exists
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"\n  [OK] Task '{_TASK_NAME}' scheduled at {_START_TIME} IST Mon-Fri")
        print(f"       watchdog will auto-start the trading system before market open.")
        print(f"       Verify: schtasks /Query /TN {_TASK_NAME}\n")
    else:
        print(f"\n  [FAIL] schtasks error: {result.stderr.strip()}")
        print("  Run this script as Administrator if you get an access-denied error.\n")


def _uninstall_scheduler():
    """Remove the auto-start Task Scheduler job."""
    import subprocess
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", _TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"\n  [OK] Task '{_TASK_NAME}' removed.\n")
    else:
        print(f"\n  [FAIL] {result.stderr.strip()}\n")


if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "start"

    if cmd == "start":
        _start_all()
    elif cmd == "restart":
        # Hard stop (deep kill) then fresh start — the recommended way to restart
        _start_all()
    elif cmd == "status":
        _status()
    elif cmd == "stop":
        _stop()
    elif cmd == "watchdog":
        _watchdog()
    elif cmd == "schedule":
        _install_scheduler()
    elif cmd == "unschedule":
        _uninstall_scheduler()
    else:
        print(f"Unknown command '{cmd}'. Use: start | restart | status | stop | watchdog | schedule | unschedule")
