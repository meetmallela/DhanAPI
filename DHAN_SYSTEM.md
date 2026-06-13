# Dhan Algorithmic Trading System
**Built:** March – April 2026 | **Mode:** Sandbox (token valid until 2026-05-04)

---

## Table of Contents
1. [System Overview](#system-overview)
2. [Architecture Diagram](#architecture-diagram)
3. [Data Flow](#data-flow)
4. [Component Reference](#component-reference)
   - [Core Layer](#core-layer)
   - [Strategy Layer](#strategy-layer)
   - [Orchestration & Execution](#orchestration--execution)
   - [Monitoring & Dashboard](#monitoring--dashboard)
   - [Resource Management](#resource-management)
   - [Pre-Market Checks](#pre-market-checks)
5. [Database Schema](#database-schema)
6. [Configuration Files](#configuration-files)
7. [Market Hours Guard](#market-hours-guard)
8. [Logging](#logging)
9. [Key Parameters](#key-parameters)
10. [File Locations](#file-locations)
11. [Daily Startup Sequence](#daily-startup-sequence)
12. [Known Limitations](#known-limitations)

---

## System Overview

A fully automated options trading system built on the Dhan API (India). It:

- Fetches live OHLCV candle data from Dhan every 10 seconds
- Runs 5 independent technical strategies across 4 indices
- Resolves ATM option `security_id` dynamically from the Dhan scrip master
- Places MARKET INTRADAY orders on Dhan
- Monitors every open position with 3-stage trailing stop-loss
- Streams real-time LTP via Dhan WebSocket (DhanFeed v2)
- Force-exits all positions at 15:25 IST
- Provides a live local web dashboard at `http://127.0.0.1:5050`
- Optionally receives trade signals from Telegram channels

---

## Architecture Diagram

```
┌────────────────────────────────────────────────────────────────────┐
│                  DHAN ALGORITHMIC TRADING SYSTEM                   │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  DATA LAYER                                                        │
│  ├── LTPFeed          WebSocket real-time LTP (DhanFeed v2)       │
│  ├── DhanClient       REST API: OHLCV candles, orders, funds      │
│  └── StrikeLookup     Options security_id from scrip master CSV   │
│                                                                    │
│  STRATEGY LAYER  (all run inside DhanOmniEngine)                  │
│  ├── EMA 9/21         Simple EMA crossover (5m)                   │
│  ├── OptionScalper    EMA44 + ATR, MTF aligned (5m vs 15m)        │
│  ├── SupertrendMACD   Supertrend + MACD confluence (15m)          │
│  ├── AdvancedEMAORB   EMA/VWAP/S&R triple guard + ORB (5m+15m)   │
│  └── PairLeadership   RELIANCE + HDFC as NIFTY bias filter        │
│                                                                    │
│  EXECUTION LAYER                                                   │
│  ├── DhanOmniEngine   Runs all strategies, places orders          │
│  └── DhanOrderPlacer  Executes signals from Telegram / DB         │
│                                                                    │
│  MONITORING LAYER                                                  │
│  ├── DhanSLMonitor    Trailing SL + hard exit at 15:25 IST        │
│  └── DhanDashboard    Flask web UI, 10s polling, SQLite cache     │
│                                                                    │
│  SUPPORT LAYER                                                     │
│  ├── MasterResource   Centralised paths, logging, config          │
│  ├── db_utils         Thread-safe SQLite with WAL + retry         │
│  └── Day-Start Tools  Pre-market checks + simulate_signal.py      │
│                                                                    │
│  STORAGE                                                           │
│  ├── trading.db           signals + orders (main)                 │
│  ├── dhan_dashboard.db    API snapshot history (last 500)         │
│  └── dhan_scrip_master.csv  Options chain (cached 8h from Dhan)  │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow

```
[Dhan API: OHLCV candles every 10s]
          │
          ▼
[DhanOmniEngine.sync_data()]
  5m bars: refreshed every cycle
  15m bars: cached, max once per 15 min
          │
          ▼
[5 strategies evaluated in parallel]
  EMA_9_21 · OptionScalper · SupertrendMACD
  AdvancedEMAORB · PairLeadership
          │
     Signal fires?
          │
          ▼
[StrikeLookup: spot → ATM option security_id]
          │
          ▼
[Dhan API: place MARKET INTRA order]
          │
          ▼
[trading.db: INSERT into signals + orders (status=OPEN)]
          │
          ▼
[DhanSLMonitor picks up order within 5s]
  LTP source: WebSocket → REST → entry price
  3-stage trailing SL logic
          │
     SL hit or 15:25 IST?
          │
          ▼
[trading.db: UPDATE orders (status=CLOSED, pnl)]
          │
          ▼
[DhanDashboard: reflects updated state every 10s]
```

---

## Component Reference

### Core Layer

#### `core/dhan_client.py`
Thin wrapper around the `dhanhq` SDK.

| Method | Purpose |
|--------|---------|
| `get_fund_limits()` | Available balance, SOD limit, used margin |
| `get_positions()` | Open intraday positions |
| `get_holdings()` | Equity holdings |
| `get_order_list()` | All orders |
| `get_trade_book()` | Executed trade fills |
| `get_forever()` | GTT / Forever orders |
| `place_order(...)` | Place MARKET or LIMIT order |
| `intraday_minute_data(security_id, segment, instrument, from, to, interval)` | OHLCV candles — interval: 1/5/15/25/60 min |

Credentials loaded from `.env`: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`.

---

#### `core/ltp_feed.py`
Thread-safe WebSocket LTP cache using DhanFeed v2.

**Protocol:** `wss://api-feed.dhan.co?version=2&token=...&clientId=...&authType=2`

| Exchange | Code |
|----------|------|
| IDX_I | 0 |
| NSE_EQ | 1 |
| NSE_FNO | 2 |
| BSE_EQ | 4 |
| BSE_FNO | 8 |

| Method | Purpose |
|--------|---------|
| `start()` | Launch WebSocket in daemon thread |
| `subscribe(security_id, exchange_segment)` | Add to live feed |
| `unsubscribe(security_id)` | Remove from feed |
| `get_ltp(security_id)` → float\|None | Cache lookup (zero-lag) |
| `get_ltp_rest(client, security_id, exchange_segment)` → float\|None | REST fallback |

Reconnects automatically after 5s on disconnect.

---

#### `core/strike_lookup.py`
Resolves the Dhan `security_id` for an ATM option at a given spot price.

**Data source:** `https://images.dhan.co/api-data/api-scrip-master.csv`
Cached at `MasterConfiguration/data/dhan_scrip_master.csv`, refreshed every 8 hours.

```python
SYMBOL_CONFIG = {
    "NIFTY":      {"strike_inc": 50,  "exchange_segment": "NSE_FNO"},
    "BANKNIFTY":  {"strike_inc": 100, "exchange_segment": "NSE_FNO"},
    "FINNIFTY":   {"strike_inc": 50,  "exchange_segment": "NSE_FNO"},
    "SENSEX":     {"strike_inc": 100, "exchange_segment": "BSE_FNO"},
    "MIDCPNIFTY": {"strike_inc": 25,  "exchange_segment": "NSE_FNO"},
}
```

**Near-expiry ITM shift:** Within 2 days of expiry, CE moves 1 strike lower (ITM), PE moves 1 strike higher (ITM) for better delta. Falls back to exact ATM if ITM strike not in scrip master.

| Method | Returns |
|--------|---------|
| `get_atm_option(symbol, spot, option_type, expiry_date, itm_shift)` | `{security_id, trading_symbol, expiry_date, strike, lot_size, exchange_segment}` |
| `get_nearest_expiry(symbol)` | `"YYYY-MM-DD"` |
| `get_by_trading_symbol(trading_symbol)` | Same dict (reverse lookup from e.g. `NIFTY-Apr2026-22350-CE`) |

---

#### `core/order_placer.py`
Lightweight MARKET order wrapper used by `DhanOmniEngine`.

```python
place_market_order(security_id, exchange_segment, transaction_type, quantity)
→ order_id | None
```

Tracks `failed_attempts`; stops after 20 consecutive failures.

---

### Strategy Layer

All strategies return `"BULLISH"`, `"BEARISH"`, or `"NEUTRAL"`.

#### `strategies/ema_9_21.py` — EMA Crossover
- **Signal:** EMA(9) crosses EMA(21) on 5m bars
- **Min candles:** 22
- **Inputs:** 5m OHLCV DataFrame

#### `strategies/option_scalper.py` — MTF EMA Scalper
- **Signal:** 5m price vs EMA(44) **must agree** with 15m bias
- **Both timeframes must align** before signal fires
- **Inputs:** 5m + 15m OHLCV DataFrames

#### `strategies/supertrend_macd.py` — Supertrend + MACD Confluence
- **Signal:** Supertrend direction AND MACD vs signal line must agree
- **Parameters:** ST(10, 3), MACD(12, 26, 9)
- **Inputs:** 15m OHLCV DataFrame

#### `strategies/advanced_ema_orb.py` — Dual Advanced Strategy
Two modes in one class:

**`check_ema_vwap_sr(df_5m, sup_15m, res_15m)` — Triple Guard:**
- Bullish: EMA9 > EMA21 AND price > VWAP AND price above S/R

**`check_orb_vwap(df_5m)` — Opening Range Breakout:**
- Bullish: Price breaks above day's first-candle high AND price > VWAP AND EMA9 > EMA21

#### `strategies/pair_leadership.py` — Equity Leaders as Index Filter
- Uses RELIANCE + HDFC 5m bars to infer NIFTY direction
- Signal only fires if **both** equities agree (prevents false signals)
- Bias: price vs VWAP AND price vs 5-candle high/low

---

### Orchestration & Execution

#### `DhanOmniEngine.py`
Master strategy runner. Runs all 5 strategies across 4 indices every 10 seconds.

**Indices tracked:** NIFTY, BANKNIFTY, FINNIFTY, SENSEX
**Equity leaders:** RELIANCE, HDFC (for PairLeadership strategy)

**Data buffers (rolling, last 100 candles each):**
```
NIFTY_5m, NIFTY_15m, BANKNIFTY_5m, BANKNIFTY_15m,
FINNIFTY_5m, FINNIFTY_15m, SENSEX_5m, SENSEX_15m,
RELIANCE (5m), HDFC (5m)
```

**Per-cycle logic (inside market hours only):**
1. `sync_data()` — refresh all buffers
2. PairLeadership check → fires on NIFTY if leaders agree
3. For each index:
   - EMA_9_21 (5m)
   - OptionScalper (5m + 15m)
   - SupertrendMACD (15m)
   - AdvancedEMA_VWAP_SR (5m + 15m S/R)
   - ORB_VWAP (5m)
4. On non-NEUTRAL signal → `execute(index, bias, strategy_name)`

**`execute()` flow:**
1. Read spot from latest 5m close
2. `StrikeLookup.get_atm_option()` → option security_id
3. Place MARKET INTRA order on Dhan
4. `log_to_master()` → write to both `signals` and `orders` tables

**Data fetch behaviour:**
- DH-907 (no data, market closed) → logged at DEBUG only, no WARNING spam
- Failed fetch → existing buffer kept unchanged (silent fallback)
- 15m candles fetched at most once per 15 min (TTL cache per index)

---

#### `dhan_order_placer.py`
Alternate execution path — polls `signals` table for `processed=0` rows.

Used when signals come from Telegram or other external sources.

**Loop (2s sleep, market hours only):**
1. `SELECT * FROM signals WHERE processed=0`
2. `_resolve_instrument()` → get security_id
   - Index symbols → `StrikeLookup.get_atm_option()`
   - Equity symbols → hardcoded map (RELIANCE/HDFC)
3. `place_dhan_order()` → MARKET INTRA
4. Mark signal `processed=1`
5. INSERT into `orders` with full metadata (security_id, exchange_segment, tradingsymbol)

---

#### `dhan_sl_monitor.py`
Tracks every OPEN order, manages trailing stop-loss, force-exits at 15:25 IST.

**Config loaded from `sl_config.json`:**
```json
{
  "initial_sl_percent": 5.0,
  "trailing_activation_percent": 3.0,
  "trailing_step_percent": 1.0,
  "hard_cutoff_time": "15:25"
}
```

**3-Stage Trailing SL (for BUY positions):**

| Stage | Trigger | SL Moves To |
|-------|---------|-------------|
| 0 (default) | — | entry × (1 − 5%) |
| 1 (breakeven) | gain ≥ 3% | entry price |
| 2 (lock profit) | gain ≥ 6% | entry + 50% of (peak − entry) |
| 3 (tight trail) | gain ≥ 9% | peak × (1 − 1%) |

Symmetric logic for SELL positions (reversed).

**LTP source priority:**
1. WebSocket cache (real-time)
2. REST fallback via `ticker_data`
3. Entry price (last resort — never crashes)

**DB migration:** Adds `security_id` and `exchange_segment` columns to `orders` table at startup if absent.

---

### Monitoring & Dashboard

#### `dhan_dashboard.py`
Flask server + background poller. Serves `http://127.0.0.1:5050`.

**Background thread polls every 10s:**
- `get_fund_limits()` → funds table
- `get_positions()` → positions table
- `get_holdings()` → holdings table
- `get_order_list()` → orders table
- `get_trade_book()` → trades table
- `get_forever()` → forever_orders table

Each table keeps the last 500 snapshots in `dhan_dashboard.db`.

**`GET /api/snapshot` response:**
```json
{
  "funds": {"fetched_at": "...", "data": {...}},
  "positions": {"fetched_at": "...", "data": [...]},
  "holdings": {"fetched_at": "...", "data": [...]},
  "orders": {"data": [today_only]},
  "trades": {"fetched_at": "...", "data": [...]},
  "forever_orders": {"fetched_at": "...", "data": [...]},
  "pnl_summary": {"total": 0.0, "realized": 0.0, "unrealized": 0.0},
  "order_counts": {"TRADED": 0, "PENDING": 0, "CANCELLED": 0, "REJECTED": 0},
  "errors": {},
  "is_sandbox": true,
  "server_time": "2026-04-12T09:15:00"
}
```

**Note:** Orders are filtered to today only (by `createTime` date) — historical sandbox orders are excluded.

---

#### `dhan_tg_trader.py`
Optional Telegram signal bridge using Telethon.

- Credentials loaded from `telegram_config.json` via `MasterResource` (not `.env`)
- Listens for new messages on all joined channels
- Parses signals via `yaatra_parser` and `channel_parsers`
- Session persisted in `dhan_bot_session.session` (OTP only needed once)

---

### Resource Management

#### `master_resource.py`
Single source of truth for all paths, logging, and config loading.

**Root:** `C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration`

| Method | Returns |
|--------|---------|
| `get_trading_db_path()` | `.../data/trading.db` |
| `get_shared_db_path()` | `.../data/shared_market_data.db` |
| `get_sl_config_path()` | `.../config/sl_config.json` |
| `get_telegram_config()` | dict from telegram_config.json |
| `get_sl_exits_path()` | `.../data/sl_exits.json` |
| `setup_shared_logger(app_name)` | Logger with file + console handlers |

**Logging format:**
- Filename: `{app_name}_{DDMonYYYY}_{HH}_{MM}_{SS}.log`
- Example: `dhan_sl_monitor_12Apr2026_09_15_30.log`
- New file created on every process restart

---

### Pre-Market Checks

#### `day_start_checks.py` — Run every morning before 9:00 AM
```
C:\ProgramData\anaconda3\python.exe day_start_checks.py
```

Runs 4 checks in sequence:

| # | Script | Checks |
|---|--------|--------|
| 1 | `check_token.py` | Token valid? Shows balance + SOD limit |
| 2 | `check_scrip_master.py` | CSV present? Age < 24h? |
| 3 | `check_open_orders.py` | Any OPEN orders left from yesterday? |
| 4 | `check_strike_lookup.py` | ATM resolution works for NIFTY/BANKNIFTY/FINNIFTY? |

#### `simulate_signal.py`
Inserts a test signal (`processed=0`) into `trading.db`.
Used to verify the full pipeline: signal → order placer → Dhan API → SL monitor → dashboard.

---

## Database Schema

### `trading.db` — Main Trading Database

**`signals` table**
```sql
id            INTEGER PRIMARY KEY AUTOINCREMENT
channel_id    TEXT
channel_name  TEXT
message_id    INTEGER
raw_text      TEXT
parsed_data   TEXT  -- JSON: {symbol, action, price, strategy, ...}
timestamp     TEXT  -- ISO8601
processed     INTEGER  -- 0=pending, 1=executed, -1=failed
order_id      TEXT
order_status  TEXT  -- PLACED / EXECUTED / REJECTED
UNIQUE(channel_id, message_id)
```

**`orders` table**
```sql
id                INTEGER PRIMARY KEY AUTOINCREMENT
signal_id         INTEGER
order_id          TEXT
symbol            TEXT  -- NIFTY, BANKNIFTY, etc.
action            TEXT  -- BUY / SELL
quantity          INTEGER
entry_price       REAL
ltp               REAL  -- updated by SL monitor every 5s
stop_loss         REAL  -- updated by trailing SL logic
exit_price        REAL
pnl               REAL
status            TEXT  -- OPEN / CLOSED / CANCELLED
tradingsymbol     TEXT  -- e.g. NIFTY-Apr2026-22350-CE
security_id       TEXT  -- Dhan internal ID
exchange_segment  TEXT  -- NSE_FNO / NSE_EQ / BSE_FNO
created_at        TEXT  -- ISO8601
updated_at        TEXT  -- ISO8601
```

### `dhan_dashboard.db` — Monitoring Cache

Six tables, identical structure:
```sql
id          INTEGER PRIMARY KEY AUTOINCREMENT
fetched_at  TEXT  -- ISO8601
data        TEXT  -- JSON blob from Dhan API
-- keeps latest 500 rows per table
```
Tables: `funds`, `positions`, `holdings`, `orders`, `trades`, `forever_orders`

---

## Configuration Files

### `.env`
```
DHAN_CLIENT_ID="2604048537"
DHAN_ACCESS_TOKEN="eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9...."
```
Token valid until **2026-05-04**. Get new tokens from `https://developer.dhan.co/home`.

### `MasterConfiguration/config/sl_config.json`
```json
{
  "initial_sl_percent": 5.0,
  "trailing_activation_percent": 3.0,
  "trailing_step_percent": 1.0,
  "hard_cutoff_time": "15:25"
}
```

### `MasterConfiguration/config/telegram_config.json`
```json
{
  "api_id": 25677420,
  "api_hash": "3fe3d6d76fdffd005104a5df5db5ba6f",
  "phone": "+919833459174"
}
```

---

## Market Hours Guard

Both execution bots enforce an active trading window of **9:15 – 15:25 IST**.

| Bot | Outside hours behaviour |
|-----|------------------------|
| `DhanOmniEngine` | Skips `sync_data()` + all strategies, sleeps 60s |
| `DhanOrderPlacer` | Skips signal execution entirely, sleeps 30s |
| `DhanSLMonitor` | Monitors positions all day, force-exits at 15:25 |

```python
IST          = pytz.timezone("Asia/Kolkata")
MARKET_OPEN  = time(9, 15)
MARKET_CLOSE = time(15, 25)
```

---

## Logging

**Format:** `{app_name}_{DDMonYYYY}_{HH}_{MM}_{SS}.log`
**Location:** `MasterConfiguration/logs/`
**A new file is created on every process restart.**

| Bot | Logger name |
|-----|------------|
| T1 Dashboard | `dhan_dashboard` |
| T2 SL Monitor | `dhan_sl_monitor` |
| T3 Order Placer | `dhan_order_placer` |
| T4 Omni Engine | `dhan_omni_engine` |
| T5 TG Trader | `dhan_tg_trader` |
| Strike Lookup | `strike_lookup` |
| LTP Feed | `ltp_feed` |

**Log levels:**
- `DEBUG` — Data fetches, cache hits, DH-907 no-data (suppressed from file at normal level)
- `INFO` — Orders placed, positions opened/closed, bot startup
- `WARNING` — Unexpected API errors, SL hits, WebSocket reconnects
- `ERROR` — Exceptions, DB failures

---

## Key Parameters

| Component | Parameter | Value | Notes |
|-----------|-----------|-------|-------|
| OmniEngine | `FETCH_DAYS_5M` | 2 days | History window for 5m candles |
| OmniEngine | `FETCH_DAYS_15M` | 5 days | History window for 15m candles |
| OmniEngine | `MIN_CANDLES` | 30 | Minimum candles before any strategy fires |
| OmniEngine | `CACHE_TTL_15M` | 900s | Max 1 fetch per 15 min for 15m bars |
| OmniEngine | Poll interval | 10s | Main loop sleep |
| StrikeLookup | `CACHE_TTL_HOURS` | 8h | Scrip master refresh interval |
| StrikeLookup | `ITM_OFFSET_NEAR_EXPIRY` | 1 strike | ITM shift on near-expiry days |
| StrikeLookup | `EXPIRY_NEAR_DAYS` | 2 | Days threshold for ITM shift |
| LTPFeed | `RECONNECT_DELAY` | 5s | WebSocket reconnect wait |
| SL Monitor | `initial_sl_percent` | 5.0% | Default SL at entry |
| SL Monitor | `trailing_activation_percent` | 3.0% | Stage 1 gain trigger |
| SL Monitor | `trailing_step_percent` | 1.0% | Stage 3 trail tightness |
| SL Monitor | `hard_cutoff_time` | 15:25 | Force-exit all positions |
| SL Monitor | Poll interval | 5s | Position check frequency |
| Dashboard | `POLL_INTERVAL` | 10s | API fetch frequency |
| Dashboard | DB retention | 500 rows | Per table in dhan_dashboard.db |
| EMA_9_21 | Spans | 9 / 21 | Fast/slow EMA periods |
| OptionScalper | EMA period | 44 | Scalper EMA |
| SupertrendMACD | Supertrend | (10, 3) | Period, multiplier |
| SupertrendMACD | MACD | (12, 26, 9) | Fast, slow, signal |
| AdvancedEMAORB | S/R lookback | 21 candles | Support/resistance window |

---

## File Locations

```
DhanAPI/
├── core/
│   ├── dhan_client.py          Dhan API wrapper
│   ├── ltp_feed.py             WebSocket LTP cache
│   ├── strike_lookup.py        ATM option security_id resolver
│   └── order_placer.py         Basic order placement (legacy)
├── strategies/
│   ├── ema_9_21.py             EMA crossover
│   ├── option_scalper.py       MTF EMA44 scalper
│   ├── supertrend_macd.py      Supertrend + MACD
│   ├── advanced_ema_orb.py     Triple guard + ORB/VWAP
│   └── pair_leadership.py      RELIANCE/HDFC bias filter
├── templates/
│   └── dashboard.html          Live dashboard UI (dark theme)
├── DhanOmniEngine.py           Master strategy runner
├── dhan_dashboard.py           Flask web server + poller
├── dhan_sl_monitor.py          Trailing SL + exit manager
├── dhan_order_placer.py        Signal consumer + executor
├── dhan_tg_trader.py           Telegram signal bridge (optional)
├── master_resource.py          Centralised resource manager
├── db_utils.py                 Thread-safe SQLite utilities
├── day_start_checks.py         All-in-one morning check
├── check_token.py              Verify Dhan API token
├── check_scrip_master.py       Verify options CSV freshness
├── check_open_orders.py        List leftover open orders
├── check_strike_lookup.py      Test ATM option resolution
├── simulate_signal.py          Inject test signal into DB
├── .env                        Dhan API credentials
└── DayStart.txt                Daily startup guide

MasterConfiguration/
├── config/
│   ├── sl_config.json          SL percentages + cutoff time
│   └── telegram_config.json    Telegram API credentials
├── data/
│   ├── trading.db              signals + orders (main DB)
│   ├── shared_market_data.db   Webhook signals
│   └── dhan_scrip_master.csv   Options chain (cached, 8h TTL)
└── logs/
    └── dhan_*_DDMonYYYY_HH_MM_SS.log   Per-bot per-restart logs
```

---

## Daily Startup Sequence

```
Before 9:00 AM:
  C:\ProgramData\anaconda3\python.exe day_start_checks.py

Start bots (each in its own terminal, same working directory):
  cd C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI

  T1  C:\ProgramData\anaconda3\python.exe dhan_dashboard.py
      → open http://127.0.0.1:5050

  T2  C:\ProgramData\anaconda3\python.exe dhan_sl_monitor.py
      → must be running before any orders are placed

  T3  C:\ProgramData\anaconda3\python.exe dhan_order_placer.py
      → executes signals from Telegram / simulate_signal.py

  T4  C:\ProgramData\anaconda3\python.exe DhanOmniEngine.py
      → strategies fire automatically from 9:15 AM

  T5  C:\ProgramData\anaconda3\python.exe dhan_tg_trader.py  (optional)
      → Telegram signal bridge

Shutdown order (15:30 IST): T5 → T4 → T3 → T2 → T1
```

---

## Known Limitations

| Limitation | Detail |
|------------|--------|
| Sandbox candle data | Dhan returns DH-907 (no data) outside 9:15–15:30. Expected — handled gracefully. |
| WebSocket in sandbox | DhanFeed may not stream real ticks in sandbox. Automatically falls back to REST LTP. |
| No position sizing | Every order uses `lot_size` from scrip master (1 lot). No Kelly/risk-based sizing yet. |
| No re-entry logic | Once a position is closed by SL, the same signal will not re-enter automatically. |
| TG trader not fully integrated | `dhan_tg_trader.py` places orders but does not write to `orders` table — SL monitor will not track those positions. |
| Single strategy per signal | If 3 strategies fire simultaneously on NIFTY, 3 separate orders are placed. No deduplication. |
| Scrip master staleness | If download fails and cache is old, `get_atm_option()` returns None and no order is placed. |
| Token expiry | Current sandbox token expires **2026-05-04**. Renew at https://developer.dhan.co/home. |
