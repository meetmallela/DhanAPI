# Next Phase Research & Implementation Plan
**Prepared:** 2026-05-25 (Night) | **Review:** 2026-05-26 Morning
**Author:** Claude Code Session

---

## Executive Summary

Three new capability areas:

| # | Topic | Complexity | Est. Build Time | Priority |
|---|-------|-----------|----------------|----------|
| 6A | Commodity Tracker (MCX via Kite) | High | 3–4 days | Medium |
| 6B | Price Action Trade Engine | Very High | 5–7 days | High |
| 6C | Multi-TF Candlestick Pattern Scanner | Medium | 2–3 days | High |

**Recommended build order:** 6C → 6B → 6A (ascending complexity, 6C feeds directly into 6B)

---

## TOPIC 1 — Commodity Products Tracker (MCX via Kite)

### 1.1 What Kite Gives You on Commodities

Zerodha Kite Connect exposes MCX instruments via the same REST/WebSocket API you use for equities. The user already has a live `kite_candles.db` at:
```
C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db
```
This DB currently stores option candles. The plan is to extend it (or create a sibling `mcx_candles.db`) for commodity OHLCV data.

### 1.2 MCX Products — Tradeable Universe

| Instrument | Symbol | Lot Size | Tick | Contract Value (approx.) | Category |
|-----------|--------|----------|------|--------------------------|----------|
| Gold | GOLD | 1 kg | ₹1 | ₹7.2L | Metal |
| Gold Mini | GOLDM | 100 g | ₹1 | ₹72K | Metal |
| Gold Guinea | GOLDGUINEA | 8 g | ₹1 | ₹5.8K | Metal |
| Silver | SILVER | 30 kg | ₹1 | ₹2.7L | Metal |
| Silver Mini | SILVERM | 5 kg | ₹1 | ₹45K | Metal |
| Crude Oil | CRUDEOIL | 100 bbl | ₹1 | ₹6.4L | Energy |
| Crude Oil Mini | CRUDEOILM | 10 bbl | ₹1 | ₹64K | Energy |
| Natural Gas | NATURALGAS | 1250 mmBtu | ₹0.10 | ₹4.5L | Energy |
| Copper | COPPER | 2500 kg | ₹0.05 | ₹2.4L | Base Metal |
| Copper Mini | COPPERM | 250 kg | ₹0.05 | ₹24K | Base Metal |
| Zinc | ZINC | 5000 kg | ₹0.05 | ₹1.4L | Base Metal |
| Aluminum | ALUMINIUM | 5000 kg | ₹0.05 | ₹1.2L | Base Metal |
| Nickel | NICKEL | 250 kg | ₹0.10 | ₹1.5L | Base Metal |
| Lead | LEAD | 5000 kg | ₹0.05 | ₹1.1L | Base Metal |
| Cotton | COTTON | 25 bales | ₹1 | ₹6L | Agri |
| Cardamom | CARDAMOM | 1 kg | ₹10 | ₹28K | Agri |
| CPO | CPO | 10 MT | ₹1 | ₹9L | Agri |
| Castor Seed | CASTORSEED | 10 MT | ₹1 | ₹5.5L | Agri |

**Practical starter set for Phase 6A:** Gold Mini, Silver Mini, Crude Oil Mini, Natural Gas, Copper Mini  
(Mini contracts — accessible margin, liquid, price-action driven)

### 1.3 Architecture: Commodity Agent Brain

