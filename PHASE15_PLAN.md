# Phase 15 — v2 Audit Bug Fixes (Plan + Post-Mortem)

**Date opened:** 2026-05-01
**Date closed (code):** 2026-05-01
**Date verified live:** *pending — scheduled for 2026-05-05*
**Session:** TRD-20260416 (Phase 15 in build log)

---

## 1. Why Phase 15 Existed

Phase 4 of session TRD-20260416 produced `audit_v2_runtime.py`. Its first run on Apr 23–24 data revealed a broken funnel:

| Symptom | Evidence |
|---|---|
| 23,098 non-NEUTRAL strategy signals on Apr 24 → **0 OmniEngine orders** | `strategy_signals` vs `orders` cross-tab |
| OptionScalper alone fired 20,138 BULLISH/BEARISH in one day | impossible at 5 indices × 75 5m candles = max 375 |
| 5 strategies showed 0 fires (TriplePattern, BB, VWAPReclaim, CPRBreakout, PairLeadership) | not actually broken — see §5 |
| Only "TG:SIGNAL" appeared in `orders.strategy_name` on Apr 24 | OmniEngine path looked dead |

Two of these were real bugs. One was a logging hole that hid the real cause. One was a false alarm.

---

## 2. Three Real Bugs Found

### Bug A — Crossover-strategy dedup let intra-bar flip-flops through

**Affected:** `strategies/option_scalper.py`, `strategies/ema_9_21.py`, `strategies/supertrend_macd.py`

**Mechanism:** OmniEngine ticks every 10 s. While a 5-min bar is in progress, the partial bar's last close shifts each tick. EMA recomputes each tick. Crossover detection flips BULLISH ↔ BEARISH within the same bar window. The old `_last_signal` dedup blocked **consecutive same-direction** fires only — alternations passed straight through, generating 30+ fires per chop window per index.

**Fix:** two-stage dedup:
1. `_last_bar_ts: dict[str, Timestamp]` — at most one fire per `(idx, bar timestamp)`.
2. `_last_fired: dict[str, float]` — `time.monotonic()` cooldown:
   - 5 min for 5m strategies (option_scalper, supertrend_macd)
   - 2 min for 1m strategies (ema_9_21)

**Verification (synthetic):** 1 fire on first call, 9 NEUTRAL on identical follow-up calls; re-arms only after cooldown elapsed AND bar timestamp advances.

### Bug B — OrderPlacer silent permanent kill switch

**Affected:** `core/order_placer.py`

**Mechanism:**
- All order status flowed through `print()`, not `logger`. Stdout/stderr is captured to a separate file under `pythonw.exe + DETACHED_PROCESS`, so failures were invisible in the main engine log.
- `failed_attempts >= 20` permanently disabled the placer for the rest of the process. On Apr 24, sandbox.dhan.co timed out for ~5 minutes; OrderPlacer hit 20 fails, then silently returned `None` for the remaining 6 hours of the session.

**Fix:**
- All `print()` → `logger.info / warning / error`.
- New `_maybe_reset_failures()`: if `time.monotonic() - last_failure > 30 min`, zero the counter.
- New `_stopped_logged` flag: log the kill-switch state ONCE at ERROR level, not on every blocked call.

### Bug C — PairLeadership invisible to audit

**Affected:** `DhanOmniEngine.py` (call site at line 666–674)

**Mechanism:** every other strategy in the run loop calls `_log_strategy_eval(...)` regardless of signal value, so NEUTRAL evaluations are throttled-logged and BULLISH/BEARISH always logged. PairLeadership's call site skipped this entirely → strategy never appeared in `strategy_signals`, dashboard, or audit, even when it was firing.

**Fix:** added `_log_strategy_eval("NIFTY", "PairLeadership", pair_signal, nifty_spot, df_1m, df_5m)` immediately after `check_signal`, mirroring the pattern used by all other strategies.

---

## 3. False Alarm — The "5 Silent Strategies"

The audit on Apr 23–24 reported zero fires for TriplePattern, BB_MeanReversion, VWAPReclaim, CPRBreakout, PairLeadership.

PairLeadership was Bug C above. The other four were added in **Phases 9–10b on 2026-04-25** — *after* the data we were auditing. They were silent because they didn't exist yet in the running OmniEngine. A re-audit after the next live session will confirm they fire.

---

## 4. Files Changed

| File | Change |
|---|---|
| `strategies/option_scalper.py` | Two-stage dedup + 5-min cooldown |
| `strategies/ema_9_21.py` | Two-stage dedup + 2-min cooldown |
| `strategies/supertrend_macd.py` | Two-stage dedup + 5-min cooldown |
| `core/order_placer.py` | logger replaces print; 30-min auto-reset of failed_attempts |
| `DhanOmniEngine.py` | Added `_log_strategy_eval` for PairLeadership |

All five files pass `ast.parse` (verified).

---

## 5. Verification Plan — 2026-05-05 (Tuesday)

User is away on 2026-05-04, so first live test is 2026-05-05. Detailed steps in `startday_05may26.txt`. Headlines:

1. Start v2 (`python DhanOmniEngine_v2.py`) at 09:00 IST. Do NOT run v1 in parallel.
2. Mid-session spot check at 12:00 IST: `OptionScalper` non-NEUTRAL count should be ≤ 200 (was firing 20k by EOD).
3. After 16:00 IST run `python audit_v2_runtime.py`. Four required outcomes:
   1. Engine ALIVE (`alive=15 dead=0`)
   2. OptionScalper fires ≤ 375
   3. PairLeadership row appears in funnel
   4. ≥ 1 OmniEngine `strategy_name` in `orders` table
4. If sandbox times out, look for `[OrderPlacer] X min since last failure — resetting counter` lines confirming auto-recovery.

If all four pass → mark Phase 15 VERIFIED in session memory; advance to Phase 16.
If any fails → capture v2 log + audit report + stderr file; debug in next session.

---

## 6. Risks Still Carried After Phase 15

- Apr 24-style sandbox outages: auto-reset is implemented but unverified live.
- Dedup fix is unit-tested but not stress-tested across all 5 indices simultaneously.
- The fix doesn't address *why* `df_5m` partial bars update so aggressively in `_refresh_kite_indices`. A future cleanup would only push to buffer when a 5m bar is **closed**.

---

## 7. Open Items (Phase 3b, separate)

The MetaAgent reads the KB but never writes its own EXECUTE/SKIP outcomes back. Closing this loop is Phase 3b — will pick up after Phase 16 verification.
