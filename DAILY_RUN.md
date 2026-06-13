# 📋 Daily Run & Handover Document

## Daily Morning Routine
1.  **Start Dashboard:** Run `start_dashboard.bat`.
2.  **Verify Mode:** Check the sidebar to ensure you are in **Paper Trading** or **Live Trading** as intended.
3.  **Launch Agents:** Click **"🚀 Start All Systems"**.
4.  **Check Indicators:** Ensure "Signal Monitor" status turns **🟢 ONLINE**.

## Monitoring During Market Hours
-   **Signal Logs:** Watch the "Signal Logs" tab for entry alerts.
-   **Fear Gauge:** Monitor the VIX Fear Gauge. If VIX is > 15 (High Fear), volatility might be high.
-   **Heartbeats:** If an agent status turns **🔴 OFFLINE**, click its individual "Start" button to reboot it.

## Troubleshooting
-   **Instrument Error:** Ensure the `data/valid_instruments.csv` is updated for the current week.
-   **Order Placer Stalled:** If the Order Placer stops after 20 attempts, check the logs in `logs/order_placer.log` for API errors (e.g., margins, tokens).
-   **Database Locked:** If you see "database is locked" errors, wait a few seconds; the system now has auto-retry and WAL mode to handle this automatically.

## System Shutdown
-   **Market Close:** Click **"🛑 Stop All Systems"** at 15:30 IST.
-   The SL Monitor will automatically try to square off positions at 15:25 IST.
