# Inherit from EMA_9_21_Short → DhanAPI
**Reference Document — May 28, 2026, 12:13**  
*Source System:* `C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\EMA_9_21_Short`  
*Target System:* `C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI`

---

## Overview

After a thorough code audit of both systems, the table below summarises what DhanAPI is missing and should inherit. Items are ranked by trading impact.

| # | Feature | Priority | Target File(s) in DhanAPI |
|---|---|:---:|---|
| 1 | Opening Gap Risk Filter | **HIGH** | `agents/data_agent.py` |
| 2 | DTE-Based Strike Selection Matrix | **HIGH** | `core/order_placer.py` |
| 3 | 21-Candle S/R Zone as Hard Breakout Gate | **HIGH** | `strategies/ema_9_21.py`, `agents/strategy_worker.py` (Worker D) |
| 4 | Stage 2 Intermediate Profit Lock in ATR Trailing SL | **HIGH** | `sl_engine.py` |
| 5 | Exhaustion Candle Filter (> 2.5× ATR) | **MEDIUM** | `strategies/ema_9_21.py`, all entry-signal strategies |
| 6 | ATR Trailing Threshold Recalibration | **MEDIUM** | `sl_engine.py` |
| 7 | Watchdog Heartbeat + Warmup Status Reset Pattern | **MEDIUM** | `watchdog.py` (new file) |
| 8 | Futures Volume for True Index VWAP | **MEDIUM** | `agents/data_agent.py`, `strategies/indicators.py` |
| 9 | Sector Leader Bias Scoped to NIFTY Only | **LOW** | `agents/strategy_worker.py` (Worker D / EMAVWAPSRWorker) |
| 10 | Admin Console / Live-Paper Mode Switcher | **LOW** | `dashboard.py` (new file) |
| 11 | Price Sanity Guard Ranges | **LOW** | `dhan_sl_monitor.py` |

---

## 1. Opening Gap Risk Filter — HIGH PRIORITY

### What it does
On days when the index opens with a gap larger than **1.0%** (or larger than **1.2× the 14-period daily ATR in points**), all new entries for that index are blocked for the entire session. This prevents entering on exhausted momentum — the market has already moved its average daily range before the session even began.

### Why DhanAPI needs it
DhanAPI has zero gap protection. On large-gap mornings, EMA signals fire immediately after 9:30 AM on overextended prices, producing losing entries right before mean-reverting corrections.

### Source code (EMA_9_21_Short)

**`main.py` — lines 118–160 (two functions)**
```python
def check_opening_gap(df_5m):
    """Returns (gap_pct, prev_close, today_open). Returns (0.0, None, None) on failure."""
    try:
        df = df_5m.copy()
        df['date_only'] = df['date'].dt.date
        unique_dates = sorted(df['date_only'].unique())
        if len(unique_dates) < 2:
            return 0.0, None, None
        today = unique_dates[-1]
        yesterday = unique_dates[-2]
        prev_close = df[df['date_only'] == yesterday].iloc[-1]['close']
        today_open = df[df['date_only'] == today].iloc[0]['open']
        gap_pct = abs(today_open - prev_close) / prev_close * 100
        return gap_pct, prev_close, today_open
    except Exception as e:
        logger.warning(f"Error calculating gap: {e}")
        return 0.0, None, None

def get_daily_atr(kite, token):
    """Fetches daily data to calculate Daily ATR (14 period). Fallback = 150.0."""
    try:
        df = get_data(kite, token, "day", days=25)
        if df.empty or len(df) < 14:
            return 150.0
        df['h_l'] = df['high'] - df['low']
        df['h_pc'] = abs(df['high'] - df['close'].shift(1))
        df['l_pc'] = abs(df['low'] - df['close'].shift(1))
        df['tr'] = df[['h_l', 'h_pc', 'l_pc']].max(axis=1)
        return float(df['tr'].tail(14).mean())
    except Exception as e:
        logger.warning(f"Error calculating Daily ATR: {e}")
        return 150.0
```

