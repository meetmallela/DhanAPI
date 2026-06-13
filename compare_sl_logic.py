"""
compare_sl_logic.py
-------------------
Re-simulates all whatif_trades rows (that have 1-min candle data) using
both the OLD flat-5% SL logic and the NEW ATR-based v3 SL logic, then
prints a side-by-side P&L comparison.

Usage:
    python compare_sl_logic.py              # all dates with 1-min data
    python compare_sl_logic.py 2026-05-07  # specific date only
"""

import io, sys, sqlite3, json
from pathlib import Path
from datetime import datetime

import pandas as pd
import pytz

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
sys.path.insert(0, str(Path(__file__).parent))
from master_resource import MasterResource

IST            = pytz.timezone("Asia/Kolkata")
TRADING_DB     = MasterResource.get_trading_db_path()
KITE_CANDLE_DB = str(Path(MasterResource.MASTER_ROOT) / "data" / "kite_candles.db")

# ── OLD SL config (flat 5%, 3-stage) ─────────────────────────────────────────
OLD_CFG = {
    "initial_sl_percent":          5.0,
    "trailing_activation_percent": 3.0,
    "trailing_step_percent":       1.0,
    "hard_cutoff_time":            "15:25",
    "time_sl_enabled":             True,
    "time_sl_minutes":             15,
    "time_sl_min_move_pct":        1.0,
}

# ── NEW SL config (ATR-based v3) ──────────────────────────────────────────────
NEW_CFG = {
    "atr_multiplier":        1.5,
    "atr_period":            14,
    "default_sl_pct":        5.0,
    "atr_max_pct_options":   8.0,
    "breakeven_trigger_pct": 3.0,
    "trail_trigger_pct":     5.0,
    "trail_pct_am":          3.0,
    "trail_pct_pm":          1.5,
    "hard_cutoff_time":      "15:25",
    "time_sl_enabled":       True,
    "time_sl_minutes":       15,
    "time_sl_min_move_pct":  1.0,
}

# Try loading user overrides from sl_config.json
try:
    with open(Path(__file__).parent / "sl_config.json") as _f:
        _ov = json.load(_f)
        NEW_CFG.update({k: v for k, v in _ov.items() if not k.startswith("_")})
except Exception:
    pass


# ── Candle loader ─────────────────────────────────────────────────────────────

def _load_candles(tradingsymbol: str, run_date: str) -> pd.DataFrame | None:
    """Load 1-min candles from kite_candles.db for a given tradingsymbol + date."""
    try:
        con = sqlite3.connect(KITE_CANDLE_DB)
        rows = con.execute(
            "SELECT dt, open, high, low, close, volume "
            "FROM candles_1min "
            "WHERE tradingsymbol = ? AND dt LIKE ? "
            "ORDER BY dt",
            (tradingsymbol, f"{run_date}%"),
        ).fetchall()
        con.close()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
        return df
    except Exception as e:
        print(f"  [CANDLE ERROR] {tradingsymbol} {run_date}: {e}", file=sys.stderr)
        return None


# ── OLD SL simulation (flat 5%, 3-stage trailing) ────────────────────────────

def _old_update_trailing_sl(entry, peak, current_sl, action, stage, cfg):
    act_pct  = cfg["trailing_activation_percent"] / 100
    step_pct = cfg["trailing_step_percent"] / 100
    if action == "BUY":
        gain_pct = (peak - entry) / entry if entry else 0
        if gain_pct >= 3 * act_pct and stage < 3:
            new_sl = peak * (1 - step_pct)
            if new_sl > current_sl: return new_sl, 3
        elif gain_pct >= 2 * act_pct and stage < 2:
            new_sl = entry + (peak - entry) * 0.5
            if new_sl > current_sl: return new_sl, 2
        elif gain_pct >= act_pct and stage < 1:
            if entry > current_sl: return entry, 1
    else:
        gain_pct = (entry - peak) / entry if entry else 0
        if gain_pct >= 3 * act_pct and stage < 3:
            new_sl = peak * (1 + step_pct)
            if new_sl < current_sl: return new_sl, 3
        elif gain_pct >= 2 * act_pct and stage < 2:
            new_sl = entry - (entry - peak) * 0.5
            if new_sl < current_sl: return new_sl, 2
        elif gain_pct >= act_pct and stage < 1:
            if entry < current_sl: return entry, 1
    return current_sl, stage


