"""
setup_eod_schedule.py
---------------------
Registers (or updates) a Windows Task Scheduler task that runs eod_report.py
at 20:00 IST every Monday–Friday.

Run ONCE to install the schedule (requires no admin rights for current-user tasks):
    python setup_eod_schedule.py

To remove the task later:
    python setup_eod_schedule.py --remove
"""

import subprocess
import sys
from pathlib import Path

TASK_NAME   = "DhanEODReport"
PYTHON_EXE  = r"C:\ProgramData\anaconda3\python.exe"
SCRIPT_PATH = str(Path(__file__).parent / "eod_report.py")
RUN_TIME    = "20:00"          # 8 PM IST (local clock)
DAYS        = "MON,TUE,WED,THU,FRI"


def task_exists() -> bool:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True, text=True
    )
    return result.returncode == 0


def create_task():
    """Create or replace the scheduled task."""
    # Delete if already exists so we can recreate cleanly
    if task_exists():
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=True)
        print(f"  Removed existing task '{TASK_NAME}'")

    cmd = [
        "schtasks", "/Create",
        "/TN",  TASK_NAME,
        "/TR",  f'"{PYTHON_EXE}" "{SCRIPT_PATH}"',
        "/SC",  "WEEKLY",
        "/D",   DAYS,
        "/ST",  RUN_TIME,
        "/RL",  "HIGHEST",          # run with highest available privilege
        "/F",                        # force overwrite
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  Task '{TASK_NAME}' scheduled successfully.")
        print(f"  Runs at {RUN_TIME} every {DAYS}")
        print(f"  Script: {SCRIPT_PATH}")
        print(f"  Reports saved to: MasterConfiguration\\logs\\eod_report_YYYY-MM-DD_HH-MM-SS.txt")
    else:
        print("  ERROR creating task:")
        print(result.stderr or result.stdout)
        sys.exit(1)


def remove_task():
    if not task_exists():
        print(f"  Task '{TASK_NAME}' does not exist — nothing to remove.")
        return
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=True)
    print(f"  Task '{TASK_NAME}' removed.")


def show_task():
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        # Print only the relevant lines
        for line in result.stdout.splitlines():
            if any(k in line for k in ("TaskName", "Next Run", "Last Run", "Status",
                                        "Schedule Type", "Start Time", "Days")):
                print(" ", line.strip())
    else:
        print(f"  Task '{TASK_NAME}' not found.")


if __name__ == "__main__":
    if "--remove" in sys.argv:
        remove_task()
    elif "--status" in sys.argv:
        show_task()
    else:
        print("=" * 60)
        print("  Dhan EOD Report — Windows Task Scheduler Setup")
        print("=" * 60)
        create_task()
        print()
        print("  Current task status:")
        show_task()
        print("=" * 60)
        print()
        print("  To verify:  python setup_eod_schedule.py --status")
        print("  To remove:  python setup_eod_schedule.py --remove")
        print("  Manual run: python eod_report.py")
        print()