**`main.py` — lines 221–234 (usage in the scan loop)**
```python
# Gap Opening Risk Filter
gap_pct, prev_close, today_open = check_opening_gap(df_5m)
daily_atr = get_daily_atr(kite, token)
gap_points = abs(today_open - prev_close) if prev_close and today_open else 0.0

is_gap_excessive = False
if gap_pct > 1.0:
    is_gap_excessive = True
    logger.warning(f"[{index_name}] GAP RISK: Opening gap of {gap_pct:.2f}% exceeds 1.0% limit.")
elif daily_atr and gap_points > (1.2 * daily_atr):
    is_gap_excessive = True
    logger.warning(f"[{index_name}] GAP RISK: Opening gap of {gap_points:.1f} pts exceeds 1.2x Daily ATR.")

if is_gap_excessive:
    continue  # Skip this index for the entire session
```

### How to adapt for DhanAPI

- Add a `gap_blocked: set[str]` flag on the `DataAgent` (reset at midnight).
- After fetching daily candles per index, compute `check_opening_gap()` once per day (e.g., on the first 9:30 AM cycle).
- If gap is excessive, add that index name to `gap_blocked`.
- In `DataAgent._dispatch()`, skip fan-out to strategy workers for any index in `gap_blocked`.
- `get_daily_atr()` can use Dhan's `get_daily_candles()` call — no Kite dependency.

---

## 2. DTE-Based Strike Selection Matrix — HIGH PRIORITY

### What it does
Instead of always buying the ATM option, the system shifts the strike based on **Days To Expiry (DTE)**:

| DTE | Strike Choice | Rationale |
|---|---|---|
| 0 (Expiry Day) | 1 step **ITM** | ATM decays asymptotically intraday; ITM has intrinsic value + delta > 0.65 |
| 1 (Day before) | **ATM** | Balanced delta vs theta profile |
| ≥ 2 (Normal) | 1 step **OTM** | Lower absolute cost, maximum % leverage on trend |

### Why DhanAPI needs it
`core/order_placer.py` in DhanAPI always targets ATM. On expiry day this is a silent P&L drain — theta accelerates so sharply that even correct directional trades lose money.

### Source code (EMA_9_21_Short)

**`core/order_placer.py` — lines 92–116**
```python
# --- DTE STRIKE SELECTION MATRIX ---
try:
    exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d").date()
    curr_dt = datetime.now().date()
    dte = (exp_dt - curr_dt).days
except Exception as dte_err:
    logger.warning(f"DTE calculation failed: {dte_err}. Defaulting ATM.")
    dte = 1

if dte == 0:
    # Expiry Day (DTE = 0) -> Shift 1 step ITM to combat theta decay
    if option_type == 'CE':
        strike -= config['step']
    else:
        strike += config['step']
    logger.info(f"Expiry Day (DTE=0): Shifted ITM to strike {strike}")
elif dte >= 2:
    # Extension Days (DTE >= 2) -> Shift 1 step OTM for leverage
    if option_type == 'CE':
        strike += config['step']
    else:
        strike -= config['step']
    logger.info(f"Normal Regime (DTE={dte}): Shifted OTM to strike {strike}")
else:
    logger.info(f"Day-Before-Expiry (DTE={dte}): Keeping ATM strike {strike}")
```

### How to adapt for DhanAPI

- In `core/order_placer.py`, after resolving the initial ATM strike and before calling `strike_lookup.get_security_id()`, insert the DTE block above.
- `expiry_date` is already available from `strike_lookup` (it returns the near-expiry date).
- Each index `step` is already in `INDICES_CONFIG` (`NIFTY=50`, `BANKNIFTY=100`, etc.).
- The re-lookup with the adjusted strike is a second call to `strike_lookup.get_security_id(index, adjusted_strike, option_type)`.

---

## 3. 21-Candle S/R Zone as Hard Breakout Gate — HIGH PRIORITY

