# Role: Strategy Architect
You are building a modular trading bot for Krishna Prasanna Mallela.

## Architecture Rules
1. **Never** modify the `SL monitor` project files.
2. All strategy calculations must reside in `strategies/`.
3. Use the `kite_config.json` in `config/` for all credentials.
4. If a signal is generated, always verify the `generated_at` timestamp in the config to ensure the session hasn't expired.

## Strategy Definition
- **Entry:** 9 EMA crosses above 21 EMA AND Price is above the Support Zone.
- **Exit:** 9 EMA crosses below 21 EMA OR Price hits the 15:25 IST hard-cutoff.


# Agentic Trading System Architecture

## 1. Signal Monitor (Agent 1)
- **Role**: Fetch 5M and 15M data, and INDIAVIX.
- **Interval**: 300s (runs continuously).
- **Task**: Calculate 15M/21-candle support. Log signals to `signal_table` (preventing duplicates) if conditions met. Stores INDIAVIX data in `vix_history`.

## 2. Order Placer (Agent 2)
- **Role**: Watch `signal_table` for `order_placed = 'N'`.
- **Task**: Execute Kite orders (FUT/Options) or simulate orders in Paper Trading Mode. Update `order_table` with `broker_order_id`. Stops after 20 failed attempts.

## 3. SL Monitor (Agent 3)
- **Role**: Track active `broker_order_id` from `order_table`.
- **Task**: Place SL, trail every 30s, log P/L to `order_tracker`.

## Database Schema
- SQLite: `trading_system.db`.
- Tables: `signal_table`, `order_table`, `order_tracker`, `vix_history`, `system_config`.


# Tri-Agent System Requirements

## Agent 1: Signal Monitor
- Timeframes: 5M (Indicators) & 15M (Major S/R).
- Database: Insert new rows to `signal_table` with `order_placed='N'`, avoiding duplicates within a 5-minute window.
- Data: Fetches INDIAVIX and stores it in `vix_history`.

## Agent 2: Order Placer
- Function: Fetch ATM strike based on `nifty_spot`.
- Database: Poll `signal_table`. Upon execution, update `order_placed='Y'` and populate `order_table`.
- Modes: Supports "Paper Trading" (simulated orders) and "Actual Trading" (real Kite orders), configurable via the Dashboard or `set_trade_mode.py`.
- Resilience: Stops after 20 failed order placement attempts.

## Agent 3: SL Monitor (Ref: sl_monitor_with_trailing_ATR_v2.py)
- Features: ATR-based initial SL, 3-stage trailing logic, and forced session exit.
- Tracking: Log all closed trades into `order_tracker` with final P/L.
- Resilience: State reconciliation to detect unprotected positions after a crash.

## Additional Features
- **Paper Trading Mode:** A configurable option allowing users to simulate trades without real money.
- **India VIX Integration:** Fetches and stores INDIAVIX data, displayed on the Dashboard with a "Fear Gauge" (VIX < 15: Low Fear, 15-20: Medium Fear, > 20: High Fear).
- **Project Documentation:** `PROJECT_DOCS.md` provides comprehensive information about the system.
- **Handover Document:** `HANDOVER_DOC.md` facilitates onboarding for new team members.