```
┌─────────────────────────────────────────────────────────────┐
│                  CommodityBrain (agent)                     │
│                                                             │
│  ┌───────────────────┐   ┌───────────────────────────────┐ │
│  │  Data Ingestion   │   │     Macro Context Engine      │ │
│  │  Layer            │   │                               │ │
│  │  - MCX OHLCV (Kite│   │  - DXY (US Dollar Index)      │ │
│  │    WebSocket 1M)  │   │  - Brent vs WTI spread        │ │
│  │  - Daily OHLC     │   │  - COMEX Gold vs MCX Gold     │ │
│  │  - Volume data    │   │  - LME Copper vs MCX Copper   │ │
│  │  - Open Interest  │   │  - NYMEX Nat Gas reference    │ │
│  │    (MCX FOI)      │   │  - Rupee/Dollar rate (USDINR) │ │
│  └───────────────────┘   └───────────────────────────────┘ │
│                                                             │
│  ┌───────────────────┐   ┌───────────────────────────────┐ │
│  │  Session Overlap  │   │    Intraday Momentum          │ │
│  │  Model            │   │    Screener                   │ │
│  │                   │   │                               │ │
│  │  MCX Hours:       │   │  - ATR14 momentum (current    │ │
│  │  09:00–23:30 IST  │   │    range vs avg range)        │ │
│  │                   │   │  - VWAP deviation model       │ │
│  │  Key overlaps:    │   │  - Volume spike detection     │ │
│  │  US open 19:30 IST│   │  - SuperTrend (7,3) signal    │ │
│  │  = high vol for   │   │  - EMA9/21 crossover          │ │
│  │  Energy+Metals    │   │  - RSI divergence             │ │
│  └───────────────────┘   └───────────────────────────────┘ │
│                                                             │
│  ┌───────────────────┐   ┌───────────────────────────────┐ │
│  │  Inventory Events │   │    Position Sizer +           │ │
│  │  Calendar         │   │    Risk Manager               │ │
│  │                   │   │                               │ │
│  │  - EIA Crude Oil  │   │  - ATR-based stop loss        │ │
│  │    (Wed 21:00 IST)│   │  - Max 1 lot per commodity    │ │
│  │  - EIA Nat Gas    │   │  - Overnight margin safety:   │ │
│  │    (Thu 20:00 IST)│   │    auto-exit before 23:00 IST │ │
│  │  - FOMC dates     │   │  - Correlation cap: Gold +    │ │
│  │  - RBI policy     │   │    Silver = 1 combined lot    │ │
│  └───────────────────┘   └───────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 1.4 Models to Track Movement

#### Model A — Session Momentum Model
Track each commodity's intraday momentum relative to the session's opening price. Use:
- First 15-min range as baseline (ORB)
- ATR14 for volatility normalisation
- 3 zones: Morning (09:00–12:00), Afternoon (12:00–17:00), US Overlap (19:00–23:30)

Each zone has historically different behaviour:
- **Morning:** Indian domestic sentiment + carry-over from overnight
- **Afternoon:** Position squaring + MCX rollover activity
- **US Overlap (19:00–23:30 IST):** Highest volatility for Energy and Metals — mirrors NYMEX/CME opening

#### Model B — Global Reference Divergence Model
Compare MCX prices to global benchmarks in real-time:
```
gold_premium    = MCX_gold_price / (COMEX_gold_usd × USDINR × 31.1)  # 1 troy oz = 31.1g
crude_spread    = MCX_crude − (WTI_usd × USDINR / 6.28918)           # 1 barrel = 6.289 liters... no → 1 bbl = 158.99L
copper_spread   = MCX_copper − (LME_copper_usd × USDINR / 1000)      # ₹ per kg
```
When divergence exceeds 2σ from 20-session mean → mean-reversion signal (or tariff/sentiment news alert).

#### Model C — Inventory-Driven Volatility Model
Pre-mark the EIA release times (hard-coded weekly calendar). Apply a "no-trade zone" 10 mins before / after release. After the number:
- If draw > consensus → bullish impulse signal (Crude up)
- If build > consensus → bearish impulse signal (Crude down)
- Magnitude filter: 3× ATR14 expected on release day

#### Model D — Seasonality Model (for Agriculture)
Gold: historically bullish Oct–Nov (Diwali, wedding season)
Crude: Q1 → demand recovery, Q3 → driving season premium
Agriculture: monsoon-linked crop reports (June–Sept)
Store seasonal bias as a ±0.5 weight modifier on signals.

### 1.5 Data Fetch Architecture

```python
# New file: agents/commodity_brain.py