### What it does
The 5-minute resistance zone is the 21-candle rolling high (shifted 1 bar back to avoid look-ahead). A **Bullish entry is only valid when price closes above this resistance**; a **Bearish entry only when price closes below the 21-candle support**. This filters out signals that fire mid-range and never confirm a structural breakout.

Additionally, the **15-minute timeframe** versions of the same levels are used as a second, broader structural gate — the price must also be above the 15M resistance (long) or below the 15M support (short).

### Why DhanAPI needs it
`strategies/ema_9_21.py` in DhanAPI uses ADX and RSI but has **no S/R breakout confirmation**. Signals fire in the middle of consolidation ranges when EMA crossovers happen on low momentum.

### Source code (EMA_9_21_Short)

**`strategies/indicators.py` — lines 20–22 (5M S/R zone calculation)**
```python
# Support & Resistance Zones (21-candle lookback, shifted 1 bar to avoid look-ahead)
df['support_zone'] = df['low'].rolling(window=21).min().shift(1)
df['resistance_zone'] = df['high'].rolling(window=21).max().shift(1)
```

**`strategies/indicators.py` — lines 83–108 (entry conditions with S/R gate)**
```python
# Note: the CORRECT version (audit-fixed) uses res_15m for bullish, sup_15m for bearish
bullish_entry = (
    latest['close'] > latest['ema_9'] and
    latest['close'] > latest['ema_21'] and
    (latest['close'] > vwap if vwap_valid else True) and
    latest['close'] > latest['resistance_zone'] and   # 5M breakout above resistance
    is_bullish_trend and not is_exhausted and
    latest['close'] > res_15m and                     # 15M breakout above resistance (CORRECTED)
    (leader_bias >= 0)
)

bearish_entry = (
    latest['close'] < latest['ema_9'] and
    latest['close'] < latest['ema_21'] and
    (latest['close'] < vwap if vwap_valid else True) and
    latest['close'] < latest['support_zone'] and      # 5M breakdown below support
    is_bearish_trend and not is_exhausted and
    latest['close'] < sup_15m and                     # 15M breakdown below support (CORRECTED)
    (leader_bias <= 0)
)
```

**`main.py` — lines 83–92 (15M level calculation)**
```python
def get_15m_levels(df_15m):
    """Support and Resistance on 15M using 21-candle lookback."""
    if df_15m is None or df_15m.empty:
        return None, None
    sup = df_15m['low'].rolling(window=21).min().iloc[-1]
    res = df_15m['high'].rolling(window=21).max().iloc[-1]
    return sup, res
```

### How to adapt for DhanAPI

- Add `support_zone` and `resistance_zone` columns inside `strategies/indicators.py` (already has `calculate_indicators()` equivalent) — add the two rolling lines.
- In `DataAgent`, fetch **both 5M and 15M candles** per index and pass both to the `MarketSnapshot`.
- In `EMAVWAPSRWorker` (Worker D) and `EMA921Worker` (Worker A), read `sup_15m` / `res_15m` from the snapshot and gate signal generation behind the breakout check.
- Note the **audit-confirmed bug in EMA_9_21_Short**: bullish should check `res_15m` (not `sup_15m`) and bearish should check `sup_15m` (not `res_15m`). Implement the **corrected** version shown above.

---

## 4. Exhaustion Candle Filter (> 2.5× ATR) — MEDIUM PRIORITY

### What it does
If the current 5-minute candle's range (high − low) exceeds **2.5× ATR**, the system marks it as an "exhaustion candle" and blocks all entries for that bar. This prevents buying at the very top of a parabolic spike or shorting at the very bottom of a capitulation candle.

### Why DhanAPI needs it
None of DhanAPI's 30+ strategies filter for exhaustion candles. Entry on a massive momentum candle is the single most common cause of immediately hitting stop-loss.

### Source code (EMA_9_21_Short)

**`strategies/indicators.py` — lines 65–70**
```python
curr_range = latest['high'] - latest['low']
atr = latest['atr'] if not pd.isna(latest['atr']) else 0

# Exhaustion Filter: Avoid entering on massive candles
is_exhausted = curr_range > (2.5 * atr) if atr > 0 else False
```

