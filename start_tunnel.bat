@echo off
REM start_tunnel.bat -- Cloudflare tunnel for Dhan Dashboard
REM Starts cloudflared and writes the public URL to tunnel_url.txt
REM Run manually or via Task Scheduler at login

cd /d "C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI"

REM Kill any existing cloudflared instances first
taskkill /IM cloudflared.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul

REM Start tunnel and capture URL
start /B cloudflared.exe tunnel --url http://localhost:5050 --logfile cloudflared_tunnel.log

REM Wait for URL to appear in log, then write to tunnel_url.txt
timeout /t 12 /nobreak >nul
python -c "
import re, pathlib, time
for _ in range(10):
    try:
        log = pathlib.Path('cloudflared_tunnel.log').read_text(errors='replace')
        urls = re.findall(r'https://[a-z0-9\-]+\.trycloudflare\.com', log)
        if urls:
            url = urls[-1]
            pathlib.Path('tunnel_url.txt').write_text(url)
            print('Tunnel URL:', url)
            break
    except Exception:
        pass
    time.sleep(2)
"