MCX_INSTRUMENTS = {
    "GOLDM":       {"token": None, "exchange": "MCX", "lot": 100, "unit": "g"},
    "SILVERM":     {"token": None, "exchange": "MCX", "lot": 5000, "unit": "g"},
    "CRUDEOILM":   {"token": None, "exchange": "MCX", "lot": 10,  "unit": "bbl"},
    "NATURALGAS":  {"token": None, "exchange": "MCX", "lot": 1250, "unit": "mmBtu"},
    "COPPERM":     {"token": None, "exchange": "MCX", "lot": 250,  "unit": "kg"},
}
# Tokens fetched once from kite.instruments("MCX") → save to instruments.csv
```

**Key challenge:** MCX instruments change every month (near-month contract expiry). Need auto-rollover logic: switch to next contract 5 days before expiry.

### 1.6 New Files Required

| File | Purpose |
|------|---------|
| `agents/commodity_brain.py` | Main CommodityBrain daemon thread |
| `core/mcx_feed.py` | Kite WebSocket subscription + SQLite writer for MCX |
| `core/mcx_instruments.py` | Instrument token lookup + auto-rollover |
| `core/commodity_baselines.py` | ATR14, vol20, session ranges per commodity |
| `/api/commodities` | Dashboard endpoint |
| `templates/` → Commodities tab | Dashboard UI tab |

### 1.7 Critical Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Overnight gap risk (MCX runs till 23:30) | Auto-exit at 22:45 for all open commodity positions |
| High contract value (Gold = ₹7.2L) | Default to MINI contracts only |
| Contract expiry gaps | Auto-rollover 5 days before expiry |
| EIA release slippage | No-trade zone ±10 min around release |
| USDINR rate dependency | Fetch USDINR rate from Kite `NSE:USDINR` or yfinance |

---

## TOPIC 2 — Price Action Trade Engine

### 2.1 Universe Decision

**Recommendation: Use the FnO 209-stock universe (already built)**

| Option | Pros | Cons |
|--------|------|------|
| All NSE listed (~2000+) | Maximum coverage | 80% illiquid — PA charts meaningless; data volume overwhelming |
| Nifty 50 | Very liquid, clean charts | Only 50 stocks — misses mid-cap opportunities |
| Nifty 500 | Wide coverage | Many illiquid names; no options hedge available |
| **FnO 209 stocks** ✓ | Liquid, clean PA, derivatives available, already mapped to security IDs | Misses some SMID names |
| FnO 209 + 5 indices | Best of both worlds for intraday | Slightly larger scan |

**Final answer:** FnO 209 stocks + NIFTY, BANKNIFTY, MIDCPNIFTY, FINNIFTY, SENSEX = **214 instruments**

The FnO filter guarantees:
- Exchange-mandated liquidity (≥15% market-wide position limit)
- Tight bid-ask spreads (market makers active)
- Options hedge available if position sours
- Already have `security_id` mappings for Dhan quote_data()

### 2.2 Price Action Concepts to Implement

#### Tier 1 — Structural Concepts (backbone, run on 1H/4H)
These define **where** PA setups are valid:

```
1. Swing High / Swing Low Detection
   - A swing high: highest high with N lower highs on both sides (N=3 for 4H, N=5 for 1H)
   - A swing low: inverse
   - Implementation: adapt existing find_peaks()/find_troughs() from strategies/indicators.py

2. Market Structure: HH/HL (uptrend), LH/LL (downtrend), ranging
   - Tag last 6 swing points, classify structure
   - Example: HH > HL > HH = confirmed uptrend

3. Break of Structure (BOS)
   - Price closes ABOVE last swing high in uptrend = continuation BOS
   - Price closes BELOW last swing high in downtrend = continuation BOS

4. Change of Character (ChoCH)
   - Price closes BELOW last swing low in uptrend = first reversal warning
   - Separate from BOS — it's the early warning, not confirmation

5. Supply & Demand Zones
   - A zone is the consolidation BEFORE a large explosive move
   - Supply zone: consolidation before a strong drop (future sell zone)
   - Demand zone: consolidation before a strong rally (future buy zone)
   - Stored with: zone_high, zone_low, time_created, strength_score
```

#### Tier 2 — Execution Concepts (run on 5M/15M for entries)
These define **when** to enter after structural context is set:

```
6. Order Block (OB)
   - The last bearish candle before a large bullish impulse = Bullish OB
   - The last bullish candle before a large bearish impulse = Bearish OB
   - When price returns to OB = potential entry
   - OB body range stored (open to close of that one candle)

7. Fair Value Gap (FVG) / Imbalance
   - Candle N-1 high < Candle N+1 low = Bullish FVG (gap in price)
   - Candle N-1 low > Candle N+1 high = Bearish FVG
   - Price often returns to fill FVG before resuming trend

8. Liquidity Sweep / Stop Hunt
   - Multiple swing lows at same level = buy-side liquidity pool
   - When price wicks below all of them then closes above = liquidity sweep
   - Setup: enter long after sweep, stop below wick

9. Inside Bar / NR4 / NR7
   - Inside bar: High < prev High AND Low > prev Low (compression)
   - NR4: narrowest range of last 4 bars (extreme compression)
   - NR7: narrowest range of last 7 bars
   - Breakout from these = high-probability expansion

