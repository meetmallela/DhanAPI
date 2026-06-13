"""
tunnel_manager.py
-----------------
Manages the Cloudflare quick tunnel for the Dhan Dashboard.

On start:
  1. Kills any stale cloudflared processes
  2. Starts cloudflared tunnel pointing at localhost:5050
  3. Extracts the trycloudflare.com URL from the log
  4. Sends the URL to Telegram so you always have it on your phone
  5. Writes URL to tunnel_url.txt for local reference
  6. Monitors cloudflared and restarts it if it crashes

Run:
    python tunnel_manager.py          (foreground, with console output)
    pythonw tunnel_manager.py         (background, silent -- used by Task Scheduler)

Auto-start:
    Registered as "DhanDashboardTunnel" Task Scheduler job (run as admin once):
    schtasks /Create /TN "DhanDashboardTunnel"
             /TR "C:\\ProgramData\\anaconda3\\pythonw.exe
                  C:\\Users\\meetm\\OneDrive\\Desktop\\GCPPythonCode\\DhanAPI\\tunnel_manager.py"
             /SC ONLOGON /F
"""

import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

_ROOT      = Path(__file__).parent
_LOG_FILE  = _ROOT / "cloudflared_tunnel.log"
_URL_FILE  = _ROOT / "tunnel_url.txt"
_CLOUDFLARED = _ROOT / "cloudflared.exe"

# Telegram config (same bot the trading system uses)
_TG_TOKEN   = "8155923389:AAEIfjjaJNA_57zqn2czgZoTWpqcKKFxwTU"
_TG_CHAT_ID = "494844168"

# Logging to file (pythonw has no console)
_MANAGER_LOG = Path("C:/Users/meetm/OneDrive/Desktop/GCPPythonCode/MasterConfiguration/logs") / \
               f"tunnel_manager_{datetime.now().strftime('%d%b%Y')}.log"
_MANAGER_LOG.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TUNNEL] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(str(_MANAGER_LOG), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("tunnel_manager")


# ── Telegram notification ─────────────────────────────────────────────────────

def _send_telegram(msg: str):
    try:
        url  = f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": _TG_CHAT_ID, "text": msg}).encode()
        urllib.request.urlopen(url, data=data, timeout=10)
        logger.info("Telegram notification sent")
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


# ── Kill stale cloudflared processes ─────────────────────────────────────────

def _kill_existing():
    try:
        subprocess.run(["taskkill", "/IM", "cloudflared.exe", "/F"],
                       capture_output=True)
        time.sleep(2)
        logger.info("Killed any existing cloudflared processes")
    except Exception:
        pass


# ── Extract tunnel URL from log ───────────────────────────────────────────────

def _wait_for_url(timeout_secs: int = 30) -> str | None:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        try:
            content = _LOG_FILE.read_text(errors="replace")
            urls = re.findall(r"https://[a-z0-9\-]+\.trycloudflare\.com", content)
            if urls:
                return urls[-1]
        except Exception:
            pass
        time.sleep(1)
    return None


# ── Start cloudflared ─────────────────────────────────────────────────────────

def _start_cloudflared() -> subprocess.Popen:
    # Clear old log
    try:
        _LOG_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    proc = subprocess.Popen(
        [str(_CLOUDFLARED), "tunnel", "--url", "http://localhost:5050",
         "--logfile", str(_LOG_FILE)],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    logger.info(f"cloudflared started (pid={proc.pid})")
    return proc


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 55)
    logger.info("  Dhan Dashboard Tunnel Manager starting")
    logger.info("=" * 55)

    _kill_existing()

    restart_count = 0

    while True:
        restart_count += 1
        logger.info(f"Starting cloudflared (attempt #{restart_count})")

        proc = _start_cloudflared()

        # Wait for URL
        url = _wait_for_url(timeout_secs=30)

        if url:
            logger.info(f"Tunnel URL: {url}")

            # Save to file
            _URL_FILE.write_text(url, encoding="utf-8")

            # Send to Telegram
            now_str = datetime.now().strftime("%d %b %Y %H:%M IST")
            msg = (
                f"Dhan Dashboard is LIVE\n"
                f"URL: {url}\n"
                f"Login: dhan / (your password)\n"
                f"Time: {now_str}"
            )
            _send_telegram(msg)
        else:
            logger.warning("Could not extract tunnel URL within 30s")
            _send_telegram("Dashboard tunnel started but URL not detected. Check tunnel_url.txt")

        # Monitor the process -- restart if it crashes
        while True:
            ret = proc.poll()
            if ret is not None:
                logger.warning(f"cloudflared exited (code={ret}) -- restarting in 15s")
                time.sleep(15)
                break
            time.sleep(10)


if __name__ == "__main__":
    main()
