# Phase 16 — Multi-Strike Scalp Strategy (Plan)

**Date opened:** 2026-05-01
**Build target:** 2026-05-01 → 2026-05-02
**First backtest:** 2026-05-02
**First live deploy:** ≥ 2026-05-12 (after one full week of v2 baseline data)
**Session:** TRD-20260416 (Phase 16 in build log)

---

## 1. Hypothesis (one sentence)

> On any 1-minute bar, scan all 6 NIFTY weekly contracts (ATM, ATM±1 × CE/PE) and trade only the contract whose **volume spike + ATR-band breakout + VWAP-band confirmation + candlestick pattern** align — targeting the contract's own VWAP line for exit.

This combines the user's multi-strike intuition with the three specific patterns from `Index_trading_strategies.md` (VWAP reversion with σ bands, candlestick + volume confirmation, index-specific stops).

---

## 2. Why This Is Different From Existing Strategies

| Aspect | Existing strategies (A–K) | Phase 16 — MultiStrikeScalp |
|---|---|---|
| Strike scope | ATM only, picked at execution time | 6 contracts pre-monitored, best one wins |
| Trigger logic | Single-indicator (EMA cross OR VWAP reclaim OR ATR breach) | **4-gate confluence** required simultaneously |
| Exit | Fixed % SL / target | **VWAP line** as explicit target (Index doc §1) |
| Stops | Same across indices | **Index-specific** (Nifty tight, BankNifty wide) |
| Trades on | Spot crossover | **Option premium** behavior — captures IV/gamma |

---

## 3. Signal Logic — Entry Gates (ALL four required)

For each of the 6 candidate contracts on every 1-min bar:

| Gate | Pass condition |
|---|---|
| **1. Volume spike** | `vol[-1] >= mean(vol[-21:-1]) + 2 × std(vol[-21:-1])` (Z-score ≥ 2) |
| **2. ATR-band breakout** | `close > upper ATR(14) × 2σ` (BULL) OR `close < lower band` (BEAR) |
| **3. VWAP-band confirmation** | `close > VWAP + 1σ` (BULL) OR `close < VWAP − 1σ` (BEAR) — same direction as gate 2 |
| **4. Candlestick** | Last bar is Hammer / Bullish Engulfing (BULL) OR Shooting Star / Bearish Engulfing (BEAR) |

**Anti-fakeout filter (5th, applied after gate 4):** the *paired inverse contract at the same strike* must NOT also be breaking the upper ATR band — rules out IV crush squeezes where both CE and PE inflate together.

---

## 4. Multi-Strike Selection Rule

When ≥ 1 of the 6 contracts passes all gates simultaneously:

```
score = 0.4 × volume_z
      + 0.3 × atr_breach_pct        # how far past the ATR band
      + 0.3 × |close - vwap| / vwap_std
```

Pick the **highest score**. If 0 contracts pass → skip the bar.

This makes the strategy "opportunistic" — it doesn't commit to a strike upfront; it lets the market choose.

---

## 5. SL / Target (per Index_trading_strategies.md §Core Rules)

| Index | SL on premium | Target | RR |
|---|---|---|---|
| NIFTY | 12 pts | VWAP line OR +30 pts (whichever first) | ~2:1 |
| BANKNIFTY | 25 pts | VWAP line OR +60 pts | ~2:1 |
| SENSEX | 15 pts | VWAP line OR +35 pts | ~2:1 |

Hard time-stop: 15:25 IST (existing SL monitor handles).

---

## 6. Capital Discipline

Per Index_trading_strategies.md §Core Rules ("never risk > 2%"):
- **1 lot per trigger × max 3 concurrent trades** = ~₹6k risk on ₹1 L capital (≈ 6%, tight but tradable for sandbox testing)
- Existing `_has_open_position(symbol, strategy_name)` enforces "no double-up on same strategy"

---

## 7. Files To Create / Modify

| File | Status | Role |
|---|---|---|
| `strategies/multi_strike_scalp.py` | NEW | Main strategy class (`MultiStrikeScalpStrategy`) |
| `strategies/candlestick.py` | NEW | `is_hammer`, `is_bullish_engulfing`, `is_shooting_star`, `is_bearish_engulfing` |
| `strategies/indicators.py` | MODIFY | Add `atr_bands(df, period=14, mult=2.0)` |
| `core/strike_lookup.py` | MODIFY | Add `get_atm_neighbors(symbol, spot, expiry_date)` returning 6 contracts |
| `DhanOmniEngine.py` | MODIFY | Register strategy L; instantiate; call in run loop |
| `agents/strategy_worker.py` | MODIFY | Add `MultiStrikeScalpWorker` class for v2 architecture |
| `DhanOmniEngine_v2.py` | MODIFY | Register worker L (12 workers total) |
| `backtest/multi_strike_backtest.py` | NEW | Replay against `kite_candles.db` for Apr 16-24 |

---

## 8. Validation Path (don't deploy until each step passes)

1. **Unit-test each gate** on synthetic 1-min option data.
2. **Backtest on Apr 16-24** option-candle data already in `kite_candles.db`.
3. **Decision rule for live deploy** — *all three* must hold:
   - signals/day between 2 and 10
   - win rate ≥ 55%
   - average winner ≥ 1.8 × average loser
4. **First-week paper trial** (2026-05-12 onwards): 1 lot per signal, audit daily via `audit_v2_runtime.py`.
5. **MetaAgent integration**: the KB will tag `strategy=MultiStrikeScalp`. Let RAG accumulate ~30 trades before relying on its filtering.

---

## 9. Known Risks (called out, not hidden)

- **Confluence dilution.** Most multi-indicator AND filters look great on backtest and underperform live because the 4-gate filter is so restrictive that the strategy fires 0–1 times/day, and one bad day kills the week.
- **Premium ≠ spot in IV crush moments.** Anti-fakeout gate helps but isn't bulletproof.
- **ATM-1 / ATM+1 wider spreads.** The 30-pt target may be eaten by realistic fills outside ATM.
- **Candlestick patterns on 1m option premium** are noisier than on spot. Pattern thresholds need empirical tuning, not textbook defaults.
- **Sandbox fills are not real fills.** Backtest results from sandbox are an upper bound on real performance.

---

## 10. Sequencing Decision

**Today (2026-05-01):** Build code only. No live run.
1. PHASE15_PLAN.md ✅
2. PHASE16_PLAN.md ✅ (this file)
3. `strategies/candlestick.py`
4. `strategies/indicators.py` — add `atr_bands`
5. `core/strike_lookup.py` — add `get_atm_neighbors`
6. `strategies/multi_strike_scalp.py`
7. Wire into v1 + v2
8. `backtest/multi_strike_backtest.py`
9. Update memory + startday_05may26.txt

**2026-05-05 (Tuesday):** Phase 15 verification first. Phase 16 stays disabled in code.
**2026-05-06 to 05-09:** Run backtest on existing data; tune thresholds if signal density is wrong.
**≥ 2026-05-12 (Monday):** First live deploy if backtest decision rule §8.3 passes.

---

## 11. Open Items After Phase 16

- Phase 3b: MetaAgent decision feedback loop (writes its own EXECUTE/SKIP outcomes back to KB).
- Phase 17 candidate: per-strategy regime filter in MetaAgent retrieval (current query doesn't filter by ADX regime).
- Phase 17 candidate: real-money pilot ₹10–25k after 1 month of stable paper performance.