10. Pin Bar / Rejection Candle
    - Wick ≥ 2× body, wick pointing toward rejected level
    - Confirmation: close within top/bottom 33% of candle range
```

#### Tier 3 — Filters (quality gates)
```
11. EMA Trend Filter: price above EMA21 = bullish bias; below = bearish bias
12. Volume Confirmation: signal candle volume > 1.3× avg20
13. HTF Alignment: 4H structure must agree with 1H entry direction
14. Time Filter: avoid first 15 min (9:15–9:30) and last 15 min (3:15–3:30)
15. Gap Filter: pre-market gap >3% often invalidates intraday PA zones
```

### 2.3 Multi-Timeframe Hierarchy

```
4H  →  Defines primary trend + major S/R + large supply/demand zones
1H  →  Defines intermediate structure + OB/FVG zones for entries
5M  →  Entry candle pattern confirmation (BOS on 5M after 1H OB touch)
1M  →  Precise entry timing (first 1M close back above 5M OB level)
```

**Trade workflow example:**
```
1. 4H: RELIANCE in uptrend (HH + HL pattern), price pulls back to 4H demand zone
2. 1H: BOS on 1H confirms buyers active at that zone + 1H OB visible
3. 5M: Pin bar or Inside Bar breakout on 5M at the 1H OB level
4. 1M: Entry on first 1M candle close above the 5M Inside Bar high
5. Stop: below 1M swing low (or below the 1H OB zone)
6. Target: last 4H swing high (or 1:2 R:R minimum)
```

### 2.4 Data Source (Already Available)

```
kite_candles.db  →  candles_1min  (live + historical 1M data)
                    resample to 5M, 1H, 4H via pandas groupby/resample