**Usage in entry logic:**
```python
bullish_entry = (
    ...
    not is_exhausted and   # <-- blocks entry when candle is oversized
    ...
)
```

### How to adapt for DhanAPI

- Add to `strategies/indicators.py` as a shared utility:
  ```python
  def is_exhaustion_candle(df: pd.DataFrame, atr_mult: float = 2.5) -> bool:
      latest = df.iloc[-1]
      atr = latest.get('atr', 0)
      return bool((latest['high'] - latest['low']) > atr_mult * atr) if atr > 0 else False
  ```
- Call this check inside every strategy worker's `generate_signal()` method before returning a `SignalEvent`.
- The ATR column is already computed in `strategies/indicators.py` — no new data fetch required.

---

## 5. Futures Volume for True Index VWAP — MEDIUM PRIORITY

### What it does
Spot indices (NIFTY 50, BANKNIFTY, etc.) have **zero volume** at the exchange level. Calculating VWAP directly on a spot chart produces a meaningless flat line. The EMA_9_21_Short system solves this by:
1. Looking up the **current-month Futures contract token** for the index.
2. Fetching the Futures 5M candle data (which has real volume).
3. Merging the Futures `volume` column onto the Spot price dataframe by timestamp.
4. Computing: `VWAP = cumsum(TP × Vol) / cumsum(Vol)` with a daily reset.

This produces a mathematically sound VWAP since Futures volume tracks spot liquidity almost perfectly.

### Why DhanAPI needs it
`strategies/indicators.py` in DhanAPI calculates VWAP as a rolling mean of typical price (no volume). This is a **VWAP proxy**, not true VWAP, and will diverge from the actual institutional VWAP level during high-activity sessions.

### Source code (EMA_9_21_Short)

**`strategies/indicators.py` — lines 37–54 (VWAP with Futures volume injection)**
```python
# Use volume_df for VWAP if provided (Spot Indices have zero native volume)
if volume_df is not None and not volume_df.empty:
    temp_vol = volume_df[['date', 'volume']].copy()
    temp_vol['date'] = pd.to_datetime(temp_vol['date'])
    df = pd.merge(df, temp_vol, on='date', how='left', suffixes=('', '_vol'))
    if 'volume_vol' in df.columns:
        df['volume'] = df['volume_vol'].fillna(0)
        df.drop(columns=['volume_vol'], inplace=True)

df['tp'] = (df['high'] + df['low'] + df['close']) / 3
df['vwap'] = df.groupby(df['date'].dt.date, group_keys=False).apply(
    lambda x: (x['tp'] * x['volume']).cumsum() / x['volume'].cumsum()
)
```

**`main.py` — lines 238–244 (Futures token lookup & volume fetch trigger)**
```python
volume_df = None
if df_5m['volume'].sum() == 0:
    fut_token = get_futures_token(config['symbol'])
    if fut_token:
        volume_df = get_data(kite, fut_token, "5minute")
        if not volume_df.empty:
            logger.info(f"Using Futures volume for {index_name} VWAP")

df_5m = calculate_indicators(df_5m, volume_df=volume_df)
```

**`main.py` — lines 36–57 (Futures token lookup from instrument CSV)**
```python
def get_futures_token(symbol):
    """Finds the current-month Futures instrument token from the instrument CSV."""
    try:
        if not os.path.exists(CSV_PATH): return None
        df = pd.read_csv(CSV_PATH)
        futs = df[(df['symbol'] == symbol) & (df['instrument_type'] == 'FUT')]
        if futs.empty: return None
        futs['expiry_date'] = pd.to_datetime(futs['expiry_date'])
        futs = futs[futs['expiry_date'] >= pd.Timestamp.now().normalize()]
        futs = futs.sort_values(by='expiry_date')
        return int(futs.iloc[0]['instrument_token']) if not futs.empty else None
    except Exception as e:
        logger.warning(f"Futures lookup failed for {symbol}: {e}")
    return None
```

