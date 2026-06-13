"""
sl_engine.py
------------
Shared SL state-machine used by:
  • dhan_sl_monitor.py  — live tick-by-tick management
  • eod_whatif_backtest.py — EOD candle replay

Public API
----------
step_sl(entry, action, peak, current_sl, atr, cfg, hour) -> (new_sl, stage_name)

This is a pure function: no I/O, no logging, no side effects.
Callers own ATR fetching, peak tracking, state persistence, and logging.

Stage progression
-----------------
  ATR mode (atr is not None) — 3-stage:
    INITIAL  →  BREAKEVEN    (gain ≥ atr_beven_mult × ATR,  default 1.0)
             →  PROFIT_LOCK  (gain ≥ atr_lock_mult  × ATR,  default 1.5 → SL locks at entry ± 0.75 ATR)
             →  ATR_TRAILING (gain ≥ atr_trail_mult  × ATR,  default 2.5 → SL trails peak ± atr_trail_dist × ATR)

  Pct fallback (no ATR data):
    INITIAL  →  BREAKEVEN (gain_pct ≥ breakeven_activation_pct)
             →  TRAILING_AM / TRAILING_PM / TRAILING_FINAL (gain_pct ≥ trail_activation_pct)
             Trail is anchored to PEAK so SL never retreats on a dip.
"""


def step_sl(
    entry: float,
    action: str,
    peak: float,
    current_sl: float,
    atr: float | None,
    cfg: dict,
    hour: int,
) -> tuple[float, str]:
    """
    Single step of the SL state machine.

    Parameters
    ----------
    entry      : trade entry price
    action     : "BUY" or "SELL"
    peak       : best price seen since entry (max for BUY, min for SELL);
                 must be updated by the caller before each call
    current_sl : current stop-loss level
    atr        : 1-min ATR of the instrument, or None when unavailable
    cfg        : sl_config dict — relevant keys:
                   atr_trail_mult, atr_trail_dist, atr_lock_mult, atr_lock_dist,
                   atr_beven_mult, trail_activation_pct, breakeven_activation_pct,
                   trail_pct_am, trail_pct_pm, trail_pct_final
    hour       : current IST hour (for AM/PM trail-% selection in pct-fallback)

    Returns
    -------
    (new_sl, stage_name)
      new_sl     : updated SL (equals current_sl when no trigger fires)
      stage_name : "INITIAL" | "BREAKEVEN" | "PROFIT_LOCK" | "ATR_TRAILING" |
                   "TRAILING_AM" | "TRAILING_PM" | "TRAILING_FINAL"
    """
    is_long = action == "BUY"

    # ── ATR mode (preferred) — 3-stage ──────────────────────────────────────
    if atr and atr > 0:
        trail_act_mult = cfg.get("atr_trail_mult",  2.5)   # activation threshold (ATR units)
        trail_dist     = atr * cfg.get("atr_trail_dist",   trail_act_mult)  # trail distance from peak
        lock_mult      = cfg.get("atr_lock_mult",   1.5)   # Stage 2 activation threshold
        lock_dist      = atr * cfg.get("atr_lock_dist",  0.75)  # Stage 2 lock distance from entry
        beven_mult     = cfg.get("atr_beven_mult",  1.0)   # Stage 1 activation threshold
        beven_dist     = atr * beven_mult

        gain     = (peak - entry) if is_long else (entry - peak)
        gain_atr = gain / atr

        if is_long:
            trail_sl = round(peak - trail_dist, 2)
            lock_sl  = round(entry + lock_dist, 2)

            # Stage 3: Active trailing from peak
            if gain_atr >= trail_act_mult and trail_sl > entry and trail_sl > current_sl:
                return trail_sl, "ATR_TRAILING"

            # Stage 2: Lock partial profit at entry + 0.75 ATR
            if gain_atr >= lock_mult and current_sl < lock_sl:
                return lock_sl, "PROFIT_LOCK"

            # Stage 1: Move to breakeven
            if gain >= beven_dist and current_sl < entry:
                return entry, "BREAKEVEN"

        else:  # SELL
            trail_sl = round(peak + trail_dist, 2)
            lock_sl  = round(entry - lock_dist, 2)

            # Stage 3: Active trailing from peak
            if gain_atr >= trail_act_mult and trail_sl < entry and trail_sl < current_sl:
                return trail_sl, "ATR_TRAILING"

            # Stage 2: Lock partial profit at entry − 0.75 ATR
            if gain_atr >= lock_mult and current_sl > lock_sl:
                return lock_sl, "PROFIT_LOCK"

            # Stage 1: Move to breakeven
            if gain >= beven_dist and current_sl > entry:
                return entry, "BREAKEVEN"

        return current_sl, "INITIAL"

    # ── Percentage fallback (no ATR data) ─────────────────────────────────────
    if hour >= 15:
        trail_pct  = cfg.get("trail_pct_final", 3.0) / 100
        stage_name = "TRAILING_FINAL"
    elif hour >= 13:
        trail_pct  = cfg.get("trail_pct_pm",    2.0) / 100
        stage_name = "TRAILING_PM"
    else:
        trail_pct  = cfg.get("trail_pct_am",    3.0) / 100
        stage_name = "TRAILING_AM"

    trail_act = cfg.get("trail_activation_pct",     2.0) / 100
    beven_act = cfg.get("breakeven_activation_pct", 1.0) / 100

    if is_long:
        gain_pct = (peak - entry) / entry if entry else 0
        if gain_pct >= trail_act:
            new_sl = round(max(peak * (1 - trail_pct), entry), 2)
            if new_sl > current_sl:
                return new_sl, stage_name
        elif gain_pct >= beven_act and current_sl < entry:
            return entry, "BREAKEVEN"
    else:
        gain_pct = (entry - peak) / entry if entry else 0
        if gain_pct >= trail_act:
            new_sl = round(min(peak * (1 + trail_pct), entry), 2)
            if new_sl < current_sl:
                return new_sl, stage_name
        elif gain_pct >= beven_act and current_sl > entry:
            return entry, "BREAKEVEN"

    return current_sl, "INITIAL"
