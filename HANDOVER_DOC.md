# 🤖 Daily Run Handover Document: Trading System v2.0

## 1. System Architecture (Managed Service)
This system has evolved into a managed service model where a **Master Watchdog** maintains the health of all secondary agents.

## 2. Updated Agent Ecosystem

*   **Watchdog Master (`watchdog.py`)**:
    *   **Role:** The system maintainer. Monitors heartbeats of all agents every 30s. Automatically restarts any agent that hangs for >300s.
*   **Signal Monitor (`main.py`)**: Handles index scanning and EMA strategy detection. Now uses centralized config.
*   **Order Placer (`core/order_placer.py`)**: Polls for signals. ITM strike selection logic active on expiry days.
*   **SL Monitor (`core/sl_monitor_agent.py`)**: 
    *   **New:** Robust instance locking and price sanity checks.
    *   **New:** Tracks mathematical vs structural SL hits for research.
*   **Dashboard v2.0 (`dashboard.py`)**:
    *   **New:** Management interface to Start/Stop individual agents.
    *   **New:** Strategy Lab for P/L comparison.
    *   **New:** Integrated system logs tab.

## 3. Automation & Control

### Silent Startup (Recommended)
1.  **Run `invis_launcher.vbs`**: This launches the system invisibly in the background.
2.  **Startup Folder**: Place a shortcut to `invis_launcher.vbs` in the Windows Startup folder to make the bot start on PC boot.

### The Dashboard (Control Center)
*   **Performance Monitoring**: View cumulative P/L curves and win rates.
*   **Agent Management**: Use the toggle buttons under "Agent Manager" to control individual processes.
*   **Mode Switch**: Toggle Paper/Live trading from the sidebar.

## 4. Maintenance & Safety
- **Singleton Locks**: If an agent fails to start manually, check for `.lock` files in the root folder.
- **Price Sanity**: If you see `[!!!] PRICE SANITY ALERT` in the logs, the API returned a price glitch; the agent skipped the cycle safely.
- **Heartbeat Status**:
    - `🟢 ONLINE`: Agent is running and updating DB.
    - `🟡 WARMUP`: Agent recently started and fetching historical data.
    - `🔴 OFFLINE`: Agent has not reported heartbeats for 5+ minutes.

## 5. Directory Mapping
- **Credentials**: `C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\kite_config.json`
- **Database**: `trading_system.db`
- **Visual Utilities**: `utils/visuals.py` and `utils/process_control.py`

---
*Prepared by: Gemini Multi-Agent System Upgrade*
*Last Update: March 03, 2026*


---
*Prepared for: Daily Handover*
*Date: February 20, 2026*