def simulate_old(entry_price, action, entry_time, candles, cfg):
    is_long   = action == "BUY"
    sl_pct    = cfg["initial_sl_percent"] / 100
    sl_price  = entry_price * (1 - sl_pct if is_long else 1 + sl_pct)
    initial_sl = sl_price
    peak = entry_price; stage = 0; max_p = entry_price; min_p = entry_price

    ch, cm = map(int, cfg["hard_cutoff_time"].split(":"))
    t_on   = cfg["time_sl_enabled"]
    t_min  = cfg["time_sl_minutes"]
    t_move = cfg["time_sl_min_move_pct"] / 100

    mkt_open   = entry_time.replace(hour=9, minute=15, second=0, microsecond=0)
    start_time = max(entry_time, mkt_open)
    df = candles[candles["timestamp"] >= start_time].copy()
    if df.empty:
        return dict(exit_price=entry_price, exit_reason="NO_DATA",
                    pnl_per_unit=0.0, pnl_pct=0.0, sl_initial=initial_sl)

    for _, c in df.iterrows():
        ts  = c["timestamp"].to_pydatetime()
        ltp = c["close"]
        max_p = max(max_p, c["high"]); min_p = min(min_p, c["low"])
        if ts.hour > ch or (ts.hour == ch and ts.minute >= cm):
            return _fin(ltp, "CUTOFF", entry_price, action, initial_sl)
        peak = max(peak, c["high"]) if is_long else min(peak, c["low"])
        sl_price, stage = _old_update_trailing_sl(entry_price, peak, sl_price, action, stage, cfg)
        if is_long and c["low"] <= sl_price:
            return _fin(sl_price, "TRAILING_SL" if stage > 0 else "INITIAL_SL",
                        entry_price, action, initial_sl)
        if not is_long and c["high"] >= sl_price:
            return _fin(sl_price, "TRAILING_SL" if stage > 0 else "INITIAL_SL",
                        entry_price, action, initial_sl)
        if t_on:
            elapsed = (ts - start_time).total_seconds() / 60
            if elapsed >= t_min:
                move = ((ltp - entry_price) / entry_price if is_long
                        else (entry_price - ltp) / entry_price) if entry_price else 0
                if move < t_move:
                    return _fin(ltp, "TIME_SL", entry_price, action, initial_sl)

    last = df.iloc[-1]
    return _fin(last["close"], "EOD", entry_price, action, initial_sl)


# ── NEW ATR-based SL simulation (v3) ─────────────────────────────────────────

