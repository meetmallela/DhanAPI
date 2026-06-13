# System Assessment Report: Dhan API Options & Futures Trading System
**Date of Assessment:** May 29, 2026  
**Status:** Confidential - Production System Audit  
**Author:** Senior Trading Systems Architect & Financial Platform Security Expert  

---

## 1. Executive Summary

This report presents a thorough, independent, and highly critical architectural and security assessment of the automated trading system located at `C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI`. 

### Core System Overview
The system is a production-grade, highly modular, multi-process algorithmic options and futures trading platform built for the Indian markets (NSE/BSE/MCX). The system comprises:
1. **Telegram Ingestion:** Plaintext and conversational natural language signal scraping via a custom Telegram Client (`telegram_reader_production.py`) parsing across 20+ channels using rule-based and LLM-assisted (Claude Haiku) extractors.
2. **Strategy Generation & Orchestration:** The core engines—`DhanOmniEngine.py` (v1) and the heavily agentic `DhanOmniEngine_v2.py` (v2)—managing an aggressive roster of 28 strategy workers in parallel.
3. **Intelligence & Filtering:** LLM and vector database matching (`MetaAgent` using ChromaDB and Claude API) for signal verification against historical trade outcomes.
4. **Execution & Placing:** Live paper trading via Dhan Sandbox APIs, backed by dynamic pre-trade filters (OTM thresholds, premium-chase guards, VWAP slope checking, and index momentum rules) in `order_placer_dhan_sandbox.py`.
5. **Risk & Stop-Loss Management:** Dedicated process-level tracking (`dhan_sl_monitor.py`) executing dynamic, candle-close ATR-based stop losses, spot/option underlying checks, and time-based cutoffs.
6. **Dashboard Monitoring:** Flask-based real-time tracking (`dhan_dashboard.py`) powered by database-level snapshotting.

### General Design Evaluation
The platform shows highly advanced, innovative quantitative logic (particularly in its multi-layered filters and multi-timeframe confirmation rules). However, from a **Trading Infrastructure and Security Architecture** perspective, the system is in a **highly vulnerable and fragile state**. 

It exhibits critical design flaws that present severe operational risks, performance degradation under heavy trading hours, and significant risk of broker account exploitation due to credentials leakage.

> [!CAUTION]
> **Immediate Action Required:** A total of **6 high-severity plaintext credentials leaks** (including API Secrets, Claude LLM Keys, Kite Connect Access Tokens, and Telegram Bot Tokens with private Chat IDs) were discovered exposed directly in workspace directories and plain config files.

---

## 2. System Architecture & Data Flows

The system relies on a **SQLite-backed messaging architecture** where independent, detached processes communicate asynchronously by writing and reading state from tables in a shared database (`trading.db`).

### Detailed Process Flow Diagram
The following Mermaid flowchart maps the complete ingestion, intelligence, execution, risk monitoring, and dashboard monitoring pipeline:

