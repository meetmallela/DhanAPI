# 📊 Dhan Unified Strategy Ecosystem

This document outlines the strategies integrated into the **Dhan Omni-Engine** for paper trading in the Sandbox environment. All strategies are logged into the centralized `trading.db` in the `MasterConfiguration` directory.

---

## 1. EMA 9/21 Crossover
Standard trend-following strategy designed to capture momentum during strong market moves.

*   **Instrument Traded**: NIFTY 50 (Spot or ATM Options)
*   **Primary Timeframe**: 5-Minute
*   **Entry Signal**:
    *   **BULLISH**: 9 EMA crosses **above** 21 EMA.
    *   **BEARISH**: 9 EMA crosses **below** 21 EMA.
*   **Exit Signal**:
    *   Opposite crossover (Trend Reversal).
    *   Fixed % Stop Loss (default 5%) or Target managed by `dhan_sl_monitor.py`.

---

## 2. Pair Leadership Strategy
Uses the concept of "Market Leaders" where the two heavyweights of the Nifty 50 (RELIANCE & HDFC Bank) dictate the direction of the index.

*   **Instruments Traded**: NIFTY 50
*   **Leader Instruments**: RELIANCE & HDFC Bank
*   **Timeframe**: 1-Minute or 5-Minute
*   **Entry Signal**:
    *   **BULLISH**: BOTH Reliance and HDFC are trading **above** their Daily VWAP AND have broken the high of the last 5 candles.
    *   **BEARISH**: BOTH Reliance and HDFC are trading **below** their Daily VWAP AND have broken the low of the last 5 candles.
*   **Exit Signal**:
    *   One of the leaders breaks the bias (e.g., Reliance falls below VWAP while in a Bullish trade).
    *   Structure-based Stop Loss managed by `dhan_sl_monitor.py`.

---

## 3. High Conviction Option Scalper (EMA 44)
A multi-timeframe strategy designed for quick option buying with high probability filters.

*   **Instrument Traded**: NIFTY 50 Options (Dynamic Strike Selection: ATM/ITM)
*   **Timeframe**: 5-Minute (Signal) + 15-Minute (MTF Trend Alignment)
*   **Entry Signal**:
    *   **MTF Filter**: 15-minute price must be on the same side of the 44 EMA as the signal.
    *   **BULLISH**: 5-minute price crosses **above** 44 EMA while 15-minute trend is Bullish.
    *   **BEARISH**: 5-minute price crosses **below** 44 EMA while 15-minute trend is Bearish.
*   **Exit Signal**:
    *   5-minute price closes back across the 44 EMA.
    *   Dynamic ATR-based Trailing SL (managed by the system).

---

## 4. Telegram-Driven Signal Bridge
A passive execution engine that converts expert alerts from Telegram into automated Dhan orders.

*   **Instruments Traded**: NIFTY / BANKNIFTY / FINNIFTY Options
*   **Source**: Integrated with `yaatra_parser` and `channel_parsers`.
*   **Entry Signal**:
    *   Automated parsing of Telegram messages for keywords (e.g., "BUY NIFTY 22000 CE", "ENTRY ABOVE 150").
    *   Signals are validated against the `MasterConfiguration` rules before execution.
*   **Exit Signal**:
    *   Stop Loss and Targets provided in the Telegram message.
    *   Automated 3-Stage ATR Trailing SL (Stage 1: Breakeven, Stage 2: Locking Profit, Stage 3: Max Trailing).

---

## 🛠 System Components
*   **`DhanOmniEngine.py`**: The central hub that runs all strategies in parallel.
*   **`dhan_order_placer.py`**: Handles market order execution on the Dhan Sandbox.
*   **`dhan_sl_monitor.py`**: Provides real-time trailing and exit management for all active positions.
*   **`MasterConfiguration/`**: Houses all shared logs, configs, and the `trading.db`.