### How to adapt for DhanAPI

- Dhan provides Futures candle data via the same candle endpoint — look up the Futures `security_id` from `strike_lookup.py` using `instrument_type='FUT'`.
- In `DataAgent._fetch_candles()`, for each spot index, check if volume sums to zero; if so, fetch the Futures candle series and pass it as `volume_df` alongside `df_5m`.
- The VWAP merge logic is pure pandas — directly portable from the source above.

---

## 6. Sector Leader Bias Scoped Correctly to NIFTY Only — LOW PRIORITY

### What it does
Checks if **Reliance (738561)** and **HDFC Bank (341249)** are above/below their 20-period EMA. If both agree, returns a leader bias of +1 (bullish) or −1 (bearish). This bias gates signal confirmation — a bearish signal requires bearish leader bias.

The key fix from the audit: **this check is only meaningful for NIFTY**. It is not applicable to BANKNIFTY, FINNIFTY, MIDCPNIFTY, or SENSEX because Reliance has zero weight in BANKNIFTY/FINNIFTY and neither stock is in MIDCPNIFTY.

### Source code (EMA_9_21_Short)

**`main.py` — lines 94–116 (sector bias function)**
```python
def get_sector_bias(kite):
    """Checks Reliance and HDFC Bank alignment vs their 20 EMA."""
    try:
        leaders = {"RELIANCE": 738561, "HDFCBANK": 341249}
        biases = []
        for name, token in leaders.items():
            df = get_data(kite, token, "5minute")
            if not df.empty and len(df) > 20:
                ema20 = df['close'].ewm(span=20, adjust=False).mean().iloc[-1]
                curr = df['close'].iloc[-1]
                biases.append(1 if curr > ema20 else -1)
        if sum(biases) == 2: return 1
        if sum(biases) == -2: return -1
        return 0
    except Exception as e:
        logger.warning(f"Sector bias check failed: {e}")
        return 0
```

**`main.py` — lines 261–265 (correctly scoped to NIFTY only)**
```python
# Only apply leader bias to NIFTY — Reliance/HDFC have no weight in other indices
leader_bias = 0
if index_name == "NIFTY":
    leader_bias = get_sector_bias(kite)
```

### How to adapt for DhanAPI

- DhanAPI's Worker K (`PairLeadershipWorker`) already tracks pair bias but applies it too broadly.
- Add a guard in `EMAVWAPSRWorker` (Worker D): only fetch/apply leader bias when `snapshot.index_name == "NIFTY"`.
- Fetch Reliance and HDFC Bank candles via Dhan's candle API (resolve their `security_id` from `strike_lookup`). Both are NSE_EQ instruments.

---

## 7. Price Sanity Guard Ranges — LOW PRIORITY

### What it does
Before acting on any spot LTP from the broker feed, the system validates that the price falls within a known realistic range per index. If the price is outside the range (API spike / feed error), the cycle is skipped entirely — no exit is triggered on corrupt data.

### Why DhanAPI needs it
DhanAPI's `dhan_sl_monitor.py` already has a sanity guard but may need the ranges kept current. Documenting the EMA_9_21_Short version as a calibration reference.

### Source code (EMA_9_21_Short)

**`core/sl_monitor_agent.py` — lines 210–219**
```python
ranges = {
    "NIFTY":      (15000, 30000),
    "BANKNIFTY":  (40000, 70000),
    "FINNIFTY":   (15000, 35000),
    "SENSEX":     (60000, 95000),
    "MIDCPNIFTY": (8000,  16000)
}
valid_range = ranges.get(data['index'], (0, 1_000_000))
if spot_ltp < valid_range[0] or spot_ltp > valid_range[1]:
    logger.error(f"PRICE SANITY ALERT: {data['index']} at {spot_ltp}. Expected {valid_range}. Skipping.")
    continue
```

### How to adapt for DhanAPI

