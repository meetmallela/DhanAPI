"""
dhan_sl_monitor.py
------------------
Agent 3: Stop-Loss Monitor

Polls the orders table for OPEN positions, tracks live LTP via Kite API
(primary) and Dhan WebSocket (supplement), manages trailing SL, and forces
exit at 15:25 IST for equity/index positions and 23:30 IST for MCX commodity
positions (configurable via hard_cutoff_time / commodity_cutoff_time in sl_config.json).

LTP source priority
-------------------
1. Kite batch ltp() — covers NFO + BFO + MCX reliably; called once per cycle
2. Dhan WebSocket (LTPFeed) — supplemental; reliable for NSE_FNO only;
   zero-value ticks from BSE_FNO/sandbox are ignored (not None, not > 0)
3. Candle DB fallback (kite_candles.db latest close) — 1-min delayed
4. If all fail, position is skipped that cycle (never uses stale/zero data)
"""

import mysql_sqlite_bridge
import sqlite3
import time
import json
import pytz
import numpy as np
import threading
from datetime import datetime, timedelta, date
from pathlib import Path
from core.dhan_client import DhanClient
from core.ltp_feed import LTPFeed
from core.strike_lookup import StrikeLookup
from master_resource import MasterResource
from sl_engine import step_sl

# Kite client for live option LTP (fallback when Dhan WS/REST unavailable in sandbox)
import sys as _sys
_kcs_lib = r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib'
if _kcs_lib not in _sys.path:
    _sys.path.insert(0, _kcs_lib)
try:
    from kite_candle_store import get_kite as _get_kite, resolve_option_token as _resolve_opt_token
    _KITE_LTP_ENABLED = True
except Exception:
    _KITE_LTP_ENABLED = False

logger = MasterResource.setup_shared_logger("dhan_sl_monitor")

IST = pytz.timezone("Asia/Kolkata")

_DAILY_PNL_PATH  = MasterResource.MASTER_ROOT / "data" / "daily_pnl_state.json"

# Market hours gate for WebSocket stale-data check (IST)
_MARKET_OPEN_H, _MARKET_OPEN_M   = 9, 15
_MARKET_CLOSE_H, _MARKET_CLOSE_M = 15, 30
_pnl_lock       = threading.Lock()

def _update_daily_pnl_state(trade_pnl: float):
    """Accumulate realized P&L into the shared state file for the circuit breaker."""
    today = date.today().isoformat()
    with _pnl_lock:
        try:
            if _DAILY_PNL_PATH.exists():
                with open(_DAILY_PNL_PATH) as f:
                    state = json.load(f)
                if state.get("date") != today:
                    state = {"date": today, "realized_pnl": 0.0}
            else:
                state = {"date": today, "realized_pnl": 0.0}
            state["realized_pnl"] = round(state["realized_pnl"] + trade_pnl, 2)
            state["last_updated"] = datetime.now().isoformat()
            with open(_DAILY_PNL_PATH, "w") as f:
                json.dump(state, f, indent=4)
            logger.info(f"[PNL] Daily realized P&L: Rs.{state['realized_pnl']:+.2f}")
        except Exception as e:
            logger.warning(f"[PNL] Could not update daily_pnl_state.json: {e}")

def _write_sl_exit(tradingsymbol: str):
    """
    Record tradingsymbol in the sl_exits DB table so DhanOmniEngine can block
    cross-strategy re-entries on the same option today.  INSERT OR IGNORE
    ensures idempotency; SQLite WAL mode handles concurrent reads safely.
    """
    if not tradingsymbol:
        return
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO sl_exits (tradingsymbol, exit_date, created_at) VALUES (?, ?, ?)",
            (tradingsymbol, date.today().isoformat(), datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        logger.info("[SL BLACKLIST] Added %s to today's SL exit blacklist", tradingsymbol)
    except Exception as e:
        logger.debug("[SL BLACKLIST] Could not write sl_exits to DB: %s", e)


_INDEX_BASES = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'SENSEX', 'MIDCPNIFTY', 'BANKEX'}
_KITE_DB     = r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db'
_MCX_DB      = r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\mcx_candles.db'

# Maps the leading alpha portion of a Dhan MCX tradingsymbol (e.g. 'CRUDEOIL26JUN8100PE')
# to the symbol key used in mcx_candles_1min (populated by MCXCandleCollector).
_MCX_SYM_MAP = {
    "CRUDEOIL":   "CRUDEOILM",
    "CRUDEOILM":  "CRUDEOILM",
    "COPPER":     "COPPERM",
    "COPPERM":    "COPPERM",
    "NATURALGAS": "NATURALGAS",
    "GOLD":       "GOLDM",
    "GOLDM":      "GOLDM",
    "GOLDPETAL":  "GOLDM",
    "SILVER":     "SILVERM",
    "SILVERM":    "SILVERM",
    "SILVERMIC":  "SILVERM",
    "ZINC":       "ZINC",
    "ALUMINIUM":  "ALUMINIUM",
    "LEAD":       "LEAD",
    "NICKEL":     "NICKEL",
}

# Commodity bases that should route to mcx_candles.db, not kite_candles.db
_MCX_BASES = frozenset(_MCX_SYM_MAP.keys())

import re as _re

