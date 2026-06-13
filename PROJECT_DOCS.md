# 🤖 Agentic Trading System Documentation

## 1. Project Overview
This is a modular, tri-agent trading system built for **Krishna Prasanna Mallela**. The system uses a 5M EMA Crossover strategy (9 EMA / 21 EMA) combined with 15M Support/Resistance validation to trade Nifty options.

## 2. Architecture (The Three Agents)

### Agent 1: Signal Monitor (`main.py`)
- **Frequency:** Runs every 30 seconds.
- **Strategy:** 9 EMA crosses 21 EMA + Price relative to 15M Support/Resistance.
- **VIX Monitor:** Fetches India VIX and tracks fear levels.
- **Duplicate Prevention:** Won't trigger the same signal type within a 5-minute window.

### Agent 2: Order Placer (`core/order_placer.py`)
- **Modes:** 
    - **Paper Trading:** Simulates orders in the database without spending money.
    - **Live Trading:** Places actual orders via Kite Connect API.
- **Strike Selection:** ATM (At-The-Money) strikes from `data/valid_instruments.csv`.
- **Resilience:** Stops automatically after 20 consecutive failed attempts.

### Agent 3: SL Monitor (`core/sl_monitor_agent.py`)
- **Role:** Tracks active positions and manages Stop Loss.
- **Trailing:** ATR-based trailing logic (Stage 1, 2, and 3).
- **Session Cutoff:** Forces exit at 15:25 IST.

## 3. Database Schema (`trading_system.db`)
- `signal_table`: Logs all generated entry signals.
- `order_table`: Logs entries (Real or Paper). Includes `option_entry_price` for accurate P/L analysis.
- `order_tracker`: Tracks P/L and SL executions.
- `vix_history`: Historical India VIX data.
- `system_config`: Global settings (e.g., Paper Trading Toggle).
- `system_status`: Agent heartbeats for the dashboard.

**Stability Features:**
- **WAL Mode:** Write-Ahead Logging is enabled to allow concurrent database access.
- **Connection Timeouts:** All agents use a 30-second timeout to prevent "Database is locked" errors.

## 4. Dashboard (`dashboard.py`)
- **Master Control:** Start/Stop all agents with one click.
- **Fear Gauge:** Real-time India VIX sentiment (Low/Medium/High Fear).
- **Live Monitoring:** Real-time view of signals, orders, and realized P/L.
- **Mode Toggle:** Switch between Paper and Live trading instantly via the sidebar.

## 5. Setup & Requirements
- **Python:** 3.8+ (Anaconda environment recommended).
- **API:** Kite Connect API credentials in `config/kite_config.json`.
- **Data:** Instruments must be listed in `data/valid_instruments.csv`.