```

No new data source needed. The resample functions already work in `paper_vs_kite_sim.py`.

### 2.5 New Files Required

| File | Purpose |
|------|---------|
| `core/pa_engine.py` | Core PA detection library (swing points, BOS, ChoCH, OB, FVG) |
| `core/pa_zones.py` | Supply/demand zone state machine + SQLite storage |
| `agents/pa_scanner.py` | Scans 214 instruments every 5M, detects setups, writes alerts |
| `data/pa_zones.db` | Supply/demand zones, OBs, FVGs, active setups |
| `/api/pa_setups` | Dashboard endpoint |
| `templates/` → PA tab | Dashboard UI |

### 2.6 PA Alert Schema (SQLite)

```sql
CREATE TABLE pa_setups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT,
    timeframe   TEXT,      -- '1M', '5M', '1H', '4H'
    setup_type  TEXT,      -- 'OB_TOUCH', 'FVG_FILL', 'BOS', 'CHOCH', 'INSIDE_BAR_BREAK', etc.
    direction   TEXT,      -- 'BULLISH', 'BEARISH'
    entry_price REAL,
    stop_price  REAL,
    target_1    REAL,
    target_2    REAL,
    risk_reward REAL,
    structure   TEXT,      -- '4H: UPTREND, 1H: PULLBACK, 5M: ENTRY'
    htf_aligned INTEGER,   -- 1 if higher TF agrees
    vol_confirm INTEGER,   -- 1 if volume confirmed
    detected_at TEXT,
    status      TEXT DEFAULT 'ACTIVE',  -- 'ACTIVE', 'TRIGGERED', 'INVALIDATED', 'TARGET_HIT'
    closed_at   TEXT
);
```

---

## TOPIC 3 — Multi-Timeframe Candlestick Pattern Scanner

### 3.1 Complete Pattern Library

#### Single-Candle Patterns (context / entry filter)

| # | Pattern | Bias | Key Rule |
|---|---------|------|----------|
| S1 | Doji | Indecision | `abs(close-open) ≤ 0.1 × (high-low)` |
| S2 | Dragonfly Doji | Bullish | Doji + lower wick ≥ 2× range, no upper wick |
| S3 | Gravestone Doji | Bearish | Doji + upper wick ≥ 2× range, no lower wick |
| S4 | Long-Legged Doji | Indecision | Both wicks ≥ 1.5× body |
| S5 | Hammer | Bullish reversal | At downtrend bottom; lower wick ≥ 2× body; little/no upper wick |
| S6 | Inverted Hammer | Bullish reversal | At bottom; upper wick ≥ 2× body; little/no lower wick |
| S7 | Hanging Man | Bearish reversal | Hammer shape at uptrend top |
| S8 | Shooting Star | Bearish reversal | Inverted Hammer shape at top |
| S9 | Bullish Marubozu | Strong bullish | open=low, close=high (no wicks) |
| S10 | Bearish Marubozu | Strong bearish | open=high, close=low |
| S11 | Spinning Top | Indecision | Small body, wicks on both sides roughly equal |

#### 2-Candle Patterns — Full List (17 patterns)

| # | Pattern | Bias | Detection Rule |
|---|---------|------|----------------|
| C2_01 | **Bullish Engulfing** | Reversal ↑ | C1 bearish, C2 bullish; C2_open ≤ C1_close AND C2_close ≥ C1_open |
| C2_02 | **Bearish Engulfing** | Reversal ↓ | C1 bullish, C2 bearish; C2_open ≥ C1_close AND C2_close ≤ C1_open |
| C2_03 | **Bullish Harami** | Reversal ↑ | C1 large bearish; C2 small bullish inside C1's body |
| C2_04 | **Bearish Harami** | Reversal ↓ | C1 large bullish; C2 small bearish inside C1's body |
| C2_05 | **Bullish Harami Cross** | Strong reversal ↑ | C1 large bearish; C2 is Doji inside C1 body |
| C2_06 | **Bearish Harami Cross** | Strong reversal ↓ | C1 large bullish; C2 is Doji inside C1 body |
| C2_07 | **Tweezer Bottom** | Reversal ↑ | C1 bearish, C2 bullish; same low (within 0.2%) |
| C2_08 | **Tweezer Top** | Reversal ↓ | C1 bullish, C2 bearish; same high (within 0.2%) |
| C2_09 | **Piercing Line** | Reversal ↑ | C1 large bearish; C2 opens below C1_low, closes above 50% of C1 body |
| C2_10 | **Dark Cloud Cover** | Reversal ↓ | C1 large bullish; C2 opens above C1_high, closes below 50% of C1 body |
| C2_11 | **Bullish Kicker** | Strong reversal ↑ | C1 bearish; C2 gaps up (C2_open ≥ C1_open), strongly bullish |
| C2_12 | **Bearish Kicker** | Strong reversal ↓ | C1 bullish; C2 gaps down (C2_open ≤ C1_open), strongly bearish |
| C2_13 | **On-Neck** | Bearish continuation | C1 large bearish; C2 small bullish; C2_close ≈ C1_low (within 0.2%) |
| C2_14 | **In-Neck** | Bearish continuation | Like On-Neck but C2_close slightly above C1_low |
| C2_15 | **Thrusting** | Bearish continuation | C2_close < 50% of C1 body but above C1_low |
| C2_16 | **Matching Low** | Support | Two consecutive bearish candles with same close (within 0.2%) |
| C2_17 | **Matching High** | Resistance | Two consecutive bullish candles with same close (within 0.2%) |

#### 3-Candle Patterns — Full List (25 patterns)

| # | Pattern | Bias | Key Detection Rule |
|---|---------|------|---------------------|
| C3_01 | **Morning Star** | Strong reversal ↑ | C1 large bearish; C2 small body (gap optional); C3 large bullish closing >50% into C1 body |
| C3_02 | **Evening Star** | Strong reversal ↓ | C1 large bullish; C2 small body; C3 large bearish closing >50% into C1 body |
| C3_03 | **Morning Doji Star** | Very strong reversal ↑ | Morning Star where C2 is a Doji |
| C3_04 | **Evening Doji Star** | Very strong reversal ↓ | Evening Star where C2 is a Doji |
| C3_05 | **Bullish Abandoned Baby** | Strongest reversal ↑ | Morning Doji Star with true gaps both sides of C2 |
| C3_06 | **Bearish Abandoned Baby** | Strongest reversal ↓ | Evening Doji Star with true gaps both sides of C2 |
| C3_07 | **Three White Soldiers** | Strong bullish trend | 3 consecutive large bullish candles, each open inside prev body, each close higher |
| C3_08 | **Three Black Crows** | Strong bearish trend | 3 consecutive large bearish candles, each open inside prev body, each close lower |
| C3_09 | **Three Inside Up** | Reversal ↑ | C1 large bearish; C2 small bullish inside C1 (Harami); C3 closes above C1_open |
| C3_10 | **Three Inside Down** | Reversal ↓ | C1 large bullish; C2 small bearish inside (Harami); C3 closes below C1_open |
| C3_11 | **Three Outside Up** | Reversal ↑ | C1 bearish; C2 bullish engulfing; C3 closes higher |
| C3_12 | **Three Outside Down** | Reversal ↓ | C1 bullish; C2 bearish engulfing; C3 closes lower |
| C3_13 | **Bullish Three Line Strike** | Continuation ↑ | 3 Black Crows + C4 bullish engulfing all 3 (counterintuitive bullish) |
| C3_14 | **Bearish Three Line Strike** | Continuation ↓ | 3 White Soldiers + C4 bearish engulfing all 3 |
| C3_15 | **Upside Tasuki Gap** | Bullish continuation | C1 large bullish; C2 gaps up bullish; C3 bearish opens in gap but doesn't close it |
| C3_16 | **Downside Tasuki Gap** | Bearish continuation | C1 large bearish; C2 gaps down bearish; C3 bullish opens in gap but doesn't close it |
| C3_17 | **Advance Block** | Bearish warning | 3 White Soldiers but bodies progressively smaller + longer upper wicks |
| C3_18 | **Deliberation Pattern** | Bullish stall | 2 long White Soldiers + 3rd very small bullish (hesitation) |
| C3_19 | **Identical Three Crows** | Very bearish | 3 Black Crows where each opens at prev close |
| C3_20 | **Two Crows** | Reversal ↓ | C1 large bullish; C2 small bearish gap-up; C3 large bearish engulfs C2, closes within C1 |
| C3_21 | **Tri-Star Bullish** | Strong reversal ↑ | 3 Doji; middle one gapped down from C1; C3 gapped up |
| C3_22 | **Tri-Star Bearish** | Strong reversal ↓ | 3 Doji; middle one gapped up; C3 gapped down |
| C3_23 | **Ladder Bottom** | Reversal ↑ | 3 consecutive bearish candles with lower opens; C4 strong bullish close |
| C3_24 | **Unique Three River Bottom** | Reversal ↑ | C1 large bearish; C2 small bearish Hammer making new low; C3 small bullish body |
| C3_25 | **Concealing Baby Swallow** | Reversal ↑ | 2 Bearish Marubozu; C3 bearish with upper wick above C2; C4 engulfs C3 (rare) |

**Total: 11 single + 17 two-candle + 25 three-candle = 53 patterns**

### 3.2 Implementation Approach: Two Options

#### Option A — TA-Lib (Recommended if installable)
```python
import talib
# 61 CDL* functions, zero custom logic needed
result = talib.CDLENGULFING(open, high, low, close)   # returns +100/-100/0
result = talib.CDLMORNINGSTAR(o, h, l, c, penetration=0.3)
# Wrap all in a loop → one-shot multi-pattern scan
```
**Pros:** Battle-tested, covers all 53 patterns, fast (C-level).  
**Cons:** Binary dependency (can be a pain to install on Windows). Try: `pip install TA-Lib-precompiled` or `conda install -c conda-forge ta-lib`

#### Option B — Pure Pandas (Fallback, already portable)
Build on top of the existing `backtest/indicators.py` infrastructure. All rules are arithmetic comparisons on the last 3 rows of the OHLCV DataFrame:
```python
def bullish_engulfing(df: pd.DataFrame) -> pd.Series:
    c1_bear = df["close"].shift(1) < df["open"].shift(1)
    c2_bull = df["close"] > df["open"]
    c2_engulfs = (df["open"] <= df["close"].shift(1)) & (df["close"] >= df["open"].shift(1))
    return (c1_bear & c2_bull & c2_engulfs).astype(int) * 100