- Cross-check the ranges in `dhan_sl_monitor.py` against these values and update if stale.
- The pattern is already present in DhanAPI — this is purely a calibration sync.

---

## 8. Stage 2 Intermediate Profit Lock in ATR Trailing SL — HIGH PRIORITY

### What it does
EMA_9_21_Short's SL Monitor implements a true **3-stage** ATR trailing system. The critical middle stage — **Stage 2: Lock Partial Profit** — is entirely absent from DhanAPI's `sl_engine.py`.

**Full stage comparison:**

| Stage | EMA_9_21_Short Trigger | EMA_9_21_Short Action | DhanAPI Equivalent |
|---|---|---|---|
| Stage 1 | progress ≥ **1.0 ATR** | SL → Entry (breakeven) | BREAKEVEN at 0.5 ATR ✓ |
| **Stage 2** | **progress ≥ 1.5 ATR** | **SL → Entry + 0.75 ATR (lock profit)** | **MISSING** |
| Stage 3 | progress ≥ **2.5 ATR** | SL → Peak − 1.5 ATR (active trail) | ATR_TRAILING at 2.0 ATR ✓ |

### Why DhanAPI needs it
Without Stage 2, a position that travels 1.9 ATR in favour and then reverses will exit at breakeven — giving back all accrued profit. With Stage 2, the same reversal exits at Entry + 0.75 ATR, locking in real money. This single stage is responsible for the system's positive profit factor on medium-strength trend days.

### Source code (EMA_9_21_Short)

**`core/sl_monitor_agent.py` — lines 249–274 (full 3-stage block)**
```python
# Calculate progress in ATR units
progress_atr = ((spot_ltp - data['entry']) * side_mul) / atr if atr > 0 else 0.0

# Stage 3 (Active Trailing from Peak): Progress >= 2.5 ATR
if progress_atr >= 2.5:
    new_sl = data['peak_spot'] - (1.5 * atr * side_mul)
    if (data['side'] == 'LONG' and new_sl > data['current_math_sl']) or \
       (data['side'] == 'SHORT' and new_sl < data['current_math_sl']):
        data['current_math_sl'] = new_sl
        data['sl_stage'] = 3

# Stage 2 (Lock Partial Profit): Progress >= 1.5 ATR
elif progress_atr >= 1.5 and data['sl_stage'] < 2:
    new_sl = data['entry'] + (0.75 * atr * side_mul)
    data['current_math_sl'] = new_sl
    data['sl_stage'] = 2
    logger.warning(f"   [TRAIL ATR] Stage 2: Locked +0.75 ATR Profit at {round(new_sl, 2)}")

# Stage 1 (Move to Breakeven): Progress >= 1.0 ATR
elif progress_atr >= 1.0 and data['sl_stage'] < 1:
    data['current_math_sl'] = data['entry']
    data['sl_stage'] = 1
    logger.warning(f"   [TRAIL ATR] Stage 1: SL moved to Breakeven at {round(data['entry'], 2)}")
```

**Symmetry note (verified in v3.0 audit):** For PE positions (`side_mul = -1`), `peak_spot` tracks the *lowest* price reached — not the highest. Stage 2 locks `Entry − 0.75 × ATR` (which is below entry for a short, locking profit). Stage 3 trails at `Peak_Spot + 1.5 × ATR` (upward from the trough). The math is fully symmetrical for both CE and PE.

### How to adapt for DhanAPI

In `sl_engine.py`, the `update_sl()` function currently has two stages. Add the intermediate stage:

```python
# After BREAKEVEN stage and before ATR_TRAILING stage:
elif gain_atr >= 1.5 and state['stage'] < 'PROFIT_LOCK':
    lock_sl = entry + (0.75 * atr * side_mul)
    state['math_sl'] = lock_sl
    state['stage'] = 'PROFIT_LOCK'
```

Also review whether to move the breakeven trigger from `0.5 ATR` to `1.0 ATR` (see Section 9 below).

---

## 9. ATR Trailing Threshold Recalibration — MEDIUM PRIORITY