```mermaid
flowchart TB
    %% Telegram Ingestion Channel
    subgraph TG_Ingestion ["1. Telegram Ingestion Pipeline (Process 1)"]
        A[Telegram Channels] -->|Scrapes Plaintext| B["telegram_reader_production.py"]
        B -->|Rule-Based / LLM Parsers| C["channel_parsers.py"]
        C -->|INSERT unprocessed signal| D[("trading.db (signals table)")]
    end

    %% Agentic Strategy Generation
    subgraph OmniEngine_V2 ["2. Strategy Generation Pipeline (Process 2: DhanOmniEngine_v2)"]
        E["DirectorAgent (Stage 1 & 2)"] -->|Injects Day Bias| F["DataAgent"]
        G[Kite / Dhan Real-time Feeds] -->|Feeds OHLCV Ticks| F
        F -->|Fans out MarketSnapshot| H["28 Strategy Workers (A-AD)"]
        H -->|Pushes signal events| I["signal_queue (In-memory)"]
        
        I -->|RAG / Similarity search| J["MetaAgent (ChromaDB + Claude API)"]
        J -->|Approved signals| K["approved_queue (In-memory)"]
        K -->|ExecutionAgent| L["engine.execute()"]
        L -->|INSERT internal signal| D
    end

    %% Execution and Sandbox Placing
    subgraph Order_Execution ["3. Order Placing Pipeline (Process 3)"]
        D -->|Polls processed=0| M["order_placer_dhan_sandbox.py"]
        M -->|Enforces OTM, VWAP, Chase, Lot Filters| N{Pre-Trade Filters Passed?}
        N -->|No| O["Mark processed=-1 (SKIPPED)"]
        N -->|Yes| P["DhanClient (Sandbox API)"]
        P -->|Places immediate MARKET order| Q[Dhan Sandbox Broker]
        P -->|INSERT order details status=OPEN| R[("trading.db (orders table)")]
        M -->|Mark processed=1 (PLACED)| D
    end

    %% Stop-Loss & Risk Management
    subgraph SL_Monitor ["4. Risk & SL Monitoring Pipeline (Process 4)"]
        R -->|Polls status=OPEN| S["dhan_sl_monitor.py"]
        T["Kite API (Primary LTP)"] -->|Batch LTP Update| S
        U["Dhan WebSocket (Secondary LTP)"] -->|Live Ticks| S
        S -->|Enforces min-hold & candle close| V{Trailing ATR SL Hit?}
        V -->|Yes| W["DhanClient.place_order() (MARKET exit)"]
        W -->|Square off contract| Q
        W -->|UPDATE status=CLOSED| R
        W -->|Record realised P&L| X["daily_pnl_state.json"]
    end

    %% Dashboard and Circular Bloat
    subgraph Dashboard_State ["5. Dashboard Pipeline (Process 5)"]
        Y["dhan_dashboard.py (Flask UI)"] -->|REST Poll every 10s| Z["Dhan API / Sandbox"]
        Z -->|Returns massive Sandbox history| Y
        Y -->|Dumps JSON blobs without pruning| AA[("dhan_dashboard.db (orders table)")]
        AA -->|1.2 GB circular bloat!| Y
        Y -->|Reads today P&L state| X
    end

    classDef db fill:#f9f,stroke:#333,stroke-width:2px;
    classDef process fill:#bbf,stroke:#333,stroke-width:2px;
    classDef caution fill:#fbb,stroke:#333,stroke-width:2px;
    class D,R,AA db;
    class B,M,S,Y,H,J process;
    class AA caution;
```

---

## 3. Deep-Dive Component Breakdown

### A. DhanOmniEngine v1 vs. v2
*   **DhanOmniEngine.py (v1):** Employs a basic multi-threaded runner executing 6 technical strategies (EMA 9/21, OptionScalper EMA 44, Supertrend MACD, EMA VWAP S/R, Opening Range Breakout, Pair Leadership) across 4 index assets. It queries the Kite API and databases sequentially, relying on simple execution logic.
*   **DhanOmniEngine_v2.py (v2):** Introduces a heavily agentic, RAG-integrated pipeline running **38 distinct threads** (26 strategy workers A-V + Y-AD, and 12 infrastructure daemons).
    *   **DirectorAgent:** Operates at 09:00 IST to generate a daily macro thesis using Claude Haiku (based on market gap %, India VIX, and a 5-day historical trend). It then validates it at 09:20 IST based on the first five 1-minute candle prints, injecting a `day_bias` into the platform.
    *   **MetaAgent (RAG Filter):** Employs ChromaDB to retrieve the most similar 10 past trades. If the current signal matches a high-win-rate historical setup (WR $\ge 40\%$), the entry barrier is lowered. If it contradicts, it raises the exit filter bar to WR $\ge 60\%$.
    *   **Specialist Daemon Workers:** Features dedicated algorithmic workers such as the `GammaBlastWorker` (ATM straddle execution on score $\ge 7$), `VIXStraddleWorker` (volatility expansion plays), `IronCondorWorker` (range-bound option writing), and `IntradayThetaDecayWorker` (Goldilocks straddle writing).