def _mcx_candle_sym(tradingsymbol: str) -> str | None:
    """Return the mcx_candles_1min symbol for a Dhan MCX tradingsymbol, or None.
    Handles both compact format (CRUDEOILM26JUN9000CE) and
    hyphenated format (CRUDEOIL-16Jun2026-9000-CE).
    """
    if '-' in tradingsymbol:
        base = tradingsymbol.split('-')[0].upper()
        return _MCX_SYM_MAP.get(base)
    m = _re.match(r'^([A-Z]+)\d', tradingsymbol or "")
    return _MCX_SYM_MAP.get(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn():
    return sqlite3.connect(MasterResource.get_trading_db_path(), timeout=30)


def _ensure_db_columns():
    """Add any missing columns to the orders table and create sl_exits table."""
    conn = _get_conn()
    cur  = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA table_info(orders)")
    existing = {row[1] for row in cur.fetchall()}
    for col, typ in [
        ("security_id",        "TEXT"),
        ("exchange_segment",   "TEXT"),
        ("actual_entry_price", "REAL"),
        ("slippage",           "REAL"),
        ("ltp",                "REAL"),
        ("sl_stage",           "TEXT"),
        ("peak_price",         "REAL"),
        ("price_signal",       "TEXT"),
    ]:
        if col not in existing:
            cur.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
            logger.info(f"DB migration: added column orders.{col}")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sl_exits (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tradingsymbol TEXT NOT NULL,
            exit_date     TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            UNIQUE(tradingsymbol, exit_date)
        )
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class DhanSLMonitor:
    def __init__(self, is_sandbox: bool = True):
        self.client     = DhanClient(is_sandbox=is_sandbox)
        self.is_sandbox = is_sandbox

        # LTP feed (WebSocket, background thread)
        self.ltp_feed = LTPFeed(
            client_id    = self.client.client_id,
            access_token = self.client.access_token,
        )

        # Strike lookup (resolves security_id from tradingsymbol in old orders)
        self.strike_lookup = StrikeLookup()

        # Kite client for live option LTP (sandbox Dhan has no working market data)
        self._kite = _get_kite() if _KITE_LTP_ENABLED else None
        if self._kite:
            logger.info("Kite LTP client ready — will use as primary LTP source")
        else:
            logger.warning("Kite LTP client unavailable — LTP will be limited")

        # Kite LTP cache: {order_id: ltp} — refreshed every poll cycle via batch call
        self._kite_ltp: dict[str, float] = {}

        # In-memory position book
        # {order_id: {symbol, security_id, exchange_segment, entry_price,
        #             sl_price, action, quantity, peak_price, stage}}
        self.active_trades: dict[str, dict] = {}

        # Load SL config
        self.sl_config = self._load_sl_config()

        # DB migration
        _ensure_db_columns()

        # Start the WebSocket feed
        self.ltp_feed.start()

        logger.info(f"DhanSLMonitor initialized (sandbox={is_sandbox})")

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _load_sl_config(self) -> dict:
        try:
            with open(MasterResource.get_sl_config_path()) as f:
                return json.load(f)
        except Exception:
            return {
                "initial_sl_percent":          5,
                "index_sl_percent":            8,
                "hard_cutoff_time":            "15:25",
                "commodity_cutoff_time":       "23:30",
                "min_hold_candles":            10,
                "time_sl_enabled":             True,
                "time_sl_minutes":             15,
                "time_sl_min_move_pct":        1.0,
            }

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _check_btst_exits(self, now_ist: datetime):
        """
        Exit BTST positions at market open (09:15–09:20 IST) on their sell_date.
        Called once per main-loop iteration; does nothing outside that 5-min window.
        """
        from datetime import time as _dtime
        exit_start = _dtime(9, 15)
        exit_end   = _dtime(9, 20)
        today_str  = now_ist.date().isoformat()
        if not (exit_start <= now_ist.time() <= exit_end):
            return

        for order_id, trade in list(self.active_trades.items()):
            if not trade.get("btst_flag"):
                continue
            sell_date = trade.get("btst_sell_date")
            if sell_date != today_str:
                continue

            logger.info(
                "[BTST EXIT] %s — sell_date=%s reached at market open. Closing at LTP.",
                order_id, sell_date,
            )
            ltp = trade.get("last_ltp_for_sl") or trade.get("entry_price", 0)
            self._close_position(order_id, trade, ltp, reason="BTST_SELL_DATE")

    def monitor_positions(self):
        logger.info("SL Monitor: starting polling loop")
        _last_hb          = time.monotonic()
        _last_cfg_reload  = time.monotonic()
        _CFG_RELOAD_SECS  = 60          # hot-reload sl_config.json every 60 s
        _BTST_INTERVAL_S      = 900          # 15 min between BTST SL checks
        _LONGTERM_INTERVAL_S  = 14400        # 4 hours between LONGTERM SL checks
        _btst_last_check      = {}           # order_id → monotonic time of last check
        _lt_last_check        = {}           # order_id → monotonic time of last LONGTERM check
        while True:
            try:
                now_ist = datetime.now(IST)
                if now_ist.weekday() >= 5:   # Saturday=5, Sunday=6
                    logger.debug("Weekend — SL monitor paused")
                    time.sleep(60)
                    continue

                # Hot-reload sl_config.json so changes take effect without restart
                if time.monotonic() - _last_cfg_reload >= _CFG_RELOAD_SECS:
                    new_cfg = self._load_sl_config()
                    if new_cfg != self.sl_config:
                        self.sl_config = new_cfg
                        logger.info("[CONFIG] sl_config.json reloaded")
                    _last_cfg_reload = time.monotonic()

                self.sync_active_orders()

                # BTST next-day market-open exit: at 09:15–09:20 IST on sell_date
                self._check_btst_exits(now_ist)

                # Refresh Kite LTPs for all active trades in one API call
                self._batch_kite_ltp_update()

                now_mono = time.monotonic()
                for order_id, trade in list(self.active_trades.items()):
                    ptype = trade.get("position_type", "INTRADAY")

                    # LONGTERM: check SL every 4 hours only, skip EOD forced exit
                    if ptype == "LONGTERM":
                        last = _lt_last_check.get(order_id, 0.0)
                        if now_mono - last < _LONGTERM_INTERVAL_S:
                            continue
                        _lt_last_check[order_id] = now_mono
                    # BTST: only evaluate SL every 15 minutes
                    elif trade.get("btst_flag"):
                        last = _btst_last_check.get(order_id, 0.0)
                        if now_mono - last < _BTST_INTERVAL_S:
                            continue
                        _btst_last_check[order_id] = now_mono

                    self.manage_trailing_sl(order_id, trade)

                self._check_hard_cutoff()

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

            # Heartbeat every 2 min by wall-clock (not iteration count — Kite call may block)
            if time.monotonic() - _last_hb >= 120:
                logger.info(f"[HEARTBEAT] SL Monitor alive — tracking {len(self.active_trades)} position(s)")
                _last_hb = time.monotonic()

                # WebSocket stale-data check (SensexGaamaAnalyser pattern):
                # If feed is connected but no tick has arrived in the last 30s during
                # market hours, the data pipe may be frozen without a connection error.
                try:
                    from datetime import time as _dtime
                    _now_t = datetime.now().time()
                    _in_mkt = (
                        _dtime(_MARKET_OPEN_H, _MARKET_OPEN_M)
                        <= _now_t
                        <= _dtime(_MARKET_CLOSE_H, _MARKET_CLOSE_M)
                    )
                    if _in_mkt and self.ltp_feed.is_connected:
                        _age = self.ltp_feed.tick_age_seconds
                        if _age > 30:
                            logger.warning(
                                "[WS STALE] Dhan WebSocket last tick %.0fs ago — "
                                "data pipe may be frozen. Forcing reconnect.",
                                _age,
                            )
                            self.ltp_feed.force_reconnect()
                except Exception as _ws_err:
                    logger.debug("[WS STALE] heartbeat check error: %s", _ws_err)

            # DB heartbeat — confirms loop is executing (detected by watchdog.py)
            try:
                from core.watchdog_store import heartbeat as _wdhb
                _wdhb("sl_monitor", f"tracking={len(self.active_trades)}")
            except Exception:
                pass

            time.sleep(5)

    # ------------------------------------------------------------------
    # Order sync
    # ------------------------------------------------------------------

    def sync_active_orders(self):
        """
        Pull OPEN orders from the DB.  For each new order:
          - resolve security_id (from DB column, or from tradingsymbol via StrikeLookup)
          - subscribe it to the WebSocket feed
          - add to active_trades
        """
        try:
            conn = _get_conn()
            cur  = conn.cursor()
            cur.execute("""
                SELECT order_id, symbol, entry_price, quantity,
                       stop_loss, action, tradingsymbol,
                       security_id, exchange_segment, created_at,
                       actual_entry_price, strategy_name, strategy_params,
                       COALESCE(btst_flag, 0), btst_sell_date,
                       COALESCE(position_type, 'INTRADAY')
                FROM   orders
                WHERE  status = 'OPEN'
                  AND (entry_placed_at IS NOT NULL
                       OR actual_entry_price IS NOT NULL
                       OR order_id LIKE 'PAPER_%'
                       OR order_id LIKE 'SANDBOX_%')
            """)
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            logger.error(f"sync_active_orders DB error: {e}")
            return

        seen_ids = set()
        for row in rows:
            (order_id, symbol, entry_price, qty,
             sl, _action_raw, tradingsymbol,
             security_id, exchange_segment, created_at,
             actual_entry_price, strategy_name, strategy_params_raw,
             btst_flag, btst_sell_date, position_type) = row
            action = (_action_raw or "BUY").upper()   # normalise: 'buy'/'BUY'/'Buy' → 'BUY'

            seen_ids.add(order_id)

            # Resolve security_id if not stored in DB
            if not security_id and tradingsymbol:
                info = self.strike_lookup.get_by_trading_symbol(tradingsymbol)
                if info:
                    security_id      = info["security_id"]
                    exchange_segment = info["exchange_segment"]
                    # Persist to DB so we don't look it up every cycle
                    self._save_security_id(order_id, security_id, exchange_segment)

            if not security_id:
                logger.warning(
                    f"Cannot resolve security_id for order {order_id} "
                    f"(symbol={symbol}, tradingsymbol={tradingsymbol}) — skipping"
                )
                continue

            exchange_segment = exchange_segment or "NSE_FNO"

            if order_id not in self.active_trades:
                is_index = any(symbol.upper().startswith(b) for b in _INDEX_BASES) or \
                           any((tradingsymbol or '').upper().startswith(b) for b in _INDEX_BASES)
                max_sl_pct = self.sl_config.get("index_sl_percent", 8) if is_index \
                             else self.sl_config.get("initial_sl_percent", 5)
                min_sl_pct = self.sl_config.get("initial_sl_percent", 5)

                # Fixed-pct floor (always valid, used when ATR unavailable)
                floor_sl = (entry_price * (1 - max_sl_pct / 100)
                            if action == "BUY"
                            else entry_price * (1 + max_sl_pct / 100))

                # ATR-based initial SL (Priority 3): gives more room on volatile days,
                # falls back to floor when ATR < min_sl_pct of entry.
                # Mirrors WhatIf _compute_initial_sl() exactly.
                atr = self._compute_option_atr(tradingsymbol or "")
                if atr and atr > 0:
                    atr_mult = self.sl_config.get("atr_multiplier", 1.5)
                    if action == "BUY":
                        atr_sl  = round(entry_price - atr * atr_mult, 2)
                        atr_sl  = max(atr_sl, entry_price * (1 - max_sl_pct / 100))  # cap width
                        computed_sl = atr_sl if atr_sl < floor_sl else floor_sl       # use if wider
                    else:
                        atr_sl  = round(entry_price + atr * atr_mult, 2)
                        atr_sl  = min(atr_sl, entry_price * (1 + max_sl_pct / 100))
                        computed_sl = atr_sl if atr_sl > floor_sl else floor_sl
                else:
                    computed_sl = floor_sl

                # If strategy provided an explicit SL, use it; otherwise use our computed SL
                initial_sl  = sl or computed_sl
                sl_pct_used = abs(entry_price - initial_sl) / entry_price * 100

                # Parse opened_at from DB created_at (ISO string)
                try:
                    opened_at = datetime.fromisoformat(created_at) if created_at else datetime.now(IST)
                except Exception:
                    opened_at = datetime.now(IST)

                hold_secs  = self.sl_config.get("min_hold_candles", 3) * 60
                hold_until = opened_at.replace(tzinfo=None) + timedelta(seconds=hold_secs)

                # Parse strategy-specific context (e.g. ORB levels for ORB_VWAP)
                try:
                    sp = json.loads(strategy_params_raw) if strategy_params_raw else {}
                except Exception:
                    sp = {}

                self.active_trades[order_id] = {
                    "symbol":            symbol,
                    "tradingsymbol":     tradingsymbol or "",
                    "security_id":       security_id,
                    "exchange_segment":  exchange_segment,
                    "entry_price":       entry_price,
                    "quantity":          qty or 1,
                    "sl_price":          initial_sl,
                    "action":            action,
                    "peak_price":        entry_price,
                    "stage":             "INITIAL",
                    "opened_at":         opened_at,
                    "hold_until":        hold_until,
                    "last_sl_minute":    -1,
                    "last_ltp_for_sl":   entry_price,
                    "is_index":          is_index,
                    "strategy_name":     strategy_name or "",
                    # BTST: hold overnight, exit next morning
                    "btst_flag":         bool(btst_flag),
                    "btst_sell_date":    btst_sell_date,
                    # Position type: INTRADAY | BTST | LONGTERM
                    "position_type":     position_type or "INTRADAY",
                    # ORB invalidation levels (populated for ORB_VWAP trades only)
                    "orb_high":          sp.get("orb_high"),
                    "orb_low":           sp.get("orb_low"),
                    # Price signal tracking (volume + direction)
                    "_ltp_buf":          [],
                    "_bar_closes":       [],
                    "_sig_last_min":     -1,
                    "_last_price_signal": "",
                    # Kite LTP token (resolved once; used for batch LTP fetches)
                    "_kite_token":       self._resolve_kite_token(tradingsymbol or ""),
                }
                # Subscribe to live feed
                self.ltp_feed.subscribe(security_id, exchange_segment)

                # Fetch actual fill price in background (non-blocking; sandbox returns no fill data)
                if not actual_entry_price:
                    import threading as _thr
                    _thr.Thread(
                        target=self._record_slippage,
                        args=(order_id, entry_price, action),
                        daemon=True,
                    ).start()

                logger.info(
                    f"New position tracked: {symbol} ({'INDEX' if is_index else 'STOCK'}) | "
                    f"order={order_id} | entry={entry_price} | "
                    f"SL={initial_sl:.2f} ({sl_pct_used:.1f}% {'ATR' if atr else 'fixed'}) | "
                    f"hold_until={hold_until.strftime('%H:%M:%S')}"
                )

        # Remove trades that are no longer OPEN in the DB (closed externally)
        for oid in list(self.active_trades.keys()):
            if oid not in seen_ids:
                trade = self.active_trades.pop(oid)
                self.ltp_feed.unsubscribe(trade["security_id"])
                logger.info(f"Position {oid} removed from active book (closed externally)")

    def _save_security_id(self, order_id: str, security_id: str, exchange_segment: str):
        try:
            conn = _get_conn()
            conn.execute(
                "UPDATE orders SET security_id=?, exchange_segment=? WHERE order_id=?",
                (security_id, exchange_segment, order_id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Could not persist security_id for {order_id}: {e}")

    def _record_slippage(self, order_id: str, signal_price: float, action: str):
        """Fetch the actual Dhan fill price and write slippage to the DB."""
        try:
            resp = self.client.get_order_by_id(order_id)
            if resp.get("status") != "success":
                return
            data = resp.get("data") or {}
            # Dhan returns list or single dict depending on SDK version
            if isinstance(data, list):
                data = data[0] if data else {}
            fill_price = float(data.get("averageTradedPrice") or 0)
            if fill_price <= 0:
                return   # not filled yet, skip
            slippage = (
                round(fill_price - signal_price, 4) if action == "BUY"
                else round(signal_price - fill_price, 4)
            )
            conn = _get_conn()
            conn.execute(
                "UPDATE orders SET actual_entry_price=?, slippage=? WHERE order_id=?",
                (fill_price, slippage, order_id),
            )
            conn.commit()
            conn.close()
            logger.info(
                f"Slippage [{order_id}]: signal={signal_price} fill={fill_price} "
                f"slip={slippage:+.4f} ({'BUY' if action=='BUY' else 'SELL'})"
            )
        except Exception as e:
            logger.debug(f"Slippage fetch skipped for {order_id}: {e}")

    # ------------------------------------------------------------------
    # SL management
    # ------------------------------------------------------------------

    def manage_trailing_sl(self, order_id: str, trade: dict):
        security_id      = trade["security_id"]
        exchange_segment = trade["exchange_segment"]
        entry_price      = trade["entry_price"]
        current_sl       = trade["sl_price"]
        action           = (trade["action"] or "BUY").upper()

        # --- 1. Get LTP: Kite batch → Dhan WebSocket → Candle DB ----------
        # Kite is primary: covers NFO, BFO (SENSEX/BANKEX), MCX uniformly.
        # Dhan WebSocket silently delivers 0.0 for BSE_FNO; treat 0 as missing.
        ltp = self._kite_ltp.get(order_id) or None   # 0.0 → None

        if ltp is None:
            ws_ltp = self.ltp_feed.get_ltp(security_id)
            if ws_ltp:                               # 0.0 from BSE_FNO → skip
                ltp = ws_ltp

        if ltp is None:
            ltp = self._get_candle_ltp(trade.get("tradingsymbol", ""))

        if ltp is None:
            logger.debug(
                f"LTP unavailable for {trade['symbol']} "
                f"(security={security_id}) — skipping cycle"
            )
            return

        # --- 1.4. MCX-OPT: prefer sweeper candle LTP; guard against stale Kite ----
        # Kite ltp() returns last *traded* price which is often the previous session's
        # close for illiquid MCX options.  MCXOptionCandleSweeper stores today's
        # intraday candles under the option symbol — use that when available.
        if trade.get("exchange_segment") == "MCX-OPT":
            entry = trade["entry_price"]

            # --- Hard guard: stop_loss looks like underlying futures price ---
            # If stop_loss > 2× entry the SL was almost certainly set to the
            # commodity futures price (e.g. NatGas at ~317 vs option at ~14).
            # This was written incorrectly by order_placer from the TG signal.
            # Neutralise it so a phantom SL trigger cannot close the trade.
            stored_sl = trade.get("sl_price", 0) or 0
            if entry > 0 and stored_sl > entry * 2.0:
                logger.warning(
                    "[MCX-OPT SL GUARD] %s stop_loss=%.2f is %.1fx entry=%.2f — "
                    "looks like underlying futures price, resetting SL to 50%% of entry",
                    trade.get("tradingsymbol"), stored_sl, stored_sl / entry, entry,
                )
                corrected_sl = round(entry * 0.50, 2)
                trade["sl_price"] = corrected_sl
                # Persist the corrected SL to DB so it survives restart
                try:
                    from master_resource import MasterResource
                    import sqlite3 as _sql
                    with _sql.connect(MasterResource.get_trading_db_path(), timeout=5) as _c:
                        _c.execute(
                            "UPDATE orders SET stop_loss=%s WHERE order_id=%s",
                            (corrected_sl, order_id),
                        )
                except Exception as _e:
                    logger.warning("[MCX-OPT SL GUARD] DB update failed: %s", _e)

            candle_ltp = self._get_mcx_candle_ltp(trade.get("tradingsymbol", ""))
            if candle_ltp is not None:
                ltp = candle_ltp   # intraday confirmed price — proceed normally
            else:
                # Sweeper hasn't populated data yet (option not traded intraday).
                # Guard: don't trail SL on Kite LTP that may be stale from prev session.
                # Still allow hard-SL check against the (now corrected) signal SL.
                if entry > 0 and ltp > entry * 2.5:
                    logger.warning(
                        "[MCX-OPT STALE] %s LTP=%.2f is %.1fx entry=%.2f — "
                        "no intraday candle yet, skipping cycle to avoid phantom SL trail",
                        trade.get("tradingsymbol"), ltp, ltp / entry, entry,
                    )
                    return   # skip entirely; sweeper will populate data next minute
                # LTP within 2.5x entry — still conservative: check hard SL only, no trail
                logger.debug(
                    "[MCX-OPT] %s: no intraday candle yet, holding SL trail "
                    "(Kite LTP=%.2f, checking hard exit only)",
                    trade.get("tradingsymbol"), ltp,
                )
                sl   = trade["sl_price"]
                sl_hit = (action == "BUY" and ltp <= sl) or (action == "SELL" and ltp >= sl)
                if sl_hit:
                    self._execute_exit(order_id, trade, sl, reason=f"SL_{trade['stage']}")
                return   # no peak update, no trailing advancement until candles arrive

        # --- 1.5. Price sanity guard — reject feed spikes / corrupt data ----
        # Option premiums must be ≥ ₹0.05 (tick minimum) and < ₹50,000.
        # Spot-level ranges for future reference (for underlying data checks):
        #   NIFTY: 15000-30000 | BANKNIFTY: 40000-70000 | FINNIFTY: 15000-35000
        #   SENSEX: 60000-95000 | MIDCPNIFTY: 8000-16000
        if not (0.05 <= ltp <= 50000.0):
            logger.error(
                "[PRICE SANITY] %s LTP=%.2f out of valid range [0.05, 50000] "
                "— feed spike, skipping cycle", trade.get("symbol", "?"), ltp
            )
            return

        # --- 2. Update peak price and rolling LTP on every tick --------
        if action == "BUY":
            trade["peak_price"] = max(trade["peak_price"], ltp)
        else:
            trade["peak_price"] = min(trade["peak_price"], ltp)

        trade["last_ltp_for_sl"] = ltp   # Change 1: track latest LTP as candle close candidate

        # --- 2.5: Direction + volume signal (non-blocking, best-effort) -
        try:
            self._update_price_signal(order_id, trade, ltp)
        except Exception as _e:
            logger.debug(f"price_signal error [{trade.get('symbol')}]: {_e}")

        # --- 3. ATR v3 trailing SL — update on every tick --------------
        current_sl = self._update_trailing_sl(trade, ltp, current_sl)
        trade["sl_price"] = current_sl

        # --- 4. Persist LTP + new SL + stage + peak to DB --------------
        self._update_db_ltp(order_id, ltp, current_sl, trade["stage"], trade["peak_price"])

        # --- 5. Change 2: skip SL check during minimum hold period -----
        now_naive = datetime.now()
        if now_naive < trade.get("hold_until", now_naive):
            logger.debug(f"{trade['symbol']}: min-hold active until {trade['hold_until'].strftime('%H:%M:%S')}")
            return

        # --- 6. Change 1: SL check only on candle close (minute boundary) ---
        current_minute = datetime.now(IST).minute
        if current_minute == trade["last_sl_minute"]:
            return   # still inside the same 1-min candle — don't check SL yet

        # New minute → use the last recorded LTP as the candle close price
        close_ltp = trade["last_ltp_for_sl"]
        trade["last_sl_minute"] = current_minute

        # --- 6.5: ORB invalidation — underlying-level thesis check for ORB_VWAP ---
        if trade.get("strategy_name") == "ORB_VWAP":
            self._check_orb_invalidation(order_id, trade)
            if order_id not in self.active_trades:
                return

        # --- 7. Time-based SL: exit stagnant trade after N minutes -----
        if self.sl_config.get("time_sl_enabled", True):
            self._check_time_sl(order_id, trade, close_ltp)
            if order_id not in self.active_trades:
                return

        # --- 8. Check SL hit on candle close ---------------------------
        sl_hit = (action == "BUY"  and close_ltp <= current_sl) or \
                 (action == "SELL" and close_ltp >= current_sl)

        if sl_hit:
            # Use SL price as exit price, not the polled LTP.
            # In live trading a real SL-M order executes at/near the SL level;
            # using close_ltp would simulate the gap-blow that only happens when
            # the price crashes past SL between 60-second polls.
            sl_exit_price = current_sl
            logger.warning(
                f"SL HIT (candle close): {trade['symbol']} | "
                f"close={close_ltp:.2f} | SL={current_sl:.2f} | "
                f"exit@SL={sl_exit_price:.2f} | stage={trade['stage']}"
            )
            self._execute_exit(order_id, trade, sl_exit_price, reason=f"SL_{trade['stage']}")

    # ------------------------------------------------------------------
    # Direction + volume signal
    # ------------------------------------------------------------------

    def _update_price_signal(self, order_id: str, trade: dict, ltp: float):
        """
        Build a real-time direction + volume signal for the open trade.

        Direction: derived from synthetic 1-min bar closes (built from LTP ticks)
          ALIGNED   — last bars moving WITH the trade (CE + price rising, etc.)
          AGAINST   — last bars moving AGAINST the trade
          NEUTRAL   — mixed / early

        Volume: underlying index last-3-candle volume trend from kite_candles.db
          e.g. "NIFTY DN VOL↑" means index is falling with rising volume (bad for CE)
        """
        # Rolling LTP buffer (last 12 readings ≈ 60s)
        buf = trade["_ltp_buf"]
        buf.append(ltp)
        if len(buf) > 12:
            buf[:] = buf[-12:]

        # Build 1-min synthetic bar closes at each minute boundary
        cur_min = datetime.now(IST).minute
        if cur_min != trade["_sig_last_min"] and trade["_sig_last_min"] != -1:
            closes = trade["_bar_closes"]
            closes.append(trade["last_ltp_for_sl"])   # use last LTP as bar close
            if len(closes) > 6:
                closes[:] = closes[-6:]
        trade["_sig_last_min"] = cur_min

        # Choose data points: bar closes preferred (cleaner), else raw LTP ticks
        pts = trade["_bar_closes"][-4:] if len(trade["_bar_closes"]) >= 2 else buf[-8:]
        action = (trade["action"] or "BUY").upper()

        if len(pts) < 2:
            sig_dir, sig_bars = "WAIT", 0
        else:
            # Tick-by-tick moves: +1 up, -1 down, 0 flat (0.15% threshold)
            moves = []
            for i in range(1, len(pts)):
                chg = (pts[i] - pts[i-1]) / pts[i-1] if pts[i-1] else 0
                moves.append(1 if chg > 0.0015 else (-1 if chg < -0.0015 else 0))

            net      = sum(moves)
            last_dir = moves[-1] if moves else 0

            if action == "BUY":
                if last_dir < 0 and net < 0:
                    sig_dir, sig_bars = "AGAINST", abs(net)
                elif last_dir > 0 and net > 0:
                    sig_dir, sig_bars = "ALIGNED", net
                else:
                    sig_dir, sig_bars = "NEUTRAL", 0
            else:  # SELL
                if last_dir > 0 and net > 0:
                    sig_dir, sig_bars = "AGAINST", net
                elif last_dir < 0 and net < 0:
                    sig_dir, sig_bars = "ALIGNED", abs(net)
                else:
                    sig_dir, sig_bars = "NEUTRAL", 0

        # Index volume signal
        vol_sig = self._get_idx_vol_signal(trade["symbol"])

        signal = json.dumps({
            "dir":  sig_dir,
            "bars": sig_bars,
            "vol":  vol_sig,
            "n":    len(pts),
        }, separators=(',', ':'))

        if signal == trade["_last_price_signal"]:
            return   # unchanged — skip DB write

        trade["_last_price_signal"] = signal
        try:
            conn = _get_conn()
            conn.execute("UPDATE orders SET price_signal=? WHERE order_id=?",
                         (signal, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"price_signal DB write error: {e}")

    def _resolve_kite_token(self, tradingsymbol: str) -> int | None:
        """Parse tradingsymbol 'SENSEX-Apr2026-77700-PE' and return Kite instrument token.
        Also handles Kite-native format (e.g. 'NATURALGAS26MAY280CE') for MCX positions."""
        if not _KITE_LTP_ENABLED or not self._kite:
            return None

        # Kite-native format (no dashes): look up directly in MCX instruments
        if '-' not in tradingsymbol:
            try:
                import kite_candle_store as _kcs
                df = _kcs._kite_instruments("MCX")
                if df is not None and not df.empty:
                    row = df[df["tradingsymbol"] == tradingsymbol]
                    if not row.empty:
                        return int(row.iloc[0]["instrument_token"])
            except Exception:
                pass
            return None

        parts = tradingsymbol.split('-')
        if len(parts) < 4:
            return None
        base, month_str, strike_str, opt_type = parts[0], parts[1], parts[2], parts[3]

        # 1. Fast path: look up expiry from candle DB (works for historical strikes)
        try:
            conn = sqlite3.connect(_KITE_DB, timeout=5)
            row = conn.execute(
                "SELECT tradingsymbol FROM candles_1min "
                "WHERE tradingsymbol LIKE ? ORDER BY dt DESC LIMIT 1",
                (f'{base}_{strike_str}_{opt_type}_%',)
            ).fetchone()
            conn.close()
            if row:
                expiry = row[0].rsplit('_', 1)[-1]
                tok = _resolve_opt_token(base, int(float(strike_str)), opt_type, expiry)
                if tok:
                    return tok
        except Exception:
            pass

        # 2. Fallback: filter Kite instruments by month+year (no day-of-week assumption).
        # Different indices have different expiry days (NIFTY=Thu, BANKNIFTY=Tue, etc.)
        # so we do not hardcode the weekday — instead we search the full instruments cache.
        try:
            import kite_candle_store as _kcs
            _MON_MAP = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                        'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}
            mon = _MON_MAP.get(month_str[:3])
            yr  = int(month_str[3:]) if len(month_str) > 3 else None
            if not mon or not yr:
                return None
            strike  = int(float(strike_str))
            exchange = "BFO" if base in {"SENSEX", "BANKEX", "SENSEX50"} else "NFO"
            df = _kcs._kite_instruments(exchange)
            if df is None or df.empty:
                return None
            hits = df[
                (df["name"]             == base) &
                (df["strike"]           == float(strike)) &
                (df["instrument_type"]  == opt_type) &
                (df["expiry"].apply(lambda d: d.month == mon and d.year == yr))
            ]
            if hits.empty:
                logger.debug(f"No Kite instrument found for {tradingsymbol} in {exchange}")
                return None
            # Multiple hits when both weekly + monthly expiry exist for the same
            # month/year (e.g. SENSEX May-21 weekly AND May-27 monthly).
            # Pick the nearest expiry that hasn't already passed.
            hits = hits.sort_values("expiry")
            from datetime import date as _date_cls
            today_date = _date_cls.today()
            future = hits[hits["expiry"].apply(lambda d: d >= today_date)]
            best = future.iloc[0] if not future.empty else hits.iloc[-1]
            tok = int(best["instrument_token"])
            logger.info(f"Kite token {tok} resolved via instruments for "
                        f"{tradingsymbol} (expiry={best['expiry']})")
            return tok
        except Exception as e:
            logger.debug(f"Kite token month-lookup failed for {tradingsymbol}: {e}")

        return None

    def _batch_kite_ltp_update(self):
        """One Kite API call to refresh LTPs for all active trades. Called once per poll cycle."""
        if not self._kite:
            return

        # Retry token resolution for positions that didn't get a token at load time.
        # This handles ATM strikes placed today whose candle data wasn't in the DB yet.
        for oid, t in self.active_trades.items():
            if t.get("_kite_token") is None and t.get("tradingsymbol"):
                # MCX-OPT positions store the instrument_token as security_id directly
                if t.get("exchange_segment") == "MCX-OPT":
                    try:
                        t["_kite_token"] = int(t["security_id"])
                    except (TypeError, ValueError):
                        pass
                else:
                    tok = self._resolve_kite_token(t["tradingsymbol"])
                    if tok:
                        t["_kite_token"] = tok

        token_map = {oid: t["_kite_token"] for oid, t in self.active_trades.items()
                     if t.get("_kite_token")}
        if not token_map:
            logger.debug("Kite LTP: no tokens resolved — skipping batch fetch")
            return
        try:
            resp = self._kite.ltp([str(tok) for tok in token_map.values()])
            for oid, tok in token_map.items():
                entry = resp.get(str(tok))
                if entry:
                    price = float(entry["last_price"])
                    if price > 0:   # never cache a zero-tick — treat as missing data
                        self._kite_ltp[oid] = price
            logger.debug(f"Kite LTP fetched for {len(resp)} instruments")
        except Exception as e:
            logger.debug(f"Kite batch LTP error: {e}")

    def _get_candle_ltp(self, tradingsymbol: str) -> float | None:
        """Use latest 1-min candle close as LTP proxy.
        Routes MCX commodity symbols to mcx_candles.db.
        Maps 'NIFTY-Apr2026-24350-PE' → LIKE 'NIFTY_24350_PE%' in kite_candles.db.
        """
        parts = tradingsymbol.split('-')
        if len(parts) < 4:
            return self._get_mcx_candle_ltp(tradingsymbol)
        base, _, strike, opt_type = parts[0], parts[1], parts[2], parts[3]
        # MCX commodity bases must use mcx_candles.db, not kite_candles.db
        if base.upper() in _MCX_BASES:
            return self._get_mcx_candle_ltp(tradingsymbol)
        try:
            conn = sqlite3.connect(_KITE_DB, timeout=5)
            row = conn.execute(
                "SELECT close FROM candles_1min WHERE tradingsymbol LIKE ? AND date(dt)=? ORDER BY dt DESC LIMIT 1",
                (f'{base}_{strike}_{opt_type}%', date.today().isoformat())
            ).fetchone()
            conn.close()
            val = float(row[0]) if row else 0.0
            return val if val > 0 else None
        except Exception:
            return None

    def _get_mcx_candle_ltp(self, tradingsymbol: str) -> float | None:
        """Latest 1-min close for an MCX commodity option/future from mcx_candles.db.

        Priority:
          1. Exact tradingsymbol match — option candle stored by MCXOptionCandleSweep
             (e.g. 'COPPER26JUN1300PE' stored directly as the symbol key)
          2. Mapped futures symbol fallback — e.g. 'CRUDEOILM' underlying price
        """
        today = date.today().isoformat()
        try:
            conn = sqlite3.connect(_MCX_DB, timeout=5)
            # 1. Exact option candle
            row = conn.execute(
                "SELECT close FROM mcx_candles_1min WHERE symbol=? AND date(dt)=? ORDER BY dt DESC LIMIT 1",
                (tradingsymbol, today)
            ).fetchone()
            if row and row[0]:
                conn.close()
                val = float(row[0])
                return val if val > 0 else None
            # 2. Futures mapped symbol
            sym = _mcx_candle_sym(tradingsymbol)
            if sym:
                row = conn.execute(
                    "SELECT close FROM mcx_candles_1min WHERE symbol=? AND date(dt)=? ORDER BY dt DESC LIMIT 1",
                    (sym, today)
                ).fetchone()
            conn.close()
            val = float(row[0]) if row and row[0] else 0.0
            return val if val > 0 else None
        except Exception:
            return None

    def _get_idx_vol_signal(self, symbol: str) -> str:
        """
        Read the last 4 1-min candles for the underlying index from kite_candles.db.
        Returns index price momentum, e.g. "NIFTY ↓↓ -0.08%" or "SENSEX ↑ +0.03%".
        Index candles have volume=0 in Kite so we use price momentum instead of volume.
        """
        base = next((b for b in _INDEX_BASES if symbol.upper().startswith(b)), None)
        if not base:
            return "N/A"
        try:
            today = date.today().isoformat()
            conn  = sqlite3.connect(_KITE_DB, timeout=5)
            rows  = conn.execute("""
                SELECT close FROM candles_1min
                WHERE tradingsymbol=? AND date(dt)=?
                ORDER BY dt DESC LIMIT 4
            """, (base, today)).fetchall()
            conn.close()
            if len(rows) < 2:
                return "N/A"
            rows   = list(reversed(rows))          # oldest first
            closes = [r[0] for r in rows]
            # Direction of each 1-min move (0.05% threshold)
            moves = []
            for i in range(1, len(closes)):
                pct = (closes[i] - closes[i-1]) / closes[i-1] if closes[i-1] else 0
                moves.append(1 if pct > 0.0005 else (-1 if pct < -0.0005 else 0))
            net = sum(moves)
            n   = len(moves)
            if net >= n:          arrow = "↑" * n
            elif net <= -n:       arrow = "↓" * n
            elif net > 0:         arrow = "↑"
            elif net < 0:         arrow = "↓"
            else:                 arrow = "~"
            total_pct = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0
            return f"{base} {arrow} {total_pct:+.2f}%"
        except Exception:
            return "N/A"

    def _get_underlying_ltp(self, symbol: str) -> float | None:
        """Latest 1-min close for the index underlying from kite_candles.db."""
        base = next((b for b in _INDEX_BASES if symbol.upper().startswith(b)), None)
        if not base:
            return None
        try:
            today = date.today().isoformat()
            conn  = sqlite3.connect(_KITE_DB, timeout=3)
            row   = conn.execute(
                "SELECT close FROM candles_1min "
                "WHERE tradingsymbol=? AND date(dt)=? ORDER BY dt DESC LIMIT 1",
                (base, today),
            ).fetchone()
            conn.close()
            return float(row[0]) if row else None
        except Exception:
            return None

    def _check_orb_invalidation(self, order_id: str, trade: dict):
        """
        Exit an ORB_VWAP position if the underlying goes back inside the ORB range.

        For a CE (BULLISH entry): underlying falling back below orb_high means the
        breakout failed — the market reclaimed the range and the thesis is invalid.
        For a PE (BEARISH entry): underlying rising back above orb_low = same logic.

        This check runs on every new candle close, after the hold period expires,
        so it won't fire during the opening IV-crush window.
        """
        orb_high = trade.get("orb_high")
        orb_low  = trade.get("orb_low")
        if orb_high is None or orb_low is None:
            return

        underlying = self._get_underlying_ltp(trade["symbol"])
        if underlying is None:
            return

        tsym        = trade.get("tradingsymbol", "")
        is_ce       = tsym.upper().endswith("-CE") or "_CE_" in tsym.upper()
        is_pe       = tsym.upper().endswith("-PE") or "_PE_" in tsym.upper()
        invalidated = False

        if is_ce and underlying < orb_high:
            logger.warning(
                "[ORB INVALID] %s: underlying %.0f fell back below orb_high %.0f "
                "— CE breakout failed, exiting", trade["symbol"], underlying, orb_high
            )
            invalidated = True
        elif is_pe and underlying > orb_low:
            logger.warning(
                "[ORB INVALID] %s: underlying %.0f rose back above orb_low %.0f "
                "— PE breakdown failed, exiting", trade["symbol"], underlying, orb_low
            )
            invalidated = True

        if invalidated:
            exit_price = (self._get_candle_ltp(tsym)
                          or self._kite_ltp.get(order_id)
                          or trade["sl_price"])
            self._execute_exit(order_id, trade, exit_price, reason="ORB_INVALID")

    def _check_time_sl(self, order_id: str, trade: dict, ltp: float):
        """Exit a trade that hasn't moved enough in the configured time window.

        Config keys used:
          time_sl_minutes      — window in minutes (default 15)
          time_sl_min_move_pct — minimum % gain required (default 1.0)

        If the trade is older than the window AND gain < min_move_pct, exit.
        """
        minutes   = self.sl_config.get("time_sl_minutes",      15)
        min_move  = self.sl_config.get("time_sl_min_move_pct",  1.0) / 100
        entry     = trade["entry_price"]
        action    = trade["action"]
        opened_at = trade.get("opened_at", datetime.now())

        elapsed_min = (datetime.now() - opened_at.replace(tzinfo=None)).total_seconds() / 60
        if elapsed_min < minutes:
            return   # still within the patience window

        if action == "BUY":
            gain_pct = (ltp - entry) / entry if entry else 0
        else:
            gain_pct = (entry - ltp) / entry if entry else 0

        if gain_pct < min_move:
            logger.warning(
                f"TIME SL: {trade['symbol']} | elapsed={elapsed_min:.1f}min | "
                f"gain={gain_pct*100:.2f}% < {min_move*100:.1f}% threshold — exiting"
            )
            self._execute_exit(order_id, trade, ltp, reason="TIME_SL")

    def _compute_option_atr(self, tradingsymbol: str) -> float | None:
        """
        Compute ATR from today's 1-min candles for this option in kite_candles.db.
        tradingsymbol is Dhan format: 'NIFTY-May2026-23700-CE'.
        Returns None when fewer than 3 candles are available (pre-market, no data).
        """
        parts = tradingsymbol.split('-')
        if len(parts) < 4:
            return None
        base, _, strike, opt_type = parts[0], parts[1], parts[2], parts[3]
        n = self.sl_config.get("atr_period", 14)
        try:
            conn = sqlite3.connect(_KITE_DB, timeout=3)
            rows = conn.execute("""
                SELECT high, low, close FROM candles_1min
                WHERE tradingsymbol LIKE ? AND date(dt)=?
                ORDER BY dt DESC LIMIT ?
            """, (f'{base}_{strike}_{opt_type}%', date.today().isoformat(), n + 1)).fetchall()
            conn.close()
            if len(rows) < 3:
                return None
            rows = list(reversed(rows))   # oldest first
            trs = []
            for i in range(1, len(rows)):
                high, low, _ = rows[i]
                prev_close   = rows[i - 1][2]
                tr = max(high - low,
                         abs(high - prev_close),
                         abs(low  - prev_close))
                trs.append(tr)
            return round(sum(trs) / len(trs), 2) if trs else None
        except Exception as e:
            logger.debug(f"[ATR] Error for {tradingsymbol}: {e}")
            return None

    def _update_trailing_sl(self, trade: dict, ltp: float, current_sl: float) -> float:
        """
        Delegate SL state-machine to sl_engine.step_sl() — shared with WhatIf backtest.

        step_sl() is a pure function; this method owns ATR fetching, logging,
        and mutating trade["stage"].
        """
        atr  = self._compute_option_atr(trade.get("tradingsymbol", ""))
        hour = datetime.now(IST).hour

        new_sl, stage_name = step_sl(
            trade["entry_price"],
            trade["action"],
            trade["peak_price"],
            current_sl,
            atr,
            self.sl_config,
            hour,
        )

        if stage_name != trade.get("stage", "INITIAL"):
            peak = trade["peak_price"]
            if atr:
                logger.info(
                    f"{trade['symbol']}: {stage_name} SL={new_sl:.2f} "
                    f"(peak={peak:.2f}, LTP={ltp:.2f}, ATR={atr:.2f})"
                )
            else:
                logger.info(
                    f"{trade['symbol']}: {stage_name} SL={new_sl:.2f} "
                    f"(peak={peak:.2f}, LTP={ltp:.2f}, pct-fallback)"
                )
            trade["stage"] = stage_name

        return new_sl

    def _update_db_ltp(self, order_id: str, ltp: float, sl: float,
                       stage: str = "INITIAL", peak: float = 0.0):
        try:
            conn = _get_conn()
            conn.execute(
                "UPDATE orders SET ltp=?, stop_loss=?, sl_stage=?, peak_price=?, updated_at=? WHERE order_id=?",
                (ltp, sl, stage, peak, datetime.now().isoformat(), order_id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB LTP update error: {e}")

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def _execute_exit(self, order_id: str, trade: dict, exit_price: float, reason: str = "SL"):
        entry  = trade["entry_price"]
        action = (trade["action"] or "BUY").upper()
        qty    = trade["quantity"]
        symbol = trade["symbol"]

        pnl = (exit_price - entry) * qty if action == "BUY" else (entry - exit_price) * qty

        logger.info(
            f"EXIT [{reason}]: {symbol} | order={order_id} | "
            f"entry={entry:.2f} → exit={exit_price:.2f} | PnL={pnl:.2f}"
        )

        try:
            conn = _get_conn()
            conn.execute(
                """UPDATE orders
                   SET status='CLOSED', exit_price=?, pnl=?, updated_at=?
                   WHERE order_id=?""",
                (exit_price, pnl, datetime.now().isoformat(), order_id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB exit update error: {e}")

        # SANDBOX_RBS_*_S orders are paper-only short SELL legs (Dhan API rejected
        # the real SELL; the engine fell back to a paper record).  The fill never
        # happened, so their P&L must not count toward the daily loss/profit state.
        is_phantom_sell = (
            (order_id.startswith("SANDBOX_RBS_") or order_id.startswith("SANDBOX_OMNI_")) and
            order_id.endswith("_S") and
            action == "SELL"
        )
        if is_phantom_sell:
            logger.warning(
                "[PHANTOM SELL] %s paper SELL leg — P&L %.2f excluded from daily state",
                order_id, pnl,
            )
        else:
            _update_daily_pnl_state(pnl)

        try:
            from rag.feedback_writer import write_trade_outcome
            write_trade_outcome(order_id, trade, exit_price, pnl, reason)
        except Exception as _fb_err:
            logger.debug(f"feedback_writer skipped: {_fb_err}")

        # SL exit blacklist — block ALL strategies from re-entering this strike today.
        # Reasons that indicate a genuine stop-loss hit (not CUTOFF/TIME_SL/EOD):
        _SL_REASONS = {"INITIAL_SL", "TRAILING_SL", "SL_INITIAL", "SL_BREAKEVEN",
                       "SL_PROFIT_LOCK", "SL_ATR_TRAILING", "ORB_INVALID"}
        _is_sl_exit = (reason in _SL_REASONS
                       or reason.startswith("SL_")
                       or reason.upper() in ("SL", "STOP_LOSS"))
        if _is_sl_exit:
            _ts = trade.get("tradingsymbol", "")
            if _ts:
                _write_sl_exit(_ts)

        self.ltp_feed.unsubscribe(trade["security_id"])
        self.active_trades.pop(order_id, None)
        self._kite_ltp.pop(order_id, None)

    # ------------------------------------------------------------------
    # LTP resolution (shared by SL checks and hard cutoff)
    # ------------------------------------------------------------------

    def _resolve_exit_ltp(self, oid: str, trade: dict) -> float | None:
        """Return the best available LTP > 0 across all sources, or None.

        Priority:
          1. Kite batch cache (covers BSE_FNO/BFO reliably — Dhan WS returns 0 there)
          2. Dhan WebSocket
          3. Dhan REST
          4. Candle DB 1-min close (slightly delayed)
        Never returns 0.0 — callers treat None as "price unavailable".
        """
        kite_price = self._kite_ltp.get(oid)
        if kite_price and kite_price > 0:
            return kite_price

        ws = self.ltp_feed.get_ltp(trade["security_id"])
        if ws and ws > 0:
            return ws

        rest = self.ltp_feed.get_ltp_rest(
            self.client, trade["security_id"], trade["exchange_segment"]
        )
        if rest and rest > 0:
            return rest

        candle = self._get_candle_ltp(trade.get("tradingsymbol", ""))
        if candle and candle > 0:
            return candle

        return None

    # ------------------------------------------------------------------
    # Hard cutoff: 15:25 IST for equity/index, 23:30 IST for MCX commodities
    # ------------------------------------------------------------------

    def _check_hard_cutoff(self):
        now_ist          = datetime.now(IST)
        now_hhmm         = now_ist.strftime("%H:%M")
        equity_cutoff    = self.sl_config.get("hard_cutoff_time",      "15:25")
        commodity_cutoff = self.sl_config.get("commodity_cutoff_time", "23:30")
        commodity_syms   = {s.upper() for s in self.sl_config.get("commodities", [])}

        to_exit = []
        for oid, trade in self.active_trades.items():
            # LONGTERM and BTST positions are held overnight — never force-exit at cutoff
            if trade.get("position_type") in ("LONGTERM", "BTST") or trade.get("btst_flag"):
                continue
            seg    = (trade.get("exchange_segment") or "").upper()
            symbol = (trade.get("symbol") or "").upper()
            is_mcx = seg.startswith("MCX") or any(symbol.startswith(c) for c in commodity_syms)
            cutoff = commodity_cutoff if is_mcx else equity_cutoff
            if now_hhmm >= cutoff:
                to_exit.append((oid, trade, cutoff))

        if to_exit:
            logger.warning(
                f"Hard cutoff reached — squaring off {len(to_exit)} position(s)"
            )
        for oid, trade, cutoff in to_exit:
            ltp = self._resolve_exit_ltp(oid, trade)
            if ltp is None:
                # All sources returned 0/None — never use entry_price (produces fake pnl=0).
                # Use last tracked LTP from the monitoring loop as the least-bad option.
                last_known = trade.get("last_ltp_for_sl", 0)
                if last_known and last_known > 0 and last_known != trade["entry_price"]:
                    ltp = last_known
                    logger.error(
                        f"CUTOFF [{oid}]: all LTP sources returned 0 for "
                        f"{trade['symbol']} — using last-known LTP {ltp:.2f} "
                        f"(Kite token resolved: {bool(trade.get('_kite_token'))})"
                    )
                else:
                    logger.critical(
                        f"CUTOFF [{oid}]: no valid LTP for {trade['symbol']} — "
                        f"position left OPEN; check Kite token resolution and "
                        f"recheck tomorrow's open orders"
                    )
                    continue   # do NOT exit at entry_price — pnl=0 is misleading
            self._execute_exit(oid, trade, ltp, reason="CUTOFF")


def _heartbeat_thread(monitor_ref, interval=90):
    """Background thread: logs heartbeat every `interval` seconds regardless of main loop blocking."""
    import threading
    while True:
        time.sleep(interval)
        try:
            logger.info(f"[HEARTBEAT] SL Monitor alive — tracking {len(monitor_ref.active_trades)} position(s)")
        except Exception:
            pass


if __name__ == "__main__":
    import threading
    monitor = DhanSLMonitor(is_sandbox=True)
    hb = threading.Thread(target=_heartbeat_thread, args=(monitor,), daemon=True, name="SLMonitorHB")
    hb.start()
    monitor.monitor_positions()