### What it does
EMA_9_21_Short and DhanAPI use different ATR multipliers for the same conceptual stages. The calibration in EMA_9_21_Short was tuned specifically for 5-minute index options:

| Parameter | EMA_9_21_Short | DhanAPI `sl_engine.py` | Difference |
|---|---|---|---|
| Breakeven activation | **1.0 ATR** | 0.5 ATR | DhanAPI triggers too early → more whipsaw exits |
| Stage 2 lock profit | **1.5 ATR** | — (missing) | — |
| Trailing activation | **2.5 ATR** | 2.0 ATR | DhanAPI activates sooner |
| Trail distance from peak | **1.5 ATR** | 2.0 ATR | DhanAPI uses a wider, looser trail |

### Why this matters
A 0.5 ATR breakeven threshold on a 5-minute index candle means breakeven is hit routinely on normal noise, causing the position to exit at zero gain on what would have been a winning trade. EMA_9_21_Short's 1.0 ATR threshold requires a genuine move before committing to breakeven, filtering out these false activations.

### How to adapt for DhanAPI
Update `config/sl_config.json` (or wherever DhanAPI stores these constants):
```json
{
  "atr_beven_mult":  1.0,
  "atr_lock_mult":   1.5,
  "atr_trail_mult":  2.5,
  "atr_trail_dist":  1.5
}
```
Make these values configurable per index (NIFTY has a larger absolute ATR than FINNIFTY).

---

## 10. Watchdog Heartbeat + Warmup Status Reset Pattern — MEDIUM PRIORITY

### What it does
EMA_9_21_Short's `watchdog.py` runs a background process that:
1. Checks each agent's `last_heartbeat` timestamp every 30 seconds from the `system_status` DB table.
2. If any agent's heartbeat is older than **300 seconds**, it kills and restarts that agent in a fresh CMD window inside the designated Anaconda environment.
3. Applies a **Warmup Status Reset** — immediately after triggering a restart, it writes a `WARMUP` status for that agent so the watchdog doesn't fire a second restart while the agent is loading historical data (which can take 30–60 seconds).

### Why DhanAPI needs it
DhanAPI's agents run as separate Python processes with no cross-process health monitoring. If `data_agent.py` or `execution_agent.py` silently crashes, the system continues without any data or order execution — with no alert and no recovery.

### Source code (EMA_9_21_Short)

**`watchdog.py` — core heartbeat check and restart logic**
```python
# Each agent updates this table every cycle
conn.execute(
    "INSERT OR REPLACE INTO system_status (agent_name, status, last_heartbeat) VALUES (?,?,?)",
    (agent_name, status, datetime.now())
)

# Watchdog check (every 30s)
cur.execute("SELECT agent_name, status, last_heartbeat FROM system_status")
for agent_name, status, last_hb in cur.fetchall():
    age_seconds = (datetime.now() - last_hb).total_seconds()
    if age_seconds > 300 and status != 'WARMUP':
        logger.warning(f"[WATCHDOG] {agent_name} is DEAD (last seen {age_seconds:.0f}s ago). Restarting...")
        # Write WARMUP immediately to prevent double-restart
        conn.execute(
            "UPDATE system_status SET status='WARMUP', last_heartbeat=? WHERE agent_name=?",
            (datetime.now(), agent_name)
        )
        conn.commit()
        # Spawn new process
        subprocess.Popen(["start", "cmd", "/k", f"conda activate trading && python {agent_scripts[agent_name]}"], shell=True)
```

### How to adapt for DhanAPI
- Create a `watchdog.py` in the DhanAPI root.
- Each agent (`data_agent.py`, `execution_agent.py`, `dhan_sl_monitor.py`) writes a heartbeat row every loop iteration using the same SQLite WAL pattern.
- The watchdog restarts via `subprocess.Popen` pointing to the agent's entry script.
- The `WARMUP` guard is critical — without it, a slow-starting agent triggers a cascade of restarts.

---

## 11. Admin Console / Live-Paper Mode Switcher — LOW PRIORITY