```
**Pros:** No binary deps, fits naturally into existing codebase.  
**Cons:** ~300 lines of pattern logic to write/verify.

**Decision for tomorrow:** Try TA-Lib install first (`conda install -c conda-forge ta-lib`). If it works, use Option A. Otherwise build Option B.

### 3.3 Multi-Timeframe Architecture

```
Data Source: kite_candles.db → candles_1min
                              ↓
                    CandleResampler
                    ├── 5M  = pd.Grouper(freq='5min')
                    ├── 1H  = pd.Grouper(freq='1H')
                    └── 4H  = pd.Grouper(freq='4H')
                              ↓
               PatternScanner (per timeframe)
               ├── Run all 53 CDL functions
               ├── Volume confirmation gate
               ├── Trend context gate (EMA21 alignment)
               └── Write to pattern_alerts table
                              ↓
                   Dashboard: "Patterns" tab
                   ├── Real-time (1M patterns every 1 min)
                   ├── 5M patterns (every 5 min)
                   └── 1H/4H patterns (end of each bar)
```

### 3.4 Noise Management (Critical for 1M)

1M charts produce enormous numbers of "pattern" signals — mostly noise. Apply these gates:

| Gate | Description | Removes |
|------|-------------|---------|
| **Volume confirmation** | Pattern candle(s) volume > 1.5× avg20 | ~40% of false signals |
| **HTF trend alignment** | 1M/5M pattern must align with 1H EMA trend | ~30% of false signals |
| **Body size filter** | Body must be ≥ 0.5% of price (eliminates micro-body patterns) | ~15% of false signals |
| **ATR filter** | Pattern candle range ≥ 0.7× ATR14 (real conviction) | ~10% of false signals |
| **Cooldown** | Same pattern on same symbol+TF cooldown = 3 candles | Eliminates repeats |

**Combined effect:** ~70% noise reduction. Expected signal rate: 15–25 meaningful patterns per scan across 209 stocks.

### 3.5 Scanner Database Schema

```sql
CREATE TABLE pattern_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,          -- '1M', '5M', '1H', '4H'
    pattern     TEXT NOT NULL,          -- 'BULLISH_ENGULFING', 'MORNING_STAR', etc.
    direction   TEXT NOT NULL,          -- 'BULLISH', 'BEARISH', 'NEUTRAL'
    confidence  INTEGER,                -- 1-3 (1=raw pattern, 2=+volume, 3=+HTF aligned)
    candle_open REAL, candle_high REAL, candle_low REAL, candle_close REAL,
    volume      REAL,
    avg_volume  REAL,
    ema21       REAL,
    htf_trend   TEXT,                   -- '1H: BULLISH', '4H: BEARISH', etc.
    suggested_entry  REAL,
    suggested_stop   REAL,
    suggested_target REAL,
    detected_at TEXT,
    bar_time    TEXT                    -- the candle's timestamp (not detection time)
);