### B. Live SL Monitor (`dhan_sl_monitor.py`)
This component is highly advanced and acts as the defensive shield of the portfolio, checking stop losses exclusively on 1-minute candle closes (preventing whip-saws from minor intraday tick spikes).
*   **LTP Source Prioritization:** To mitigate sandbox data gaps, it queries (1) Kite Connect batch LTP (covering NFO/BFO/MCX), (2) Dhan WebSocket, (3) `kite_candles.db` latest close.
*   **Dynamic Trailing ATR Engine:** Implements initial ATR-based stops cap-locked by index vs. stock rules. On every tick, it updates `peak_price` in memory, trailing the stop-loss level behind profitable moves.
*   **Min-Hold Cooldown:** Enforces a minimum holding duration (e.g., 3-10 candles) during which the stop loss check is bypassed, protecting trades from immediate market noise.

### C. Sandbox Placer vs. Live Placer
*   **order_placer_dhan_sandbox.py (Sandbox):** Polls the `signals` table for unprocessed records, checks highly granular pre-trade filters (OTM percentages, late-entry limits, VWAP slope thresholds, and index momentum filters), and submits simulated orders.
*   **Discrepancies & Market Realism:**
    1.  *Fill Slippage:* The sandbox placer assumes immediate executions at the exact signal price. In live options trading, highly volatile premiums suffer massive spreads (often 2% to 15% slippage), making paper results highly optimistic.
    2.  *Resting Orders:* Sandbox orders are squared off via market commands triggered by a local loop. Live trading requires placing bracket orders or resting SL-M/SL-L orders directly on the exchange (NSE/BSE) to prevent catastrophic loss if the local machine loses power or internet connectivity.

---

## 4. Critical Vulnerabilities & Weaknesses

### A. Plaintext Security Hazards & Credentials Leakage
The workspace contains extremely severe credentials exposures. Plaintext API keys and session secrets are stored in standard files, presenting massive financial risk.

| Filename / Location | Exposed Secret / Parameter | Value / Content | Severity |
| :--- | :--- | :--- | :--- |
| `DhanAPI/.env` | `DHAN_ACCESS_TOKEN` | Plaintext JWT token for client `2604048537` | **CRITICAL** |
| `DhanAPI/ - Copy.env` | `DHAN_ACCESS_TOKEN` | Plaintext JWT token for backup client `7067652505` | **CRITICAL** |
| `MasterConfiguration/config/telegram_config.json` | `api_id`, `api_hash`, `phone`, `bot_token`, `chat_id` | Full Telegram Client API credentials and active Bot API token | **HIGH** |
| `MasterConfiguration/config/claude_api_key.txt` | Claude API Key | Plaintext Anthropic API Token (`sk-ant-api03-...`) | **HIGH** |
| `MasterConfiguration/config/kite_config.json` | `api_key`, `api_secret`, `access_token` | Plaintext Zerodha Kite credentials and active access token | **HIGH** |

> [!CAUTION]
> **Account Hijacking & Bot Manipulation Risk:** With these active tokens, an attacker can fully hijack the broker accounts, place unauthorized live trades, intercept personal Telegram messages, and drain funds.

---

### B. State Integrity & Threading Bottlenecks (The 1.2 GB Database Problem)

#### 1. The 1.2 GB `dhan_dashboard.db` Bloat
Our quantitative database audit revealed that the database `dhan_dashboard.db` has ballooned to **1.24 GB** of disk space. 
*   **The Culprit:** The `orders` table. While it is limited to a capacity of 500 rows, each row has an average length of **2,475,192 characters (2.47 MB)**!
*   **The Cause:** `dhan_dashboard.py` polls the Dhan API's `get_order_list()` every 10 seconds. In the Dhan Sandbox environment, old orders are never purged by the broker. Thus, the endpoint returns the *entire historical list of every sandbox order ever placed*. Every 10 seconds, the dashboard dumps this entire raw JSON array (2.47 MB) into a single database cell.
*   **Concurrency Impact:** Writing a 2.5 MB JSON blob every 10 seconds causes significant SQLite disk I/O write locks.

