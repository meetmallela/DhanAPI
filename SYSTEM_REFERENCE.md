# Dhan Paper Trading System — Full Reference
**Last updated: 2026-05-29 (Replica_Fin + SensexGamma inherit) | Session: TRD-20260416 | Account: Demo Dhan (sandbox)**

---

## Table of Contents
1. [System Architecture](#1-system-architecture)
2. [Data Sources](#2-data-sources)
3. [Regime Detection](#3-regime-detection)
4. [Strategy Roster](#4-strategy-roster)
5. [Entry Filters (Order Placer)](#5-entry-filters-order-placer)
6. [Stop-Loss Management](#6-stop-loss-management)
7. [Execution Flow](#7-execution-flow)
8. [How to Run](#8-how-to-run)
9. [Learning Loop & KB Audit](#9-learning-loop--kb-audit)
10. [TG Channel Parsers](#10-tg-channel-parsers)
11. [Phase 6A — MCX Commodity Brain](#11-phase-6a--mcx-commodity-brain)
12. [Phase 7A — Leading Indicator Engine](#12-phase-7a--leading-indicator-engine)
13. [Fundamental Analysis Agent (Phase 4B)](#13-fundamental-analysis-agent-phase-4b)
14. [Watchdog Heartbeat System](#14-watchdog-heartbeat-system-added-2026-05-28)
15. [Strategy Performance (May 8–20, 2026)](#15-strategy-performance-may-820-2026)
16. [Known Limitations & Open Gaps](#16-known-limitations--open-gaps)

---

## 1. System Architecture

Nine processes run in parallel, managed by `start_trading_system.py`:

| # | Process | File | Role |
|---|---|---|---|
| 1 | **TG Reader** | `telegram_reader_production.py` | Reads Telegram signal channels → writes to `signals` DB table |
| 2 | **Order Placer** | `order_placer_dhan_sandbox.py` | Polls `signals` table → applies filters → places paper orders on Dhan sandbox |
| 3 | **SL Monitor** | `dhan_sl_monitor.py` | Polls open orders → tracks live LTP → manages trailing SL → forces exit at 15:25 |
| 4 | **OmniEngine** | `DhanOmniEngine_v2.py` | 49 agents: 26 strategy workers + 23 infrastructure daemons |
| 5 | **Dashboard** | `dhan_dashboard.py` | Flask web app at `http://127.0.0.1:5050` |
| 6 | **EOD WhatIf** | `eod_whatif_backtest.py` | Nightly backtest + KB embedding |
| 7 | **Health Monitor** | `health_monitor.py` | Deep health checks: Kite token, Claude API, log staleness → TG alerts |
| 8 | **Swing Agent** | `agents/swing_agent.py` | Overnight swing setups |
| 9 | **Watchdog** | `watchdog.py` | Heartbeat-based hung-process detector — DB heartbeats, auto-restarts with WARMUP guard |

**Two-layer watchdog:**
- **Layer 1 (PID-based)** `start_trading_system.py watchdog` — detects CRASHED processes (PID gone); registered in Task Scheduler at 09:05 IST Mon-Fri
- **Layer 2 (heartbeat-based)** `watchdog.py` — detects HUNG processes (PID alive but loop stuck); reads `system_status` DB table every 30s; WARMUP guard prevents cascade restarts

**Database:** Single SQLite file at `MasterConfiguration/data/trading.db`
- Tables: `orders`, `signals`, `strategy_signals`, `whatif_trades`, `leading_snapshots`, `leading_signals`, `commodity_signals`, `commodity_paper_trades`, `pa_zones`, `pa_setups`, `pattern_alerts`, `anomaly_alerts`, `fa_scores`, `fa_paper_portfolio`, `swing_setups`, `risk_flags`, **`system_status`** (heartbeat watchdog)

**OmniEngine v2 — 51 agents (as of 2026-05-29):**

| Category | Workers | Count |
|---|---|---|
| Strategy workers (signal queue) | A–V + Y + Z + AA + AB + AC + AD | **28** |
| Infrastructure daemons | DirectorAgent, PCRFilter, DataAgent, MetaAgent, ExecutionAgent | 5 |
| Option/Gamma daemons | OptionCandleCollector, GammaBlastWorker | 2 |
| Volatility daemons | VIXStraddleWorker (W), IronCondorWorker (X), WeeklyStrangleWorker (Z1), RedDaySellerWorker (Z2) | 4 |
| Momentum daemons | FuturesBasisWorker (3C), ExpiryBlastWorker (4A) | 2 |
| Analysis daemons | FAAgent (4B), AnomalyScanner (5A), RiskSentinel (5B) | 3 |
| Candle collectors | EquityCandleCollector (6C-data), PatternScanner (6C), PAScanner (6B) | 3 |
| MCX daemons | MCXWorker (6A), MCXCandleCollector (6A), CommodityBrain (6A) | 3 |
| Leading Engine | LeadingIndicatorEngine (7A) | 1 |

**Watchdog:** two-layer — PID-based (`start_trading_system.py watchdog`) + heartbeat-based (`watchdog.py`)

**psutil.AccessDenied fix (2026-05-27):** `wait_procs()` crashed when any target process ran elevated. Wrapped in `try/except psutil.AccessDenied` with fallback to `[p for p in targets if p.is_running()]` in `start_trading_system.py`.

---

## 2. Data Sources

### Live Index Prices
| Source | Indices | Method |
|---|---|---|
| **NSE Live Feed** (`nse_live_feed.py`) | NIFTY, BANKNIFTY, FINNIFTY | 60s HTTP polling — free, no API key |
| **Kite candle store** (`kite_candle_store`) | All 5 indices + RELIANCE + HDFC | SQLite cache; refreshed each cycle |

### Candle Buffers (in memory, `DhanOmniEngine.data`)
| Key | Timeframe | Max rows kept |
|---|---|---|
| `{IDX}_1m` | 1-minute | 200 |
| `{IDX}_5m` | 5-minute | 100 |
| `{IDX}_15m` | 15-minute | 100 |
| `RELIANCE` | 5-minute | 100 |
| `HDFC` | 5-minute | 100 |

Indices: `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `SENSEX`, `MIDCPNIFTY`

### Futures Volume Injection for True VWAP (added 2026-05-29)
Spot index candles (NIFTY, BANKNIFTY, etc.) have zero native volume at the exchange level. `DataAgent._cycle()` now injects front-month futures volume before building each `MarketSnapshot`:
- `_get_futures_volume(index_name, interval)` reads today's candles from `kite_candles.db` tables `candles_futures_1min` / `candles_futures_5min`
- `inject_futures_volume(df_spot, df_fut)` merges on timestamp with left join (preserves zero for bars with no futures match)
- Both `df_1m` and `df_5m` in the snapshot now carry real futures volume
- `vwap_with_bands()` automatically switches from TWAP proxy to true VWAP when `vol.sum() > 0`
- `futures_vol_gate(df, sma_period=20, mult=1.2)` returns `True` when latest volume > 1.2 × SMA₂₀ — used in PowerCandleStrategy and available to any other strategy

Index → futures symbol mapping: `NIFTY→NIFTY_FUT, BANKNIFTY→BANKNIFTY_FUT, FINNIFTY→FINNIFTY_FUT, MIDCPNIFTY→MIDCPNIFTY_FUT, SENSEX→SENSEX_FUT`

### Startup Seeding
- `dhan_candle_store.seed_engine()` loads last **5 trading days** of 1m candles on startup
- Resamples to 5m and 15m — strategies are warm from first tick
- `_refresh_kite_indices()` merges today's candles into existing buffer each cycle (no overwrite)

### OmniEngine Candle Fallback (updated 2026-05-21)
When `NSELiveFeed` is unavailable, `sync_data()` falls back to **Kite** (was: Dhan `intraday_minute_data`):
- All 5 indices → `_refresh_kite_indices()` via `kite_candle_store.get_candles()`
- Equity leaders (RELIANCE, HDFC) → `kite_candle_store.get_candles()` 5-min feed
- Direct `kite.historical_data()` path available if local candle DB unavailable

### Option LTP Fallback (updated 2026-05-21)
`_get_option_ltp()` in `DhanOmniEngine.py` — used at order entry to estimate option price:

| Priority | Source |
|---|---|
| 1 | `kite_candles.db` latest close for that option symbol |
| 2 | **Kite `ltp([token])`** — token resolved from NFO/BFO instruments *(was: Dhan `intraday_minute_data`)* |
| 3 | DTE-adjusted ATM estimate: `spot × 0.5% × √DTE` |

### MCX Commodity Data — Kite Primary (updated 2026-05-27)
MCX 1M candles are fetched via **Kite `historical_data()`** as primary source (Dhan sandbox returns no MCX_COMM data). Dhan is fallback only.

| Component | File | Role |
|---|---|---|
| `MCXKiteLookup` | `core/mcx_kite_lookup.py` | Resolves front-month Kite instrument token for 5 MCX symbols daily |
| `fetch_today_kite()` | `core/mcx_candle_store.py` | Fetches 1M candles via `kite.historical_data()` for full MCX session 09:00–23:30 |
| `MCXCandleCollector` | `agents/mcx_candle_collector.py` | Tries Kite first; falls back to Dhan; sweeps every ~2.5s |

**Kite symbol mapping for MCX:**
| Internal symbol | Kite name | Note |
|---|---|---|
| GOLDM | GOLDM | |
| SILVERM | SILVERM | |
| CRUDEOILM | CRUDEOILM | |
| NATURALGAS | NATURALGAS | |
| COPPERM | COPPER | No mini on Kite — maps to full COPPER (1000kg lot) |

MCX candles stored in `MasterConfiguration/data/mcx_candles.db` → table `mcx_candles_1min`.

### MCX Data via Kite (NATURALGAS, CRUDEOIL Options)
MCX symbols (NATURALGAS, CRUDEOIL options) cannot be placed on Dhan sandbox but are now **routed through Kite** instead of being skipped.

Key functions in `order_placer_dhan_sandbox.py`:
- `_MCX_NAME_MAP` — maps Dhan symbol root to Kite search name (e.g. `"NATURALGAS"` → `"NATURAL GAS"`)
- `_MCX_LOT_SIZES` — lot sizes for MCX instruments (NATURALGAS: 1250, CRUDEOIL: 100)
- `_get_mcx_instruments()` — fetches & caches Kite `MCX` instrument list
- `_resolve_mcx_kite(symbol)` → `instrument_token` — filters by name, strike, expiry, option type

MCX orders bypass the lot-size cap (`lot_size > 200`) check.

### Kite Instrument Tokens
```
NIFTY:       256265    BANKNIFTY: 260105    FINNIFTY:   257801
MIDCPNIFTY:  288009    SENSEX:    265       BANKEX:     274441
RELIANCE:    738561    HDFC:      341249  (= HDFCBANK post-merger)
```

### WebSocket Stale-Data Heartbeat (added 2026-05-29)
`core/ltp_feed.py` now tracks `_last_tick_time` (updated on every `_handle_tick()` call).

| API | Description |
|---|---|
| `ltp_feed.tick_age_seconds` | Seconds since last WebSocket tick; `math.inf` if no tick ever received |
| `ltp_feed.is_stale(threshold=30)` | True when no tick in last 30s |
| `ltp_feed.force_reconnect()` | Closes current DhanFeed → `_reconnect_loop` retries immediately (thread-safe) |

**When checked:** `dhan_sl_monitor.py` heartbeat (every 2 min). If `is_stale(30)` during market hours → logs `[WS STALE]` WARNING and calls `force_reconnect()`. This detects silent data freezes where the WebSocket connection is technically alive but no ticks are flowing (a known issue with Dhan sandbox and network interruptions).

**Why this matters:** Without this check, the main loop prints "Monitoring..." indefinitely while data feed is dead. With it, the SL monitor detects the freeze within ≤2 minutes and forces a reconnect.

### LTP for SL Monitor
**Priority (as of 2026-05-21 — Kite is PRIMARY):**

| Priority | Source | Coverage | Notes |
|---|---|---|---|
| 1 | **Kite batch `ltp()`** | NFO + BFO + MCX — all exchanges | Called once per 5s poll cycle; `0.0` treated as missing |
| 2 | Dhan WebSocket (LTPFeed) | NSE_FNO reliable; BSE_FNO returns `0.0` silently | Supplement only; `0.0` ticks ignored |
| 3 | `kite_candles.db` latest close | All tracked options | 1-min delayed; final fallback |
| — | Skip cycle | If all fail | Never uses stale/random data |

**Why the change (2026-05-21):** Dhan WebSocket delivers silent `0.0` ticks for BSE_FNO (SENSEX options). Since `0.0 is not None`, the monitor was treating it as a valid price and never falling through to Kite. SENSEX positions ran all day with ltp=0, SL never triggered. Fix: Kite promoted to Priority 1; `0.0` explicitly coerced to `None` at all levels.

**Hard-cutoff residual bug fixed (2026-05-22):** `_check_hard_cutoff` had a separate, independent LTP resolution path that bypassed `_kite_ltp` entirely and fell back to `or trade["entry_price"]` — so positions with no Dhan WS/REST price were force-closed at entry, recording PNL=0. Fix: unified into `_resolve_exit_ltp()` (Kite → WS → REST → candle DB → last-known LTP; never uses `entry_price`). See "Bugs Fixed 2026-05-22 — SL Monitor" below.

### Kite Token Resolution (SL Monitor)
`_resolve_kite_token(tradingsymbol)` in `dhan_sl_monitor.py`:
- Parses `SENSEX-May2026-75800-PE` → base, month, strike, option type
- Looks up NFO (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY) or BFO (SENSEX/BANKEX)
- **Multiple-expiry fix (2026-05-21):** When both a weekly and monthly expiry exist for the same month/year (e.g. SENSEX May-21 weekly + May-27 monthly), results are sorted by expiry ascending and the nearest unexpired date is selected (`>= today`). Previously `iloc[0]` on an unsorted DataFrame could pick the wrong contract.

### Futures Candle Data
Stored in `kite_candles.db` tables `candles_futures_1min / 5min / 15min`.
Fetched by `futures_candle_store.py` — **Kite API only as of 2026-05-21** (was Dhan `intraday_minute_data`).

| Function | Description |
|---|---|
| `get_futures_kite_token(index, offset)` | Looks up near-month futures token from Kite NFO/BFO instruments |
| `fetch_and_store(symbol, from_date, to_date)` | Calls `kite.historical_data(token, ..., oi=True)` — `client` param kept but unused |
| `backfill(client=None, lookback_days=10)` | `client` now optional; Kite supports 60+ days of 1-min futures history |
| `store_dataframe(symbol, df1m)` | Save a pre-fetched DataFrame directly (unchanged) |

Kite futures tokens resolved at runtime:

| Index | Kite Exchange | Instrument type |
|---|---|---|
| NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY | NFO | FUT |
| SENSEX | BFO | FUT |

---

## 3. Regime Detection

Computed each cycle per index from 1m candles using `ADX(14)`:

| ADX | Regime | Strategy workers active |
|---|---|---|
| ≥ 25 | **TRENDING** | A B C D E F G I J K M N O P Q R S T U V Y Z AA AB AC AD |
| 20–25 | **TRANSITION** | same as TRENDING |
| < 20 | **RANGING** | E F H I P only |

- **All regimes:** E (ORB), F (Triple Pattern), I (VWAP Reclaim), S (Harmonic), T (CandleReversal), Y (VWAPSlope), Z (FlagBreakout), AA (TriangleBreakout), AB (H&S)
- **Trending/Transition only:** A B C D G J K M N O Q R U V Z AA AB **AC AD** (most strategies)
- **Ranging only:** H (BB Mean Reversion)
- **Ranging + Trending:** P (StochRSI Divergence)
- **L (MultiStrikeScalp):** wired but disabled (`enabled=False`) pending backtest

---

## 4. Strategy Roster

### Warmup Gate
OmniEngine skips all strategies until `len(df_1m) >= 30` (30 minutes of 1m candles).

### Common v2 Filters (applied to all OmniEngine strategies except TG:SIGNAL)
| Filter | Purpose |
|---|---|
| **Volume z-score** | Bypassed for zero-volume spot index feeds (`volume.sum() == 0`) |
| **Bollinger %B** | `bollinger_pct(close)` in `strategies/indicators.py` — `(close − lower) / (upper − lower)` |
| **RSI direction** | Momentum must agree with signal direction |
| **ADX gate** | Applied to strategies where flat-market signals are noise |

---

### A. EMA 9/21
**File:** `strategies/ema_9_21.py` | **Timeframe:** 1m | **Regime:** Trend / Transition

| Parameter | Value |
|---|---|
| Short EMA span | 9 |
| Long EMA span | 21 |
| ADX period | 14 |
| ADX minimum | 25 |
| RSI period | 7 |
| RSI overbought | 70 (block BULLISH entry) |
| RSI oversold | 30 (block BEARISH entry) |
| RSI directional | > 50 required for BULLISH; < 50 required for BEARISH |
| VWAP proxy alignment | close > 20-bar typical-price mean for BULLISH; < for BEARISH |
| Exhaustion candle | Block if candle range > 2.5× ATR(14) — momentum already spent |

**Entry conditions:**
- **BULLISH:** EMA9 crosses above EMA21 AND RSI 50–70 AND close > VWAP proxy AND not exhaustion candle
- **BEARISH:** EMA9 crosses below EMA21 AND RSI 30–50 AND close < VWAP proxy AND not exhaustion candle

**S/R Gate (Worker A):** Evaluated in `EMA921Worker.evaluate()` after `check_signal()`:
- BULLISH only when 1m close > 5m 21-candle resistance (shift=1) **AND** > 15m 21-candle resistance
- BEARISH only when 1m close < 5m 21-candle support **AND** < 15m 21-candle support

**Dedup:** per-bar timestamp + 2-min cooldown per index

---

### AC. Power Candle EMA44 (OBS inherit — 2026-05-29)
**File:** `strategies/power_candle.py` | **Timeframe:** 5m | **Regime:** Trend / Transition

This is a **pullback/continuation** strategy, complementing Worker B (EMA44 crossover). It fires when an impulsive candle touches the EMA44 and closes in the slope direction — institutional buying/selling at the moving average.

| Parameter | Value |
|---|---|
| EMA period | 44 |
| ADX minimum | 20 |
| Body ratio | ≥ 60% of candle range (power candle) |
| EMA touch | `low ≤ EMA44 ≤ high` required |
| Slope direction | BULLISH: EMA rising (EMA_curr > EMA_prev) AND close > EMA44 AND close > open |
| RSI exhaustion | < 75 BULLISH / > 25 BEARISH |
| Volume gate | `vol > 1.2 × SMA_20(vol)` when futures volume available; pass-through if zero |

**Dedup:** per-bar timestamp + 5-min cooldown per index

---

### AD. Scalper V2 EMA9 Break (OBS inherit — 2026-05-29)
**File:** `strategies/scalper_v2.py` | **Timeframe:** 1m | **Regime:** Trend / Transition

1m EMA9 **price-break** strategy with high-conviction RSI thresholds. Fires only on confirmed strong momentum, not just directional bias. Less frequent than Worker A (EMA9/21 crossover) but higher conviction.

| Parameter | Value |
|---|---|
| EMA period | 9 |
| ADX minimum | 25 |
| Trigger | `prev_close ≤ EMA9 AND curr_close > EMA9` (BULLISH); reverse for BEARISH |
| RSI (BULLISH) | 60–75 (strong momentum, not overbought) |
| RSI (BEARISH) | 25–40 (strong momentum, not oversold) |

**Key distinction vs Worker A:** EMA921 requires 9/21 crossover (regime change); ScalerV2 requires EMA9 **price break** (momentum continuation). Also: RSI 60/40 vs RSI 50 midline — fires only on strong momentum.

**Dedup:** per-bar timestamp + 2-min cooldown per index

---

### B. OptionScalper EMA44
**File:** `strategies/option_scalper.py` | **Timeframe:** 5m | **Regime:** Trend / Transition | *(v2)*

| Parameter | Value |
|---|---|
| EMA period | 44 |
| ADX minimum | **25** (raised from 23) |
| RSI period | 7 |
| RSI midline | > 50 BULLISH, < 50 BEARISH |
| RSI overbought | 70 (cap) |
| RSI oversold | 30 (cap) |
| BB% BULLISH | > **0.65** |
| BB% BEARISH | < **0.35** |
| BB width | Must be **expanding**: current std > 3-bar average std |
| Volume z-score | ≥ **0.8** |
| Min candles needed | 46 (fires from ~12:45 PM IST) |

**Entry conditions:**
- **BULLISH:** close crosses above EMA44 AND close > VWAP proxy AND RSI 50–70 AND BB%>0.65 AND bands expanding AND vol z-score≥0.8
- **BEARISH:** close crosses below EMA44 AND close < VWAP proxy AND RSI 30–50 AND BB%<0.35 AND bands expanding AND vol z-score≥0.8

**Dedup:** per-bar timestamp + 5-min cooldown per index

---

### C. Supertrend + MACD
**File:** `strategies/supertrend_macd.py` | **Timeframe:** 5m | **Regime:** Trend / Transition | *(v2)*

| Parameter | Value |
|---|---|
| Supertrend period | 10 |
| Supertrend multiplier | 3× ATR |
| MACD fast / slow / signal | 12 / 26 / 9 |
| ADX period | 14 |
| ADX minimum | 23 |
| RSI directional | > **48** BULLISH (relaxed from 52), < **52** BEARISH (relaxed from 48) |
| RSI exhaustion cap | ob=70, os=30 |
| BB% BULLISH | > **0.50** (above BB midline) |
| BB% BEARISH | < **0.50** (below BB midline) |
| Volume z-score | ≥ **0.3** |

**Entry conditions:**
- **BULLISH:** ST direction = BULLISH AND MACD > signal line AND (ST flipped bull OR MACD crossed up) AND RSI 48–70 AND BB%>0.50 AND vol z-score≥0.3
- **BEARISH:** ST direction = BEARISH AND MACD < signal line AND (ST flipped bear OR MACD crossed down) AND RSI 30–52 AND BB%<0.50 AND vol z-score≥0.3

**Dedup:** per-bar timestamp + 5-min cooldown per index

---

### D. EMA VWAP SR
**File:** `strategies/advanced_ema_orb.py` → `check_ema_vwap_sr()` | **Timeframe:** 1m | **Regime:** Trend / Transition

| Parameter | Value |
|---|---|
| EMA spans | 9, 21 |
| S/R window | 21 candles rolling high/low |
| VWAP bands | ±1σ rolling std (20 periods) |
| RSI period | 7 |
| RSI ob/os | 70 / 30 |

**Entry conditions:**
- **BULLISH:** EMA9 > EMA21 AND close > VWAP AND close > 21-bar resistance AND close < VWAP+1σ AND RSI < 70
- **BEARISH:** EMA9 < EMA21 AND close < VWAP AND close < 21-bar support AND close > VWAP-1σ AND RSI > 30
- VWAP gate skipped (always True) when volume = 0 (index candles)

---

### E. ORB VWAP
**File:** `strategies/advanced_ema_orb.py` → `check_orb_vwap()` | **Timeframe:** 1m | **Regime:** All

| Parameter | Value |
|---|---|
| Opening range | First 15 × 1m candles (9:15–9:29 AM) |
| ORB formation gate | `orb_candles >= 15` (no signal before range is complete) |
| EMA confirmation | EMA21 |
| RSI ob/os | 70 / 30 |

---

### F. Triple Pattern
**File:** `strategies/triple_pattern.py` | **Timeframe:** 5m | **Regime:** All | *(v2)*

**Active window:** 9:30 AM – 2:45 PM IST | **Cooldown:** 15 minutes per index

| Parameter | Value |
|---|---|
| Lookback | 60 candles (≈5 hours; fires after ~14:15 IST) |
| Swing gap | 5 candles minimum between swings |
| Level tolerance | 1.5% deviation from average |
| ADX minimum | **18** |
| RSI BULLISH | > **45** |
| RSI BEARISH | < **55** |
| BB% BULLISH | > **0.60** |
| BB% BEARISH | < **0.40** |
| Volume z-score | ≥ **0.5** |

---

### G. Index Momentum
**File:** `strategies/index_momentum.py` | **Timeframe:** 1m | **Regime:** Trend / Transition | *(v2)*

**Active window:** 9:30 AM – 2:45 PM IST | **Cooldown:** 20 minutes per index after signal

| Index | Velocity threshold | Big-candle threshold | Lookback |
|---|---|---|---|
| NIFTY | 40 pts | 25 pts | 5 candles |
| BANKNIFTY | 150 pts | 80 pts | 5 candles |
| FINNIFTY | 50 pts | 28 pts | 5 candles |
| SENSEX | 100 pts | 60 pts | 5 candles |
| MIDCPNIFTY | 28 pts | 18 pts | 5 candles |

---

### H. Bollinger Mean Reversion
**File:** `strategies/bollinger_mean_reversion.py` | **Timeframe:** 5m | **Regime:** Ranging only (ADX < 20)

**Active window:** 9:30 AM – 2:45 PM IST | **Cooldown:** 30 minutes per index

- **BULLISH:** close ≤ lower BB AND RSI < 35
- **BEARISH:** close ≥ upper BB AND RSI > 65

---

### I. VWAP Reclaim
**File:** `strategies/vwap_reclaim.py` | **Timeframe:** 1m | **Regime:** All | *(v2)*

**Active windows:** 9:30–11:00 AM and 1:30–3:00 PM IST | **Cooldown:** **20 minutes** per index

Three signal types: Reclaim / Rejection / Band Extreme (±2σ). See original spec for full parameter table.

---

### Full Strategy Worker Roster (28 workers — all files verified 2026-05-29)

| ID | Name | File | Timeframe | Regime | Min bars | Key parameters |
|---|---|---|---|---|---|---|
| A | EMA_9_21 | `strategies/ema_9_21.py` | 1m | Trend/Trans | 23 | Crossover + ADX≥25 + RSI 50/70 + VWAP proxy + exhaustion guard + 5m/15m S/R gate |
| B | OptionScalper_EMA44 | `strategies/option_scalper.py` | 5m | Trend/Trans | 46 | EMA44 crossover + ADX≥25 + BB%>0.65/0.35 + vol z-score≥0.8 |
| C | Supertrend_MACD | `strategies/supertrend_macd.py` | 5m | Trend/Trans | 26 | ST flip + MACD cross + ADX≥23 + BB% midline + vol z-score≥0.3 |
| D | EMA_VWAP_SR | `strategies/advanced_ema_orb.py` | 1m+5m | Trend/Trans | 21 | EMA9>21 + VWAP side + 5m S/R breakout + 15m S/R gate (Worker D) |
| E | ORB_VWAP | `strategies/advanced_ema_orb.py` | 1m | All | 15 | 15-min ORB + EMA21 confirm + ADX≥22 + RSI directional + staleness gate 30m |
| F | TriplePattern | `strategies/triple_pattern.py` | 5m | All | 60 | Triple bottom/top at ±1.5% tolerance; fires after 14:15 IST |
| G | IndexMomentum | `strategies/index_momentum.py` | 1m | Trend/Trans | 6 | Velocity ≥ threshold OR single big-candle; 20-min cooldown |
| H | BB_MeanReversion | `strategies/bollinger_mean_reversion.py` | 5m | Ranging | 20 | Close at BB extreme + RSI <35/>65; 30-min cooldown |
| I | VWAPReclaim | `strategies/vwap_reclaim.py` | 1m | All | 20 | Reclaim/Rejection/±2σ band extreme; 09:30–11:00 + 13:30–15:00 IST |
| J | CPRBreakout | `strategies/cpr_breakout.py` | 1m | Trend/Trans | 2 | CPR from prev-day H/L/C; width <0.25%; 2 consecutive confirms; once/day |
| K | PairLeadership | `strategies/pair_leadership.py` | 5m | All | 21 | RELIANCE + HDFCBANK EMA20 bias → scoped to NIFTY only |
| L | MultiStrikeScalp | `strategies/multi_strike_scalp.py` | 1m | Trend/Trans | — | **DISABLED** (`enabled=False`); ATM±1 Vol Z + ATR + VWAP + candlestick |
| M | Ichimoku | `strategies/ichimoku.py` | 5m | Trend/Trans | 78 | Tenkan/Kijun/cloud; needs 78 bars → fires ~15:45 IST effectively |
| N | SMC_FVG_BOS | `strategies/smc.py` | 5m | All | 30 | Fair Value Gap + Break of Structure; detects institutional order flow |
| O | FibRetracement | `strategies/fibonacci_retracement.py` | 5m | Trend/Trans | 60 | EMA-21 slope + 38.2/50/61.8% bounce with prev-bar crossing check |
| P | StochRSI_Div | `strategies/stoch_rsi_divergence.py` | 5m | Ranging | 50 | Bullish/bearish divergence via StochRSI(14,14,3,3); 30-min cooldown |
| Q | MACD_Hist_Div | `strategies/macd_histogram_div.py` | 5m | Trend/Trans | 40 | MACD(12,26,9) histogram divergence; histogram sign required |
| R | ElliotWave | `strategies/elliott_wave.py` | 5m | Trend/Trans | 30 | Wave 3 entry detection on swing structure |
| S | Harmonic | `strategies/harmonic_pattern.py` | 5m | All | 30 | Gartley/Bat PRZ (Potential Reversal Zone) detection |
| T | CandleReversal | `strategies/candlestick_reversal.py` | 5m | All | 20 | Engulfing/Hammer/ShootingStar at VWAP proximity |
| U | DonchianBreakout | `strategies/donchian_breakout.py` | 5m | Trend/Trans | 25 | 20-bar Donchian channel breakout with volume confirm |
| V | MultiTFEMA | `strategies/multi_tf_ema.py` | 5m+15m | Trend/Trans | 78 | 15m trend filter + 5m EMA 9/21 crossover confluence |
| Y | VWAPSlope | *(inline in strategy_worker.py)* | 1m | All | 11 | 3-signal: slope + position + band-width; steep slope+price above VWAP+wide bands |
| Z | FlagBreakout | `agents/flag_breakout_worker.py` | 5m | Trend/Trans | 30 | Flag/pennant: flagpole + consolidation + breakout confirm |
| AA | TriangleBreakout | `agents/triangle_breakout_worker.py` | 5m | Trend/Trans | 30 | Ascending/Descending/Symmetrical triangle squeeze + breakout |
| AB | HSPattern | `agents/hs_pattern_worker.py` | 5m | Trend/Trans | 40 | Head & Shoulders / Inverse H&S neckline break |
| AC | PowerCandle_EMA44 | `strategies/power_candle.py` | 5m | Trend/Trans | 46 | EMA44 pullback: body≥60% + EMA touch + slope confirm + futures vol gate |
| AD | ScalerV2_EMA9 | `strategies/scalper_v2.py` | 1m | Trend/Trans | 11 | EMA9 price-break + RSI 60/40 (strong momentum only) |

### Daemon Workers (bypass MetaAgent — direct execution)

| Daemon | Worker ID | Phase | Trigger | Strategy |
|---|---|---|---|---|
| GammaBlastWorker | — | 20 | 8-factor score ≥ 7 (or 6 near expiry) | **Directional** BUY CE or PE based on spot momentum direction; straddle NOT used |
| VIXStraddleWorker | W | 22 | VIX pct60 < 25 AND pct120 < 25 | ATM straddle (CE+PE) — fires on low-IV regime |
| IronCondorWorker | X | 22 | VIX pct60 > 75 + RANGING regime | OTM iron condor Tue/Wed |
| WeeklyStrangleWorker | Z1 | 23 | Thu 15:25 IST + VIX < 20 | Sell OTM strangle; hold 1 week |
| RedDaySellerWorker | Z2 | 23 | Red > 0.5% + RSI < 40 (11:00–13:00) | Sell OTM CE |
| ExpiryBlastWorker | 4A | 4A | NIFTY expiry 14:40–15:03 IST | BUY CE on blast candle |
| FuturesBasisWorker | 3C | 3C | NF basis + OI + constituent lead-lag | → signal_queue (directional) |

**GammaBlast 8-factor scoring (all 8 implemented in `agents/gamma_blast_worker.py`):**

| Factor | Source | Max pts | Threshold |
|---|---|---|---|
| 1. Spot momentum | 1m candles | 2 | > 0.3% over 5 bars |
| 2. CE volume surge | Dhan quote poll | 2 | > 100% vs rolling avg |
| 3. PE volume surge | Dhan quote poll | 2 | > 100% vs rolling avg |
| 4. Index volume spike | 1m candles | 1 | > 2× 20-bar avg |
| 5. Bid-ask spread widening | Dhan quote poll | 1 | > 2× avg spread |
| 6. CE price move | Dhan quote poll | 1 | > 10% over 5 polls |
| 7. PE price move | Dhan quote poll | 1 | > 10% over 5 polls |
| 8. Consecutive moves | 1m candles | 1 | ≥ 3 bars same direction |

Score capped at 10. Direction from spot momentum sign; secondary: CE vs PE price change differential. Score ≥ 7 → BUY CE (BULLISH) or BUY PE (BEARISH). Score ≥ 6 near expiry (DTE=0 after 14:00, DTE=1 after 14:30). Cooldown: 60s normal, 30s expiry day.

---

## 5. Entry Filters (Order Placer)

Applied to **Telegram channel signals** in `order_placer_dhan_sandbox.py` before placing any order.

| Filter | Rule | Skip reason logged |
|---|---|---|
| **VWAP Filter** | Entry price > VWAP + 5% | `VWAP_FILTER` |
| **Chase Filter (Index)** | Entry price > day open × 130% | `CHASE_FILTER` |
| **Chase Filter (Stock)** | Entry price > day open × 160% | `CHASE_FILTER` |
| **Lot Size Cap** | lot_size > 200 (MCX exempt) | `LOT_SIZE_FILTER` |
| **MCX Routing** | NATURALGAS, CRUDEOIL options → Kite (not Dhan) | Kite order placed instead |
| **BSE FNO** | SENSEX options → sandbox limitation | Logged as `SANDBOX_RECORDED` |
| **Expired Contract** | Expiry date < today | Skipped at `_resolve_security_id` |

### OmniEngine Entry Gates (DhanOmniEngine.execute())

These apply specifically to algo-generated signals from OmniEngine workers:

| Gate | Rule |
|---|---|
| **Opening Gap Risk** | Index blocked if opening gap > 1.0% OR > 1.2× daily ATR. Checked once per day at 9:30 AM from 5m candles. Entire index suppressed for the session. |
| **DTE Strike Matrix** | DTE=0: 1-step ITM; DTE=1: ATM; DTE≥2: 1-step OTM. Applied after ATM resolution (`itm_shift=False`). |
| **Momentum Gate** | Underlying's last two 5m candles must confirm direction (CE=rising, PE=falling). ORB_VWAP exempt. |
| **Exhaustion Candle** | Signal suppressed if current candle range > 2.5× ATR(14). Momentum already spent. |
| **21-Candle S/R Gate** (Workers A, D) | BULLISH only when 1m close > 5m resistance AND > 15m resistance (21-candle rolling high, shift=1). BEARISH only when close < 5m support AND < 15m support. |
| **Burst Rate Limiter** | Max 4 algo orders per 5-minute window across all strategies. |
| **PCR OI Conviction Gate** | Block CE if `pcr_bias=="BEARISH"` (OI<0.7, no put-writing support); block PE if `pcr_bias=="BULLISH"` (OI>1.2, put-writers bullish). PCRFilter.pcr_bias already uses the exact OptionBuying thresholds. Pass-through if NEUTRAL or not ready. |
| **SL Exit Re-entry Blacklist** | When any strategy's position hits SL on a specific tradingsymbol (e.g. `NIFTY_24100_CE_29May26`), that exact option is blacklisted in `sl_exits_today.json` for the remainder of the session. All strategies are blocked from entering it again, regardless of which strategy now requests it. File resets on new calendar day. |
| **Daily Entry Cap** | Max `max_entries_per_strategy_per_day` (default 10) per strategy. |
| **Circuit Breaker** | No new entries if daily realized PnL ≤ `daily_loss_limit` (−50,000). |

---

## 6. Stop-Loss Management

Managed by `dhan_sl_monitor.py`. Polls open orders every ~5 seconds.

### ATR v4 Trailing SL Stages (3-stage — updated 2026-05-28)

Pure function in `sl_engine.py`. ATR mode activates when ATR data is available from `kite_candles.db`.

| Stage | Trigger | SL set to | Config key |
|---|---|---|---|
| `INITIAL` | Default on entry | Entry − 8% (index) / 5% (stock) | `initial_sl_percent` / `index_sl_percent` |
| `BREAKEVEN` | Gain ≥ **1.0 ATR** | Entry price (cost-free hold) | `atr_beven_mult = 1.0` |
| `PROFIT_LOCK` | Gain ≥ **1.5 ATR** | Entry + 0.75 ATR (locks partial profit) | `atr_lock_mult = 1.5`, `atr_lock_dist = 0.75` |
| `ATR_TRAILING` | Gain ≥ **2.5 ATR** | Peak − 1.5 ATR (active trail from peak) | `atr_trail_mult = 2.5`, `atr_trail_dist = 1.5` |

**Stage 2 (PROFIT_LOCK) rationale:** Without it, a position that reached 1.9 ATR gain and reversed would exit at breakeven — giving back all accrued profit. PROFIT_LOCK ensures exit at `entry + 0.75 ATR` minimum, locking a meaningful fraction before full trailing kicks in.

**Threshold changes vs v3:**
- `atr_beven_mult`: 0.5 → **1.0** (prevents premature breakeven on normal 5m noise)
- `atr_trail_mult`: 2.0 → **2.5** (activation threshold; trail distance kept tighter at 1.5 ATR)

**Percentage fallback** (when ATR unavailable): BREAKEVEN at 1% gain, trail at 2% gain (AM=3%, PM=2% from peak).

### SL Exit Re-entry Blacklist (added 2026-05-29)
Ported from Replica_Fin's `sl_exits.json` pattern.

**How it works:**
1. `dhan_sl_monitor._execute_exit()` — whenever `reason` is an SL-type exit (INITIAL_SL, TRAILING_SL, SL_BREAKEVEN, SL_PROFIT_LOCK, SL_ATR_TRAILING, ORB_INVALID, etc.), calls `_write_sl_exit(tradingsymbol)`
2. `_write_sl_exit()` appends the tradingsymbol to `MasterConfiguration/data/sl_exits_today.json` with today's date as the key
3. File format: `{"date": "2026-05-29", "exits": ["NIFTY_24100_CE_29May26", ...]}`
4. File is self-resetting: a stale date means yesterday's list, so it's ignored and cleared on next write
5. `DhanOmniEngine._is_sl_blacklisted(tradingsymbol)` — reads the file and returns True if symbol is listed and date matches today
6. New gate in `execute()` after PCR gate: if blacklisted → block entry for ALL strategies

**Why this matters:** DhanAPI's existing `_is_duplicate_strike_entry()` only blocks `(same_strategy, same_tradingsymbol)`. This SL blacklist blocks ALL strategies from re-entering an option that got stopped out — preventing whipsaw cross-strategy re-entries like EMA921 getting SL'd on NIFTY_24100_CE at 11:00, then PowerCandle entering the same strike at 11:05.

CUTOFF, TIME_SL, and EOD exits are NOT blacklisted — those are time-based exits, not SL failures. Only genuine stop-loss hits trigger the blacklist.

**EOD backup:** `dhan_sl_monitor.py` copies `sl_exits_today.json` to a timestamped archive once per day after 15:30 IST (triggered in the 2-min heartbeat loop). Format: `sl_exits_today_29May26_1532.json`. Skipped if file has zero exits. Archive stored alongside the live file in `MasterConfiguration/data/`.

### Price Sanity Guard (added 2026-05-28)
`dhan_sl_monitor.py` rejects any LTP outside `[₹0.05, ₹50,000]` before processing. Feed spikes / corrupt data are skipped silently with an error log. No SL action taken on corrupt ticks.

Spot-level sanity ranges (for future use in underlying checks):
`NIFTY: 15k-30k | BANKNIFTY: 40k-70k | FINNIFTY: 15k-35k | SENSEX: 60k-95k | MIDCPNIFTY: 8k-16k`

### Time-Based SL
- Position older than **15 minutes** AND total move < **1.0%** → exit

### Hard Cutoff
- All open positions forcibly closed at **15:25 IST**
- Exit price resolved via `_resolve_exit_ltp()` — Kite → WS → REST → candle DB → last-known LTP. **Never falls back to `entry_price`**.

### LTP Source Priority (updated 2026-05-22)
1. **Kite API batch `ltp()`** — PRIMARY
2. Dhan WebSocket (LTPFeed) — `0.0` ticks ignored
3. Dhan REST (`get_ltp_rest`) — tertiary
4. `kite_candles.db` latest close — 1-min delayed, final fallback
5. Last-known LTP from monitoring loop — absolute last resort at 15:25 only
6. Skip that cycle / leave OPEN if all fail

---

## 7. Execution Flow

### OmniEngine Signal → Order
```
DataAgent cycle (every 10s):
  → sync_data()              # refresh all candle buffers
  → inject_futures_volume()  # replace zero volume with front-month futures volume
  → opening_gap_check()      # block index for session if gap > 1% or > 1.2×ATR
  → regime_detect()          # ADX per index → TRENDING/RANGING/TRANSITION
  → fan-out MarketSnapshot to all 28 StrategyWorkers (A–V + Y + Z + AA–AD)
  → non-NEUTRAL signals → signal_queue
  → MetaAgent (RAG+LLM filter) → approved_queue
  → ExecutionAgent → engine.execute()
  → [DTE shift] → [PCR gate] → [SL blacklist] → [Burst gate] → order placed
```

### Lot Sizes (Futures reference, options use same)
| Index | Lot size |
|---|---|
| NIFTY | 65 |
| BANKNIFTY | 30 |
| FINNIFTY | 60 |
| SENSEX | 20 |
| MIDCPNIFTY | 120 |

> Lot sizes verified from live trade data (May 2026). Earlier documentation incorrectly listed NIFTY=75, BANKNIFTY=35, FINNIFTY=65, MIDCPNIFTY=75 — these reflected pre-2025 values.

---

## 8. How to Run

```bash
cd C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI

# Start all 9 components in background
python start_trading_system.py start

# Stop all
python start_trading_system.py stop

# Restart
python start_trading_system.py restart

# Run heartbeat watchdog standalone (if not started via start_trading_system.py)
python watchdog.py
```

**Dashboard:** http://127.0.0.1:5050

Dashboard tabs (as of 2026-05-28):
- **Live** — open positions, unrealized P&L, SL stage
- **Signals** — TG channel signals
- **Paper Trades** — all orders placed
- **Strategy Signals** — OmniEngine signal log
- **History** — closed trades
- **Agents** — process status + KB win-rates
- **Swing** — overnight swing setups
- **FA** — FA paper portfolio
- **Anomalies** — price/vol anomaly alerts
- **Patterns** — candlestick pattern scanner
- **PA Setups** — price action structure + zones
- **Commodities** — MCX signals + paper trade performance
- **⚡ Leading** — Leading Indicator Engine snapshots + signals *(added 2026-05-28)*

**Python environment:** `C:\ProgramData\anaconda3\python.exe`

### Pre-Market Checklist (`day_start_checks.py` each morning)

| Check | What it verifies |
|---|---|
| 1. Dhan API Token | `get_fund_limits()` or strike lookup (sandbox) |
| 2. **Kite API Token** | `kite.profile()` + NFO/BFO instrument count |
| 3. Scrip master CSV | File exists and age < 24h |
| 4. Leftover open orders | Count of OPEN orders from previous session |
| 5. Strike lookup | ATM option resolution for NIFTY/BANKNIFTY/FINNIFTY |

> **Kite token:** expires daily at midnight IST. Must be refreshed before 9:15 AM. All MCX candle data, futures candles, OBI/depth, and option chain polling depend on a valid Kite token.

### Scheduled Background Tasks

| Task | Cadence | Purpose |
|---|---|---|
| `DhanNightlyLearn` | Weekdays 16:00 IST | EOD whatif backtest + KB embedding |
| `DhanKBAudit` | Daily 16:30 IST (self-gates to every 2nd trading session) | KB snapshot + per-strategy win-rate audit |

---

## 9. Learning Loop & KB Audit

### Components

| File | Role |
|---|---|
| `rag/knowledge_base.py` | ChromaDB wrapper — `upsert`, `query`, `count`, `win_rate(strategy=...)` |
| `rag/trade_embedder.py` | Reads `whatif_trades` → embeds → upserts into ChromaDB |
| `rag/nightly_learn.py` | Phase 3 orchestrator: EOD backtest then incremental embed |
| `rag/audit_kb_growth.py` | Self-gating periodic audit |

### KB Baseline (2026-05-01)
- ChromaDB: 94 documents (Apr 16–Apr 24 trades)
- Overall win-rate: 45.9%

### WhatIf Transaction Costs (added 2026-05-29)
`eod_whatif_backtest.py` now subtracts realistic Indian market friction from `pnl_total`:

| Cost component | Rate | Applied to |
|---|---|---|
| Bid-ask spread simulation | 0.5% of notional | Both legs |
| Brokerage | ₹20 flat | Both legs |
| GST on brokerage + exchange | 18% | Both legs |
| Exchange transaction charge | 0.053% of notional | Both legs |
| STT (Securities Transaction Tax) | 0.125% of notional | Exit leg (sell) only |
| Stamp duty | 0.003% of notional | Entry leg (buy) only |

`_compute_txn_cost(premium, lot_size, is_sell)` in `eod_whatif_backtest.py`.
Round-trip cost per typical NIFTY 65-lot trade at ₹200 premium ≈ **₹650-750**.
`pnl_total` in `whatif_trades` now reflects net-of-costs P&L. Column `txn_cost` stores the gross cost. Result label (`PROFIT`/`LOSS`) also uses post-cost P&L.

---

## 10. TG Channel Parsers

### Channel Map
Defined in `channel_parsers.py` → `CHANNEL_PARSER_MAP`. Lookup key is `str(event.chat_id)`.

#### All registered channels (as of 2026-05-21)

| Channel ID | Channel Name | Parser |
|---|---|---|
| `-1001858110716` | INDEX OPTIONS PRIME | `shortterm` |
| `-1001903138387` | COPY MY TRADES BANKNIFTY | `shortterm` |
| `-1001670038276` | STOCK OPTIONS PRIME | `shortterm` |
| `-1001542890753` | BTST EQUITY CASH AND FUTURES | `shortterm` |
| `-1001404315099` | FUTURES SEGMENT BATCH | `shortterm` |
| `-1003089362819` | Wealth World Trading Hub | `wealthworld` |
| `-1003658135032` | SIDHARTH SINGH PREMIUM | `sidharth` |
| `-1003282204738` | JP Paper trade | `jp` |
| `-1001478345624` | VISION BY SMK | `generic` |
| `-1003053351657` | STOCK MARKET TRADING TIPS | `generic` |
| `-1001822833953` | COMMODITY OPTIONS PRIME | `generic` |
| `-1003800707569` | Momentum to Multibagger - Chikoutrader | `generic` |
| `luxurywithtrading` | @luxurywithtrading *(public channel)* | `generic` |

### Generic Parser MCX Commodity Options

**Supported MCX symbols:**

| Raw name(s) | Canonical (Kite) |
|---|---|
| GOLD | `GOLD` |
| GOLD MINI | `GOLDM` |
| SILVER / SILVER MINI | `SILVER` / `SILVERM` |
| CRUDEOIL, CRUDE OIL | `CRUDEOIL` |
| NATURAL GAS, NATURALGAS | `NATURALGAS` |
| COPPER / ZINC / NICKEL / LEAD / ALUMINIUM | same |

---

## 11. Phase 6A — MCX Commodity Brain

### Overview
Three co-operating daemons (all in `DhanOmniEngine_v2.py`):

| Daemon | File | Role |
|---|---|---|
| `MCXWorker` | `agents/commodity_brain.py` | Polls DXY every 5 min (Stooq); manages EIA blackout windows |
| `MCXCandleCollector` | `agents/mcx_candle_collector.py` | Fetches 1M OHLCV for 5 MCX instruments; Kite primary, Dhan fallback |
| `CommodityBrain` | `agents/commodity_brain.py` | Signal engine: EMA + SuperTrend + VWAP + ORB + DXY |

### MCX Candle Collection (updated 2026-05-27 — Kite migration)
- Dhan sandbox does not support `MCX_COMM` exchange segment for intraday data
- **Kite `historical_data()`** now primary for full MCX session 09:00–23:30 IST
- `MCXKiteLookup` (`core/mcx_kite_lookup.py`) resolves front-month Kite token per symbol daily
- COPPERM → mapped to COPPER on Kite (no mini contract available)
- After Kite token refresh (2026-05-27 23:47): 870 candles per symbol confirmed at correct INR prices

### CommodityBrain Signal Logic
Market hours: Mon–Fri 09:00–23:30 IST | Scan cycle: ~5 min

**Score computation (−5 to +5):**
| Signal | Bullish | Bearish |
|---|---|---|
| EMA cross (9/21) | +1 | −1 |
| SuperTrend | +1 | −1 |
| VWAP position | +1 | −1 |
| ORB breakout | +1 | −1 |
| DXY correlation | +1 (metals: DXY ↓) | −1 |

Signal fires at `abs(score) >= 3`. Written to `commodity_signals` table.

**Safety gates:** EIA blackout (Wed 20:00, Fri 20:00 IST for NaturalGas); DXY guard via MCXWorker singleton.

**Data source fallback:**
1. MCX 1M candles from `mcx_candles.db` (INR, Kite) — preferred
2. yfinance international USD proxies — fallback when candles empty

### Paper Trade Tracking (added 2026-05-27)

**Table:** `commodity_paper_trades` (trading.db)

| Column | Description |
|---|---|
| signal_id | UNIQUE FK to commodity_signals |
| symbol, direction, score | From the signal |
| entry_price, stop_price, target_1, target_2 | From signal |
| status | OPEN → TP1 / TP2 / SL / EXPIRED |
| exit_price, exit_reason, pnl_pts, r_multiple | Filled on close |
| opened_at, closed_at | Timestamps |
| data_source, eia_blocked | Metadata |

**Lifecycle:**
- Created by `_create_paper_trade()` when signal fires; deduped via `signal_id UNIQUE`
- Monitored by `_check_open_trades()` each cycle: walks 1M candles chronologically → SL check first, then TP2, TP1
- Expired by `_expire_open_trades()` at MCX close (23:30 IST) transition
- R-multiple = pnl_pts / risk_pts

**Dashboard endpoint:** `GET /api/commodity_paper_trades` — returns open trades, last 100 closed, stats (win rate, avg R, expected value, by-symbol, by-score breakdowns).

**Dashboard panel:** Inside Commodities tab — stats bar + by-symbol + by-score tables + open trades table + closed trades table with result pills (🎯🎯 TP2 / 🎯 TP1 / 🛑 SL / ⏰ Expired).

---

## 12. Phase 7A — Leading Indicator Engine

### Overview
**File:** `agents/leading_indicator_engine.py`  
**Supporting files:** `core/oi_chain.py`, `core/leading_store.py`  
**Indices:** NIFTY, BANKNIFTY, SENSEX  
**Market hours:** Mon–Fri 09:00–15:30 IST  
**Scan interval:** 60 seconds  

### Metrics Computed Per Cycle

#### Order Book Imbalance (OBI)
```
OBI = (bid_qty − ask_qty) / (bid_qty + ask_qty) × 100   (−100 .. +100)
```
- Source: `kite.quote(futures_token)` depth field, 5 levels aggregated
- Resolves futures token daily via `get_futures_kite_token()`
- Fallback: `kite.quote("NFO:NIFTY_FUT")` symbol-based lookup

#### Volume Delta
```
VolDelta     = Σ sign(close − open) × volume   (last 5 1-min futures bars)
vol_delta_pct = VolDelta / Σ|volume|            (−1 .. +1)
```
- Source: `candles_futures_1min` in `kite_candles.db`
- Approximation for tick-level aggressor classification (no tick data available)

#### PCR & Drift (OIChain)
**File:** `core/oi_chain.py`
```
PCR       = Σ(PE_OI) / Σ(CE_OI)    across ATM ± 8 strikes
pcr_drift = PCR_now − PCR_3_readings_ago
```
- `kite.instruments(exchange)` loaded once per calendar day
- Nearest weekly/monthly expiry selected automatically
- 4-element deque for drift computation

#### GEX Zone (simplified — no Black-Scholes)
```
put_wall  = strike with max PE OI
call_wall = strike with max CE OI

positive  → spot pinned between put_wall and call_wall  (mean-reverting)
negative  → spot outside both walls                     (trending/breakout)
near_zero → spot within 0.5 × strike_inc of either wall (potential reversal)
```

### Signal Generation

**Confidence scoring (0–100):**
| Component | Max pts | Threshold |
|---|---|---|
| OBI | 35 | ≥70→35, ≥50→25, ≥30→15, else 5 |
| VolDelta | 30 | ≥0.6→30, ≥0.4→20, ≥0.2→10 |
| PCR drift | 20 | ≥0.15→20, ≥0.08→12, ≥0.03→6 |
| GEX zone bonus | 15 | 15 if zone aligns with signal direction |

**Signal types:**
| Type | Conditions | Execution window |
|---|---|---|
| `Leading_Breakout` | OBI + VolDelta aligned, `abs(OBI)≥30`, confidence≥55 | 5 min |
| `Leading_Reversal` | PCR extreme (>1.4 or <0.6), GEX ≠ negative, confidence≥55 | 3 min |

**SL/Target:** Breakout: ±0.3% SL, nearest wall as target. Reversal: ±0.4% SL, nearest wall as target.

### Storage
**DB:** `trading.db`

`leading_snapshots` — 1 row per index per minute:
`id, index_name, spot_price, obi, vol_delta, vol_delta_pct, pcr, pcr_drift, gex_zone, put_wall, call_wall, atm_strike, snapped_at`

`leading_signals` — fired signals:
`id, index_name, signal_type, confidence_score, execution_window_mins, entry_price, stop_loss, target, risk_reward, obi, vol_delta_pct, pcr, pcr_drift, gex_zone, nearest_magnet, status, detected_at`

### Dashboard
**Endpoint:** `GET /api/leading_signals?hours=24&index=NIFTY`

**Tab:** "⚡ Leading" — live snapshots per index + signal history table with full metrics. Auto-refreshes every 10s when active.

### Wire-up in OmniEngine
```python
from agents.leading_indicator_engine import LeadingIndicatorEngine
from core.leading_store import ensure_leading_tables
ensure_leading_tables()
leading_engine = LeadingIndicatorEngine()
# in all_threads; stop() called on shutdown
```

---

## 13. Fundamental Analysis Agent (Phase 4B)

### Overview
**File:** `agents/fundamental_agent.py`  
**Bootstrap:** run `fa_setup.py` once to seed initial picks  
**Schedule:** Saturday 10:00 IST — full scan; Weekday 16:30 — price update

### Scoring Components (TICKER_ALIAS fix 2026-05-27)
yfinance is primary data source for FA scoring. Some NSE symbols don't map directly to Yahoo Finance tickers.

**`_TICKER_ALIAS` dict in `core/fa_scorer.py`:**
```python
_TICKER_ALIAS = {
    "TATAMOTORS": ["TMCV.NS", "TMPV.NS", "TATAMOTORS.BO", "TATAMOTOR.NS"],
    "LTIM":       ["LTM.NS", "LTIMINDTREE.NS", "LTIMINDTREE.BO"],
}
```
- When `.NS` ticker fails, aliases are tried in order
- Logs `WARNING: Yahoo Finance data unavailable` vs `no data (delisted or wrong symbol)`

### FA Score Snapshot (post-bootstrap, 2026-05-27)
| Symbol | Score |
|---|---|
| NATIONALUM | 88.5 |
| BPCL | 71.0 |
| HINDPETRO | 71.0 |
| VSTIND | 71.0 |
| TCS | 68.5 |
| COALINDIA | 66.5 |
| OFSS | 66.5 |

---

## 14. Watchdog Heartbeat System (added 2026-05-28)

### Overview

Two-layer process health monitoring:

| Layer | File | Mechanism | Detects |
|---|---|---|---|
| Layer 1 | `start_trading_system.py watchdog` | PID alive/dead via psutil | Crashed processes |
| Layer 2 | `watchdog.py` | DB heartbeat timestamps | Hung processes (alive but loop stuck) |

The PID-based watchdog is registered in Windows Task Scheduler (`TradingSystemAutoStart`) and starts at 09:05 IST Mon-Fri. The heartbeat watchdog starts as the 9th process in `PROCESSES`.

### DB Schema — `system_status` (trading.db)

```sql
CREATE TABLE system_status (
    agent_name     TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | WARMUP | STOPPED
    last_heartbeat TEXT NOT NULL,                    -- ISO timestamp
    pid            INTEGER,                          -- owning process PID
    notes          TEXT DEFAULT ''
)
```

### Heartbeat Sources

| Agent name | Written by | Frequency | Maps to process |
|---|---|---|---|
| `data_agent` | `DataAgent._cycle()` | Every 10s (POLL_INTERVAL) | OmniEngine |
| `sl_monitor` | `dhan_sl_monitor.py` main loop | Every 5s | SL Monitor |
| `watchdog` | `watchdog.py` own loop | Every 30s (self-heartbeat) | Watchdog |

### WARMUP Guard

Before restarting a hung process, `watchdog.py` writes `status='WARMUP'` for that agent. The watchdog skips WARMUP rows on subsequent checks for `WARMUP_SECS` (90s). This prevents cascade restarts while the fresh process loads historical data.

Flow on stale heartbeat:
1. `set_warmup(agent_name)` — DB row updated immediately
2. Kill existing PID (psutil → taskkill fallback)
3. `_launch_process(process_name)` — spawn fresh process
4. Update `.trading_pids.json`
5. TG alert: `⚠️ WATCHDOG: {agent} heartbeat stale ({age}s) → restarting {process}`
6. On next cycle, fresh agent writes `heartbeat(agent_name, 'ACTIVE')` → watchdog resumes normal monitoring

### Store API (`core/watchdog_store.py`)

```python
heartbeat(agent_name, notes="")       # write ACTIVE row from agent loop
set_warmup(agent_name, notes="")      # mark WARMUP before restarting
set_stopped(agent_name)               # mark STOPPED on clean shutdown
get_all_statuses() -> list[dict]      # read all rows (used by watchdog.py)
ensure_table()                        # called once at OmniEngine startup
```

### Tuning

| Constant | Default | Description |
|---|---|---|
| `POLL_SECS` | 30 | Watchdog check interval |
| `STALE_SECS` | 300 | Age (seconds) that triggers restart |
| `WARMUP_SECS` | 90 | Post-restart cooldown before re-checking |

---

## 15. Strategy Performance (May 7–21, 2026)

### Daily PNL (post all corrections, May 7 onwards)

| Date | Trades | PNL | Notes |
|---|---|---|---|
| May 7 | 134 | +₹2,54,039 | Expiry day, large NIFTY rally |
| May 8 | 10 | −₹13,159 | |
| May 11 | 38 | +₹73,738 | |
| May 12 | 19 | −₹2,221 | |
| May 13 | 39 | −₹37,955 | |
| May 14 | 15 | −₹18,346 | |
| May 15 | 59 | −₹37,481 | |
| May 18 | 13 | +₹23,830 | |
| May 19 | 51 | +₹23,839 | |
| May 20 | 39 | +₹28,581 | |
| May 21 | 32 | **+₹1,11,894** | 13 SENSEX positions corrected — ltp=0 bug |

### Bugs Fixed (2026-05-22) — `dhan_sl_monitor.py`
Root cause of May 21 SENSEX PNL=0 on 13 positions: Dhan WebSocket returns silent `0.0` for BSE_FNO; `_check_hard_cutoff` had separate LTP path that bypassed Kite and fell to `or trade["entry_price"]`. Three fixes:
1. Never cache `price=0` from Kite batch response
2. `_get_candle_ltp`: return `None` (not `0.0`) when DB has no price
3. `_resolve_exit_ltp()` shared helper used at both SL check and 15:25 cutoff — never uses `entry_price`

### Data Reliability
All May 7–21 corrections applied as of 2026-05-22. May 7 phantom losses (spot price as option entry, ~₹16L per order) corrected via Kite 1m sweep.

---

## 16. Known Limitations & Open Gaps

### Infrastructure

| Issue | Status | Detail |
|---|---|---|
| **Kite token automation** | ⚠️ OPEN — highest risk | Expires daily midnight IST. Must be refreshed manually before 9:15 AM. ALL price data (LTP, candles, futures, OBI depth, option chain) depends on a valid token. Automation blocker for live money deployment. |
| Dhan sandbox market data | Known | `/v2/marketfeed/ltp` returns 404 — Kite promoted to primary LTP source as workaround |
| Dhan sandbox MCX candles | Known | `MCX_COMM` returns no data — Kite `historical_data()` primary, Dhan fallback |
| SENSEX BSE_FNO order placement | Known | `SANDBOX_RECORDED` logged (Dhan sandbox limitation) |
| ~~SENSEX BSE_FNO LTP bug~~ | ✅ Fixed 2026-05-22 | `_resolve_exit_ltp()` unified; Kite primary |
| SL blacklist JSON race condition | Low risk paper | `_write_sl_exit()` uses `_sl_exits_lock` (intra-process only). `DhanOmniEngine.py` reads without a lock. Two concurrent writes from separate processes could corrupt JSON. Acceptable for paper trading; use DB approach for live money. |

### Strategy Limitations

| Issue | Detail |
|---|---|
| **Futures Volume VWAP dependency** | `inject_futures_volume()` reads today's futures candles from `kite_candles.db`. If `futures_candle_store` hasn't populated today's data yet (early morning), `df_fut` returns None and the old TWAP proxy is used. Strategies that fire before 09:30 may use stale VWAP. |
| TriplePattern (F) | Needs 60 × 5m candles → fires only after ~14:15 IST |
| Ichimoku (M) | Needs 78 × 5m candles → fires only after ~15:45 IST (effectively end-of-day) |
| OptionScalper (B) | Needs 46 × 5m candles → fires only after ~12:45 PM IST |
| MultiStrikeScalp (L) | **DISABLED** (`enabled=False`). Awaiting richer option-candle backtest data to confirm deploy criteria (2–10 signals/day, WR ≥ 55%, avg winner ≥ 1.8× avg loser). |
| PairLeadership (K) | Scoped to NIFTY only. Relies on RELIANCE + HDFCBANK 5m candles via Kite. Returns NEUTRAL in sandbox if candle data unavailable. |
| AC + AD workers (new 2026-05-29) | No KB history yet — MetaAgent may apply conservative filtering until 10+ trades accumulate per worker |
| VIXStraddleWorker (W) | Has not fired: pct120 = 52.5% (needs < 25%). VIX 120-day window includes sub-10 VIX period (Jan–Feb 2026), making today's VIX 14.98 appear median. |
| IronCondorWorker (X) | Has not fired: pct60 = 6.7% (needs > 75%). Requires high-IV environment — opposite of current low-VIX regime. |
| WeeklyStrangleWorker (Z1) | Should fire next Thu June 4 at 15:25 IST (VIX = 14.98 < 20 ✅). |

### Data & DB

| Issue | Detail |
|---|---|
| COPPERM on Kite | Maps to full COPPER (1000kg lot) — no mini contract on Kite MCX |
| MCX `commodity_signals.status` | `'ACTIVE'` never auto-expires (only `commodity_paper_trades` gets expired). Stale rows from before wiring remain in DB permanently but cause no harm. |
| Leading Engine — OBI in sandbox | `kite.quote()` depth may return empty in sandbox. OBI defaults to 0.0; signals still fire on PCR+VolDelta confidence. |
| Leading Engine — PCR pre-market | Option chain OI not populated until 09:15. First 1–2 cycles may return pcr=1.0 (neutral). |
| ~~Volume z-score bypass~~ | ✅ Fixed 2026-05-29: `inject_futures_volume()` now replaces zero spot volume with futures volume. All volume-based filters (z-score, VWAP, vol gate) work correctly when futures candles are available. |
| Lot sizes (2025 change) | NIFTY=65, BANKNIFTY=30, FINNIFTY=60, MIDCPNIFTY=120. Pre-2025 values (75/35/65/75) in old code are obsolete. |
| KB size | 1175 trades (2026-05-28). MetaAgent passes through signals with < 3 KB matches. New workers (AC, AD) will have low KB coverage until ~10–15 sessions accumulate. |

### Live Deployment Blockers (before switching to real money)
1. **Kite token automation** — manual refresh is unacceptable for unattended live trading
2. **SL blacklist race condition** — move from JSON to `trading.db` table for multi-process safety
3. **Paper → Live mode switch** — `order_placer_dhan_sandbox.py` hard-codes paper simulation; needs dual-mode switch with live_trading_config.json flag
4. **Position sizing** — currently 1 lot per signal; no Kelly criterion or volatility-adjusted sizing

---

## Holiday Calendar 2026 (NSE + BSE)

```
Jan 26  Republic Day
Mar  3  Holi
Mar 26  Ram Navami
Apr  3  Good Friday
Apr 14  Ambedkar Jayanti
May  1  Maharashtra Day
May 28  Bakri Eid               ← confirmed in expiry_calendar.py
Jun 26  Moharram
Sep 14  Ganesh Chaturthi
Oct  2  Gandhi Jayanti
Oct 20  Dussehra
Nov 10  Diwali (Balipratipada)
Nov 24  Guru Nanak Jayanti
Dec 25  Christmas
```

> MCX may observe different holidays. CommodityBrain checks Mon–Fri + MCX hours (09:00–23:30) only — it does not block on NSE equity holidays. MCX-specific holiday gate not yet implemented.