CREATE INDEX idx_pa_symbol ON pattern_alerts(symbol, detected_at);
CREATE INDEX idx_pa_timeframe ON pattern_alerts(timeframe, detected_at);
```

### 3.6 New Files Required

| File | Purpose |
|------|---------|
| `core/candle_patterns.py` | All 53 pattern detection functions (Option A: TA-Lib wrapper; Option B: pure pandas) |
| `core/candle_resampler.py` | Resample 1M → 5M/1H/4H from kite_candles.db |
| `agents/pattern_scanner.py` | Scanning daemon: every 1M for 1M TF, every 5M for 5M TF, etc. |
| `/api/patterns` | Dashboard endpoint: recent alerts by TF + symbol |
| `templates/` → Patterns tab | Dashboard UI |

### 3.7 Dashboard UI Plan

**Patterns Tab:**
- Timeframe selector: `1M | 5M | 1H | 4H | All`
- Direction filter: `All | Bullish | Bearish`
- Confidence filter: `All | ★★★ only`
- Table columns: `Symbol | Pattern | Dir | Confidence | Entry | Stop | Target | R:R | Time | TF`
- Color-coding: Green for bullish patterns, Red for bearish
- Auto-refresh: every 60 seconds

---

## DEPENDENCY MATRIX

```
6C (Pattern Scanner) REQUIRES:
  ✓ kite_candles.db (already exists)
  ✓ FnoUniverse 209 stocks (Phase 5A — done)
  ✓ backtest/indicators.py (done — ATR, EMA)
  ? TA-Lib binary (test install tomorrow)

6B (Price Action Engine) REQUIRES:
  ✓ kite_candles.db
  ✓ FnoUniverse 209 stocks
  ✓ 6C Pattern Scanner (candlestick patterns = PA entry confirmation)
  NEW: core/pa_engine.py (swing points, BOS, OB, FVG)

6A (Commodity Brain) REQUIRES:
  ✓ Kite Connect API (already integrated in MasterConfiguration)
  NEW: MCX instrument tokens
  NEW: core/mcx_feed.py
  ? Global reference prices (DXY, COMEX Gold, NYMEX Crude) — need free data source