#### 2. SQLite Lock Contentions in `trading.db`
The shared trading database `trading.db` is under extreme concurrent write pressure:
*   `pa_setups` table has **152,057 rows** (written to by `PAScanner` scanning 209 stocks every 5 min).
*   `anomaly_alerts` table has **34,355 rows** (written to by `AnomalyScanner` every 5 min).
*   `strategy_signals` table has **67,516 rows** (written to by the 28 Strategy Workers in `DhanOmniEngine_v2.py`).
*   **The Problem:** The core engine (`DhanOmniEngine.py` and `dhan_sl_monitor.py`) opens direct, raw connections to `trading.db` using `sqlite3.connect(..., timeout=5)` instead of using the thread-safe `db_utils.py` transaction wrapper.
*   **The Risk:** Under heavy market activity (e.g., exactly at 09:15:00 or at 5-minute interval breaks), multiple strategy threads write simultaneously. This leads to silent `database is locked` operational failures, swallowing strategy signals and causing execution misses.

#### 3. Python GIL and Thread Starvation
`DhanOmniEngine_v2.py` initializes **up to 51 active threads** (28 workers and 23 infrastructure/data pipelines) inside a single Python process. Under Python's Global Interpreter Lock (GIL), only one thread executes bytecode at a time. The intensive mathematical and indicator calculations (scanning 209 FnO stocks across multiple timeframes) serialize execution, risking thread starvation and critical delays in trailing stop-loss calculations.

---

### C. Broker Integration Gaps
1.  **Sandbox Data Blind Spot:** The Dhan Sandbox API does not support live market feeds. Relying entirely on Zerodha Kite API for live option LTPs creates a high-risk single-point-of-failure. If the Kite access token expires mid-day, the SL monitor fails to pull prices and skips SL tracking, leaving active positions unmonitored.
2.  **Lack of Exchange-Side SL Protection:** The system simulates stop losses in memory, only sending market square-off orders when an exit condition is met on a 1-minute close. If the internet connection drops, the local machine crashes, or the SL monitor thread hangs, the position runs naked on the live exchange.
3.  **Liquidity & Execution Discrepancy:** The sandbox immediately fills contracts at the signal price. In live trading, executing large quantities in highly illiquid strikes or during sharp momentum moves results in massive slippage that can turn a profitable backtested strategy unprofitable in production.

---

## 5. Detailed Technical Debt

The workspace contains significant technical debt and clutter:
*   **Stale Backups and Log Files:** The root folder contains numerous obsolete start logs (`startday_05may26.txt`, `startday_07may26.txt`), backup scripts (`Test_code_08may26.py`, `_tmp_kb_query.py`, `_tmp_reprice.py`), and redundant markdown files (`Inherit_from_EMA_9_21_Short_28May26_1213.md`).
*   **Redundant 0-Byte Databases:** Multiple empty databases clutter the root folder (e.g., `trading.db` and `paper_trades.db` are 0-byte placeholders in the `DhanAPI` directory, whereas the active database resides in the `MasterConfiguration/data/` folder). This leads to directory confusion and incorrect path references.
*   **Obsolete Standalone Prototypes:** Files like `HighConvictionTraderV2.py` run independent, redundant NIFTY trading loops that duplicate the Quantitative Option Scalper logic now fully modularized into `DhanOmniEngine_v2.py` (Worker B).

---

## 6. Actionable Strategic Roadmap

To transition this system into a secure, robust, and scalable high-performance institutional trading platform, a 4-phased refactoring roadmap is recommended:

```
┌─────────────────────────────────────────────────────────────────┐
│ PHASE 1: IMMEDIATE SECURITY & API SANITIZATION (Days 1-2)       │
├─────────────────────────────────────────────────────────────────┤
│ • Migrate all plain configurations to an encrypted vault.      │
│ • Enforce strict environment variables separation.              │
│ • Add all sensitive JSON, txt, and .env files to .gitignore.     │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ PHASE 2: DATABASE OPTIMIZATION & PRUNING (Days 3-5)             │
├─────────────────────────────────────────────────────────────────┤
│ • Refactor dhan_dashboard.py to filter Sandbox API arrays for   │
│   today's orders *before* saving to dhan_dashboard.db.          │
│ • Run VACUUM on dhan_dashboard.db to reclaim 1.2 GB disk space. │
│ • Transition all raw SQLite connects to db_utils.py WAL wrappers.│
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ PHASE 3: CONCURRENCY & ARCHITECTURAL DECOUPLING (Days 6-10)     │
├─────────────────────────────────────────────────────────────────┤
│ • Decouple DhanOmniEngine_v2.py into separate micro-processes  │
│   (e.g., PAScanner, AnomalyScanner, StrategyEngine).            │
│ • Switch the database layer to PostgreSQL or Redis for state    │
│   sharing to eliminate SQLite write lock contentions.           │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ PHASE 4: BROKER INTEGRATION & RISK SAFEGUARDS (Days 11-15)      │
├─────────────────────────────────────────────────────────────────┤
│ • Implement broker-side resting SL-M/SL-L orders immediately   │
│   upon position entry to guarantee disaster recovery protection. │
│ • Add a secondary live feed fallback (e.g., Dhan WebSocket or   │
│   NSE direct indices API) to eliminate the Kite single-failure. │
└─────────────────────────────────────────────────────────────────┘
```