def _resample_5min(df1m):
    df = df1m.copy().set_index("timestamp").sort_index()
    r  = df.resample("5min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    return r.reset_index()


def _atr(df1m, period=14):
    try:
        df = _resample_5min(df1m)
        if len(df) < period: return None
        hl  = df["high"] - df["low"]
        hpc = abs(df["high"] - df["close"].shift(1))
        lpc = abs(df["low"]  - df["close"].shift(1))
        return float(pd.concat([hl, hpc, lpc], axis=1).max(axis=1).tail(period).mean())
    except Exception:
        return None


def _initial_sl_new(entry, action, candles, tradingsymbol, cfg):
    is_long  = action == "BUY"
    fl_pct   = cfg["default_sl_pct"] / 100
    fixed_sl = entry * (1 - fl_pct if is_long else 1 + fl_pct)

    atr_val = _atr(candles, cfg.get("atr_period", 14))
    if atr_val and atr_val > 0:
        atr_sl = entry - atr_val * cfg["atr_multiplier"] if is_long \
                 else entry + atr_val * cfg["atr_multiplier"]
        ts_up     = tradingsymbol.upper()
        is_option = ts_up.endswith("CE") or ts_up.endswith("PE")
        max_pct   = cfg.get("atr_max_pct_options", 8.0)
        if is_option and max_pct > 0:
            atr_sl = (max(atr_sl, entry * (1 - max_pct / 100)) if is_long
                      else min(atr_sl, entry * (1 + max_pct / 100)))
        if is_long and atr_sl < fixed_sl: return atr_sl
        if not is_long and atr_sl > fixed_sl: return atr_sl

    return fixed_sl


def simulate_new(entry_price, action, entry_time, candles, cfg, tradingsymbol=""):
    is_long    = action == "BUY"
    ch, cm     = map(int, cfg["hard_cutoff_time"].split(":"))
    initial_sl = _initial_sl_new(entry_price, action, candles, tradingsymbol, cfg)
    sl_price   = initial_sl; peak = entry_price

    be_trig  = cfg["breakeven_trigger_pct"] / 100
    tr_trig  = cfg["trail_trigger_pct"]     / 100
    tr_am    = cfg["trail_pct_am"]           / 100
    tr_pm    = cfg["trail_pct_pm"]           / 100
    t_on     = cfg["time_sl_enabled"]
    t_min    = cfg["time_sl_minutes"]
    t_move   = cfg["time_sl_min_move_pct"]  / 100

    mkt_open   = entry_time.replace(hour=9, minute=15, second=0, microsecond=0)
    start_time = max(entry_time, mkt_open)
    df = candles[candles["timestamp"] >= start_time].copy()
    if df.empty:
        return dict(exit_price=entry_price, exit_reason="NO_DATA",
                    pnl_per_unit=0.0, pnl_pct=0.0, sl_initial=initial_sl)

    max_p = entry_price; min_p = entry_price
    for _, c in df.iterrows():
        ts  = c["timestamp"].to_pydatetime()
        ltp = c["close"]
        max_p = max(max_p, c["high"]); min_p = min(min_p, c["low"])
        if ts.hour > ch or (ts.hour == ch and ts.minute >= cm):
            return _fin(ltp, "CUTOFF", entry_price, action, initial_sl)
        if is_long:
            peak = max(peak, c["high"])
            gain = (peak - entry_price) / entry_price if entry_price else 0
        else:
            peak = min(peak, c["low"])
            gain = (entry_price - peak) / entry_price if entry_price else 0

        if gain >= tr_trig:
            tf = tr_pm if ts.hour >= 13 else tr_am
            if is_long:
                new_sl = max(ltp * (1 - tf), entry_price)
                if new_sl > sl_price: sl_price = new_sl
            else:
                new_sl = min(ltp * (1 + tf), entry_price)
                if new_sl < sl_price: sl_price = new_sl
        elif gain >= be_trig:
            if is_long and entry_price > sl_price:   sl_price = entry_price
            elif not is_long and entry_price < sl_price: sl_price = entry_price

        if is_long and c["low"] <= sl_price:
            return _fin(sl_price, "TRAILING_SL" if sl_price > initial_sl else "INITIAL_SL",
                        entry_price, action, initial_sl)
        if not is_long and c["high"] >= sl_price:
            return _fin(sl_price, "TRAILING_SL" if sl_price < initial_sl else "INITIAL_SL",
                        entry_price, action, initial_sl)
        if t_on:
            elapsed = (ts - start_time).total_seconds() / 60
            if elapsed >= t_min:
                move = ((ltp - entry_price) / entry_price if is_long
                        else (entry_price - ltp) / entry_price) if entry_price else 0
                if move < t_move:
                    return _fin(ltp, "TIME_SL", entry_price, action, initial_sl)

    last = df.iloc[-1]
    return _fin(last["close"], "EOD", entry_price, action, initial_sl)


def _fin(exit_px, reason, entry, action, initial_sl):
    ppu = (exit_px - entry) if action == "BUY" else (entry - exit_px)
    pct = ppu / entry * 100 if entry else 0
    return dict(exit_price=round(exit_px, 2), exit_reason=reason,
                pnl_per_unit=round(ppu, 2), pnl_pct=round(pct, 2),
                sl_initial=round(initial_sl, 2))


# ── Main comparison ───────────────────────────────────────────────────────────

def main():
    date_filter = sys.argv[1] if len(sys.argv) > 1 else None

    con = sqlite3.connect(TRADING_DB)
    con.row_factory = sqlite3.Row

    q = """
        SELECT id, run_date, signal_id, symbol, tradingsymbol,
               channel_name, action, entry_time, entry_price,
               sl_initial, pnl_per_unit, pnl_pct, pnl_total,
               result, lot_size, exit_reason, data_quality
        FROM whatif_trades
        WHERE data_available = 1
          AND pnl_total IS NOT NULL
          AND data_quality IN ('KITE_API','LOCAL_DB','KITE_1MIN','KITE_API_LIVE')
    """
    params = []
    if date_filter:
        q += " AND run_date = ?"
        params.append(date_filter)
    q += " ORDER BY run_date, id"

    rows = con.execute(q, params).fetchall()
    con.close()

    if not rows:
        print("No 1-min candle rows found.")
        return

    print(f"Re-simulating {len(rows)} trades with both SL logics...\n")

    records = []
    skipped = 0
    for row in rows:
        ts_sym   = row["tradingsymbol"]
        run_date = row["run_date"]
        candles  = _load_candles(ts_sym, run_date)
        if candles is None or candles.empty:
            skipped += 1
            continue

        try:
            entry_time = datetime.fromisoformat(row["entry_time"])
            if entry_time.tzinfo is None:
                entry_time = IST.localize(entry_time)
            else:
                entry_time = entry_time.astimezone(IST)
        except Exception:
            skipped += 1
            continue

        entry_px = row["entry_price"]
        action   = row["action"] or "BUY"
        lot_size = row["lot_size"] or 1

        old_sim = simulate_old(entry_px, action, entry_time, candles, OLD_CFG)
        new_sim = simulate_new(entry_px, action, entry_time, candles, NEW_CFG,
                               tradingsymbol=ts_sym or "")

        records.append({
            "run_date":      run_date,
            "channel":       row["channel_name"],
            "symbol":        row["symbol"],
            "tradingsymbol": ts_sym,
            "action":        action,
            "entry_price":   entry_px,
            "lot_size":      lot_size,

            "old_sl":        old_sim["sl_initial"],
            "old_exit":      old_sim["exit_price"],
            "old_reason":    old_sim["exit_reason"],
            "old_pnl_u":     old_sim["pnl_per_unit"],
            "old_pnl_total": round(old_sim["pnl_per_unit"] * lot_size, 0),
            "old_result":    "PROFIT" if old_sim["pnl_per_unit"] > 0.01
                             else "LOSS" if old_sim["pnl_per_unit"] < -0.01 else "EVEN",

            "new_sl":        new_sim["sl_initial"],
            "new_exit":      new_sim["exit_price"],
            "new_reason":    new_sim["exit_reason"],
            "new_pnl_u":     new_sim["pnl_per_unit"],
            "new_pnl_total": round(new_sim["pnl_per_unit"] * lot_size, 0),
            "new_result":    "PROFIT" if new_sim["pnl_per_unit"] > 0.01
                             else "LOSS" if new_sim["pnl_per_unit"] < -0.01 else "EVEN",
        })

    if not records:
        print(f"No candle data found for any row ({skipped} skipped).")
        return

    df = pd.DataFrame(records)

    # ── Per-date summary ──────────────────────────────────────────────────────
    print("=" * 72)
    print("  DATE-WISE COMPARISON")
    print("=" * 72)
    print(f"  {'Date':<12} {'N':>4}  "
          f"{'OLD wins':>8} {'OLD PnL':>10}  "
          f"{'NEW wins':>8} {'NEW PnL':>10}  {'Delta':>10}")
    print(f"  {'-'*12} {'-'*4}  {'-'*8} {'-'*10}  {'-'*8} {'-'*10}  {'-'*10}")
    for dt, g in df.groupby("run_date"):
        old_w = (g["old_result"] == "PROFIT").sum()
        new_w = (g["new_result"] == "PROFIT").sum()
        old_p = g["old_pnl_total"].sum()
        new_p = g["new_pnl_total"].sum()
        n     = len(g)
        print(f"  {dt:<12} {n:>4}  "
              f"{old_w:>4}/{n:<3} {old_p:>+10,.0f}  "
              f"{new_w:>4}/{n:<3} {new_p:>+10,.0f}  {new_p - old_p:>+10,.0f}")

    # ── Overall totals ────────────────────────────────────────────────────────
    old_total  = df["old_pnl_total"].sum()
    new_total  = df["new_pnl_total"].sum()
    old_wins   = (df["old_result"] == "PROFIT").sum()
    new_wins   = (df["new_result"] == "PROFIT").sum()
    n_total    = len(df)

    print(f"  {'TOTAL':<12} {n_total:>4}  "
          f"{old_wins:>4}/{n_total:<3} {old_total:>+10,.0f}  "
          f"{new_wins:>4}/{n_total:<3} {new_total:>+10,.0f}  {new_total - old_total:>+10,.0f}")

    # ── Channel / strategy breakdown ──────────────────────────────────────────
    print()
    print("=" * 72)
    print("  CHANNEL BREAKDOWN (all dates)")
    print("=" * 72)
    print(f"  {'Channel':<32} {'N':>4}  "
          f"{'OLD win%':>8} {'OLD PnL':>10}  "
          f"{'NEW win%':>8} {'NEW PnL':>10}  {'Delta':>10}")
    print(f"  {'-'*32} {'-'*4}  {'-'*8} {'-'*10}  {'-'*8} {'-'*10}  {'-'*10}")

    for ch, g in df.groupby("channel"):
        n     = len(g)
        old_w = (g["old_result"] == "PROFIT").sum()
        new_w = (g["new_result"] == "PROFIT").sum()
        old_p = g["old_pnl_total"].sum()
        new_p = g["new_pnl_total"].sum()
        print(f"  {str(ch)[:32]:<32} {n:>4}  "
              f"{100*old_w/n:>7.0f}% {old_p:>+10,.0f}  "
              f"{100*new_w/n:>7.0f}% {new_p:>+10,.0f}  {new_p - old_p:>+10,.0f}")

    # ── Exit-reason breakdown ─────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("  EXIT REASON SHIFT  (old → new)")
    print("=" * 72)
    old_r = df["old_reason"].value_counts()
    new_r = df["new_reason"].value_counts()
    all_r = sorted(set(list(old_r.index) + list(new_r.index)))
    print(f"  {'Reason':<16} {'OLD':>8} {'NEW':>8}")
    for r in all_r:
        print(f"  {r:<16} {old_r.get(r, 0):>8} {new_r.get(r, 0):>8}")

    # ── SL width comparison ───────────────────────────────────────────────────
    df["old_sl_pct"] = (df["entry_price"] - df["old_sl"]).abs() / df["entry_price"] * 100
    df["new_sl_pct"] = (df["entry_price"] - df["new_sl"]).abs() / df["entry_price"] * 100
    print()
    print("=" * 72)
    print("  SL WIDTH COMPARISON  (% from entry)")
    print("=" * 72)
    print(f"  Old SL width : avg={df['old_sl_pct'].mean():.1f}%  "
          f"min={df['old_sl_pct'].min():.1f}%  max={df['old_sl_pct'].max():.1f}%")
    print(f"  New SL width : avg={df['new_sl_pct'].mean():.1f}%  "
          f"min={df['new_sl_pct'].min():.1f}%  max={df['new_sl_pct'].max():.1f}%")
    wider  = (df["new_sl_pct"] > df["old_sl_pct"] + 0.1).sum()
    tighter = (df["new_sl_pct"] < df["old_sl_pct"] - 0.1).sum()
    same   = n_total - wider - tighter
    print(f"  ATR wider than 5%  : {wider}/{n_total} trades")
    print(f"  ATR tighter than 5%: {tighter}/{n_total} trades")
    print(f"  Same (~5%)         : {same}/{n_total} trades")

    if skipped:
        print(f"\n  (Skipped {skipped} rows — candle data not in local DB)")

    print()


if __name__ == "__main__":
    main()