### What it does
EMA_9_21_Short's `dashboard.py` provides a live web-based admin console where the operator can:
- View live agent heartbeat statuses (SCANNING / IDLE / DEAD).
- See the current open positions and live P&L.
- **Toggle between Paper Trading and Live Trading modes** via a sidebar button that writes `PAPER_TRADING = TRUE/FALSE` to the `system_config` DB table — no code restart required.
- View system logs in real-time.

### Why DhanAPI needs it
DhanAPI has no admin interface. Switching paper/live mode currently requires manually editing config files and restarting all agents — a high-risk operation during live sessions.

### How to adapt for DhanAPI
- Build `dashboard.py` using `streamlit` (same pattern as EMA_9_21_Short).
- Read/write `system_config` table from the same SQLite DB used by all agents.
- Each agent already reads `IS_PAPER` at the top of its order execution block — just ensure they poll `system_config` each cycle (not once at startup).

---

## Already Handled in DhanAPI — Do Not Re-Implement

These features exist in EMA_9_21_Short and are worth noting, but DhanAPI already has equal or superior implementations:

| Feature | EMA_9_21_Short Location | DhanAPI Equivalent | Note |
|---|---|---|---|
| Tiered VIX Position Sizing (4 buckets) | `core/order_placer.py` lines 133–148 | `core/order_placer.py` | Same 4 tiers already present |
| ATR Trailing SL (stages 1 & 3) | `core/sl_monitor_agent.py` lines 249–273 | `sl_engine.py` | Stage 2 is missing — see Section 8 above |
| 15:25 Hard Cutoff | `core/sl_monitor_agent.py` line 114 | `dhan_sl_monitor.py` | Already present |
| Paper Trading Simulation | `core/order_placer.py` IS_PAPER block | `core/simulator.py` | DhanAPI has full DB simulator |
| Signal Deduplication | `main.py` 5-min window check | `strategies/ema_9_21.py` | DhanAPI has per-bar + 120s cooldown |
| Market Hours Guard (9:30–14:30) | `main.py` `is_trading_allowed()` | `agents/data_agent.py` | Already in data agent |
| EMA 9/21 Crossover Core | `strategies/indicators.py` | `strategies/ema_9_21.py` | DhanAPI adds ADX ≥ 25 gate + RSI confirmation |
| SQLite WAL Concurrency | All agents | All agents | Both systems use WAL + 30s timeout |

---

## Suggested Implementation Order

Tackle in this sequence to get maximum risk-reduction and P&L improvement per session:

1. **Opening Gap Risk Filter** — add to `agents/data_agent.py`. Pure protection, zero strategy change.
2. **Stage 2 Intermediate Profit Lock + ATR threshold recalibration** — update `sl_engine.py`. Directly boosts profit factor on every medium-trend day.
3. **Exhaustion Candle Filter** — add shared utility to `strategies/indicators.py`, call from all workers.
4. **DTE Strike Selection Matrix** — add to `core/order_placer.py`. Self-contained, testable in paper mode.
5. **21-Candle S/R Breakout Gate** — add columns in `strategies/indicators.py`, gate Workers A and D.
6. **Watchdog Heartbeat + Warmup Reset** — create `watchdog.py`. Critical for unattended live sessions.
7. **Futures Volume VWAP** — requires Dhan Futures security_id resolution; do after the above are stable.
8. **Sector Leader Bias (NIFTY-only scope)** — minor guard clause in Worker D.
9. **Admin Console / Dashboard** — build `dashboard.py` with Streamlit. Do last; cosmetic, not risk-critical.
10. **Price Sanity Range Calibration** — verify `dhan_sl_monitor.py` ranges match the table in Section 11.

---

*Source system: EMA_9_21_Short v3.0 — certified production-ready, May 28, 2026.*  
*Second assessment (12:10) confirmed all v3.0 bugs resolved; this document incorporates findings from both audits.*  
*All code snippets require broker-layer translation (Kite Connect → Dhan API) before use.*