```

---

## OPEN QUESTIONS FOR TOMORROW MORNING

### For 6C (Candlestick Scanner):
1. **TA-Lib installation:** Run `conda install -c conda-forge ta-lib` — does it succeed on your Anaconda3 setup? If yes, 6C build time drops by ~1 day.
2. **Scan frequency for 1M patterns:** Scan every candle (real-time) or end-of-day batch? Real-time needs kite WebSocket; batch needs just kite_candles.db. Recommend: **batch scan every 5 minutes against last 30 1M candles** — avoids WebSocket complexity for v1.
3. **Pattern alert retention:** Keep 24h of pattern alerts or just last scan? Recommend 48h rolling window.

### For 6B (Price Action):
4. **Supply/Demand zone lookback:** How many bars back to scan for zones? Recommend 500 bars on 1H (≈3 months of trading). On 4H: 200 bars (≈2 years).
5. **Order Block definition:** Standard (last candle before impulse) or ICT Institutional (gap-fill OB only)? Recommend standard for v1.
6. **FVG minimum size:** Only flag FVGs larger than 0.5% of price? Below that it's micro-gap noise.

### For 6A (Commodity):
7. **Global reference prices:** Will you use a paid data source (Quandl/EIA API) or just MCX-only signals without global correlation? Recommend MCX-only for v1 — avoids API key management.
8. **Overnight trading hours:** Do you want the commodity daemon to run until 23:30 IST? This means the engine needs to stay up. Or just Indian morning session (09:00–17:00)?
9. **Paper trading vs live:** Commodity signals — paper only (like current Dhan setup) or real execution via Kite?
10. **Kite authentication:** Is your Kite API access_token auto-refreshed (daily manual step) or have you set up a token refresh automation? This affects how commodity data fetch is scheduled.

---

## BUILD PLAN SUMMARY

### ✅ Phase 6C Data Pipeline — Equity Candle Collector (DONE 2026-05-25)
```
BUILT:  core/equity_candle_store.py
          - equity_candles.db / equity_candles_1min table
          - fetch_today(symbol, security_id, dhan_client) → Dhan intraday_minute_data()
          - load_candles(symbol, days) → DataFrame  (for pattern scanner)
          - resample(df1m, minutes)    → 5M / 1H / 4H bars

BUILT:  agents/equity_candle_collector.py
          - Daemon thread: cycles through 209 FnO stocks at 0.4s/symbol
          - Startup sweep: fetches all today's candles on first market-hours run
          - Full cycle ≈ 84 s → each stock refreshed every ~84 s
          - Wired into DhanOmniEngine_v2.py as thread 9l

Universe confirmed: 5 indices (kite_candles.db 1M real-time)
                  + 209 FnO stocks (equity_candles.db, building from today)
```

### Phase 6C — Candlestick Pattern Scanner (Start first, 2–3 days)
```
Day 1:  core/candle_resampler.py  +  core/candle_patterns.py (53 patterns)
Day 2:  agents/pattern_scanner.py  +  DB schema  +  /api/patterns
Day 3:  Dashboard Patterns tab  +  end-to-end test on 10 stocks
```

### Phase 6B — Price Action Engine (Start after 6C, 5–7 days)
```
Day 1-2:  core/pa_engine.py  (swing points, BOS, ChoCH, OB, FVG)
Day 3-4:  core/pa_zones.py  +  zone persistence + SQLite
Day 5:    agents/pa_scanner.py  (214-instrument scan loop)
Day 6:    /api/pa_setups  +  dashboard PA tab
Day 7:    Integration test + tuning
```

### Phase 6A — Commodity Brain (Start after 6B, 3–4 days)
```
Day 1:  core/mcx_instruments.py  +  token fetch  +  auto-rollover
Day 2:  core/mcx_feed.py  +  commodity_baselines.py
Day 3:  agents/commodity_brain.py  (models A + B; defer C/D to v2)
Day 4:  /api/commodities  +  dashboard Commodities tab
```

---

## NOTES & CONTEXT

- All new agents should follow the existing daemon pattern:
  `threading.Thread(daemon=True)` + `_stop_event = threading.Event()` + `stop()` method
  See: `agents/anomaly_scanner.py` as the reference implementation.

- All new DB tables go into the same `trading.db` (via `DB_PATH` from `MasterResource`).
  Exception: commodity candles may warrant a separate `mcx_candles.db` to keep it clean.

- The existing `backtest/indicators.py` already has: `ema, sma, atr, rsi, macd, supertrend, vwap, bollinger`.
  These are directly usable in 6B and 6C — no re-implementation needed.

- `find_peaks()` and `find_troughs()` exist in `strategies/indicators.py` (used by harmonic_pattern.py).
  These are the swing point detectors needed for 6B PA Engine — import or copy.

- The `kite_candles.db` path is:
  `C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db`
  This is outside the DhanAPI project root — need a config constant for the path.

---

*End of Research Document — Ready for morning review*