### Detailed Phase Tasks

#### Phase 1: Security & API Sanitization (Immediate)
1.  **Credentials Relocation:** Implement a single-source configuration loader that retrieves API secrets using operating system environment variables (e.g., `os.environ.get("DHAN_ACCESS_TOKEN")`).
2.  **Local Encryption:** Encrypt `telegram_config.json` and `kite_config.json` locally using AES-256 via the `cryptography` Python library, using an decryption key stored out-of-tree.
3.  **Git Hardening:** Immediately add `.env`, `*.env`, `telegram_config.json`, `kite_config.json`, and `claude_api_key.txt` to the project's `.gitignore` to prevent accidental commits to public repositories.

#### Phase 2: Database Optimization & Pruning (High Priority)
1.  **Dashboard Filter Fix:** Modify the database saving method in `dhan_dashboard.py` to filter the JSON array returned by Dhan's sandbox. Only write orders matching `createTime.startswith(today)` to the database:
    ```python
    def _save(table: str, data):
        ts = datetime.now().isoformat(timespec="seconds")
        if table == "orders" and isinstance(data, list):
            today_str = datetime.now().strftime("%Y-%m-%d")
            data = [o for o in data if str(o.get("createTime") or "").startswith(today_str)]
        # Proceed with saving...
    ```
2.  **Disk Space Recovery:** Execute the following Python script to vacuum the bloated database and immediately reclaim 1.2 GB of disk storage:
    ```python
    import sqlite3
    conn = sqlite3.connect("dhan_dashboard.db")
    conn.execute("VACUUM")
    conn.close()
    ```
3.  **Database WAL Enforcement:** Refactor the database connections in `DhanOmniEngine.py` and `dhan_sl_monitor.py` to use `db_utils.py` context managers. This enforces Write-Ahead Logging (`PRAGMA journal_mode=WAL`) and exponential backoff retry algorithms for concurrent operations.

#### Phase 3: Concurrency & Process Decoupling (Medium Priority)
1.  **Micro-process Decoupling:** Break `DhanOmniEngine_v2.py` into separate standalone processes rather than running 51 threads under a single GIL:
    *   `pa_scanner_daemon.py` (Runs Price Action scanning in a separate OS process).
    *   `anomaly_scanner_daemon.py` (Runs anomaly scans).
    *   `strategy_engine_core.py` (Runs the primary options and futures strategy workers).
2.  **High-Performance DB Store:** Replace SQLite for real-time IPC (inter-process communication) with **Redis** or a local **PostgreSQL** instance. This eliminates write lock contentions and provides sub-millisecond state updates across all pipelines.

#### Phase 4: Broker Integration & Risk Safeguards (Operational Readiness)
1.  **Resting Exchange Stop Losses:** Modify the order execution logic to submit a primary market order and a corresponding exchange-side stop-loss market order (`SL-M`) simultaneously. This guarantees risk protection even if the local server crashes.
2.  **Multi-Feed Fallback:** Refactor `dhan_sl_monitor.py` to automatically switch to Dhan's direct WebSocket feed or public NSE API endpoints if the primary Kite Connect LTP feed fails.
3.  **Slippage Simulation:** Integrate a slippage simulator in `order_placer_dhan_sandbox.py` that penalizes paper fills based on the underlying asset's average bid-ask spread and current ADX volatility, increasing backtesting realism.
