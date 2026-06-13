import pandas as pd
import numpy as np
import time
import json
import sqlite3
import pytz
from pathlib import Path
from datetime import datetime, timedelta, timezone, time as dtime, date
from core.dhan_client import DhanClient

IST          = pytz.timezone("Asia/Kolkata")
_IST_TZ      = timezone(timedelta(hours=5, minutes=30))   # stdlib offset, pytz-independent
MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 25)

def _is_market_open() -> bool:
    now_ist = datetime.now(timezone.utc).astimezone(_IST_TZ)
    if now_ist.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE

# NSE Live Feed — used as data source when Dhan sandbox has no intraday data
try:
    from nse_live_feed import NSELiveFeed
    _NSE_FEED_AVAILABLE = True
except ImportError:
    _NSE_FEED_AVAILABLE = False

from core.order_placer import OrderPlacer
from core.strike_lookup import StrikeLookup
from master_resource import MasterResource
from db_utils import fetch_all as _db_fetch_all

# Persistent candle store — Kite data with SQLite fallback
import sys as _sys
_sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent /
                        'MasterConfiguration' / 'lib'))
try:
    from kite_candle_store import (
        get_candles    as _cs_get,
        ensure_tables  as _cs_ensure,
        save_to_db     as _cs_save,
        INDEX_TOKENS   as _CS_INDEX_TOKENS,
    )
    _CANDLE_STORE_OK = True
except ImportError:
    _CANDLE_STORE_OK = False

try:
    from dhan_candle_store import seed_engine as _dhan_seed, save_live_candle as _dhan_save_candle
    _DHAN_STORE_OK = True
except ImportError:
    _DHAN_STORE_OK = False

# Import Strategy Classes
from strategies.ema_9_21 import EMA921Strategy
from strategies.pair_leadership import PairLeadershipStrategy
from strategies.option_scalper import OptionScalperStrategy
from strategies.supertrend_macd import SupertrendMACDStrategy
from strategies.advanced_ema_orb import AdvancedEMAORBStrategy
from strategies.index_momentum import IndexMomentumStrategy
from strategies.triple_pattern import TriplePatternStrategy
from strategies.bollinger_mean_reversion import BollingerMeanReversionStrategy
from strategies.vwap_reclaim import VWAPReclaimStrategy
from strategies.cpr_breakout import CPRBreakoutStrategy
from strategies.indicators import adx as _regime_adx

# ---------------------------------------------------------------------------
# Instrument Registry
# Security IDs confirmed against Dhan scrip master (IDX_I segment for indices)
# ---------------------------------------------------------------------------
INSTRUMENT_REGISTRY = {
    "NIFTY":     {"security_id": "13",   "segment": "IDX_I",  "instrument": "INDEX"},
    "BANKNIFTY": {"security_id": "25",   "segment": "IDX_I",  "instrument": "INDEX"},
    "FINNIFTY":  {"security_id": "27",   "segment": "IDX_I",  "instrument": "INDEX"},
    "SENSEX":    {"security_id": "51",   "segment": "IDX_I",  "instrument": "INDEX"},  # BSE Sensex
    "RELIANCE":  {"security_id": "2885", "segment": "NSE_EQ", "instrument": "EQUITY"},
    "HDFC":      {"security_id": "1333", "segment": "NSE_EQ", "instrument": "EQUITY"},
}

# How many past trading days to request (Dhan allows up to 5)
FETCH_DAYS_5M  = 2
FETCH_DAYS_15M = 5

# Warmup: 30 × 1-min candles = 30 minutes before strategies fire
MIN_CANDLES    = 30    # 1-min candles (primary gate)
MIN_CANDLES_5M = 6     # 5-min candles for MTF confirmation (available after ~30 min)

# ADX threshold above which execute() places a debit spread instead of a naked long
_SPREAD_ADX_MIN = 25.0   # ADX >= 25 → debit spread instead of single-leg
_RBS_ADX_MIN    = 30.0   # ADX >= 30 → ratio back spread (takes priority over debit spread)

def _is_paper_order(order_id: str) -> bool:
    """True for any locally-generated paper order (PAPER_* or legacy SANDBOX_*).
    Used to decide whether to use the signal LTP as fill price vs fetch from Dhan."""
    return order_id.startswith(("PAPER_", "SANDBOX_"))

# Re-fetch 15m candles at most once every 15 minutes to avoid redundant calls
CACHE_TTL_15M = 15 * 60  # seconds

# ── SL exit blacklist (cross-strategy, intraday) ──────────────────────────────

def _is_sl_blacklisted(tradingsymbol: str) -> bool:
    """
    Return True when ANY strategy took a stop-loss hit on this tradingsymbol today.
    Written by dhan_sl_monitor._write_sl_exit() into the sl_exits DB table.
    Prevents whipsaw cross-strategy re-entries on an option that already proved lossy.
    Uses db_utils.fetch_all so it always reads from MySQL (via bridge) regardless of
    whether mysql_sqlite_bridge was imported by the caller.
    """
    if not tradingsymbol:
        return False
    try:
        rows = _db_fetch_all(
            MasterResource.get_trading_db_path(),
            "SELECT 1 FROM sl_exits WHERE tradingsymbol=? AND exit_date=? LIMIT 1",
            (tradingsymbol, date.today().isoformat()),
        )
        return bool(rows)
    except Exception:
        return False

# Kite instrument tokens for index spot prices (used for historical seeding)
KITE_INDEX_TOKENS = {
    "NIFTY":     256265,   # NSE:NIFTY 50
    "BANKNIFTY": 260105,   # NSE:NIFTY BANK
    "FINNIFTY":  257801,   # NSE:NIFTY FIN SERVICE
    "MIDCPNIFTY":288009,   # NSE:NIFTY MID SELECT
    "SENSEX":    265,      # BSE:SENSEX
    "BANKEX":    274441,   # BSE:BANKEX
}

# Kite tokens for equity leaders used by PairLeadership strategy
KITE_EQUITY_TOKENS = {
    "RELIANCE": (738561, "NSE"),   # NSE:RELIANCE
    "HDFC":     (341249, "NSE"),   # NSE:HDFCBANK (post-merger symbol)
}


def _merge_candles(existing: pd.DataFrame, new: pd.DataFrame, tail: int = 150) -> pd.DataFrame:
    """
    Merge new candles into existing history, preserving the seeded multi-day base.
    Deduplicates on timestamp, normalises to IST-naive, returns sorted tail.
    """
    if existing is None or existing.empty:
        return new.tail(tail).reset_index(drop=True)
    combined = pd.concat([existing, new], ignore_index=True)
    combined["timestamp"] = pd.to_datetime(combined["timestamp"])
    if combined["timestamp"].dt.tz is not None:
        combined["timestamp"] = (combined["timestamp"]
                                 .dt.tz_convert("Asia/Kolkata")
                                 .dt.tz_localize(None))
    return (combined
            .drop_duplicates(subset="timestamp")
            .sort_values("timestamp")
            .tail(tail)
            .reset_index(drop=True))


class DhanOmniEngine:
    def __init__(self, is_sandbox=True):
        self._is_sandbox = is_sandbox
        self.client = DhanClient(is_sandbox=is_sandbox)
        self.placer  = OrderPlacer(is_sandbox=is_sandbox)
        self.logger  = MasterResource.setup_shared_logger("dhan_omni_engine")
        _sl_cfg_path = MasterResource.MASTER_ROOT / "config" / "sl_config.json"
        _live_cap = 1
        if _sl_cfg_path.exists():
            try:
                _live_cap = int(json.loads(_sl_cfg_path.read_text()).get("max_algo_per_symbol", 1))
            except Exception:
                pass
        self.MAX_ALGO_PER_SYMBOL = 100 if is_sandbox else _live_cap

        # Strike lookup (downloads Dhan scrip master on first use)
        self.strike_lookup = StrikeLookup()

        # Initialize Strategies
        self.strat_ema_921  = EMA921Strategy()
        self.strat_pair     = PairLeadershipStrategy()
        self.strat_scalp    = OptionScalperStrategy()
        self.strat_st_macd  = SupertrendMACDStrategy()
        self.strat_adv      = AdvancedEMAORBStrategy()
        self.strat_momentum = IndexMomentumStrategy()   # pure index-momentum, no TG
        self.strat_triple   = TriplePatternStrategy()       # Triple Bottom / Triple Top reversal
        self.strat_bb             = BollingerMeanReversionStrategy()  # Mean-reversion for ranging markets
        self.strat_vwap_reclaim   = VWAPReclaimStrategy()            # VWAP reclaim / rejection / ±2σ band
        self.strat_cpr            = CPRBreakoutStrategy()            # CPR breakout (prev day H/L/C)
        self._cpr_cache: dict[str, dict] = {}                        # {index: {top, bot, date}}

        # NSE Live Feed — start if available (used when Dhan sandbox has no intraday data)
        self._nse_feed = None
        if _NSE_FEED_AVAILABLE:
            self._nse_feed = NSELiveFeed(symbols=["NIFTY", "BANKNIFTY", "FINNIFTY"])
            self._nse_feed.start()
            self.logger.info("NSE Live Feed started — using NSE prices for strategy signals")

        self.indices = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY"]

        # Rolling OHLCV buffers  {key: DataFrame}
        self.data: dict[str, pd.DataFrame] = {}
        for idx in self.indices:
            self.data[f"{idx}_1m"]  = pd.DataFrame()   # primary — warmup gate
            self.data[f"{idx}_5m"]  = pd.DataFrame()   # for MTF confirmation
            self.data[f"{idx}_15m"] = pd.DataFrame()
        self.data["RELIANCE"] = pd.DataFrame()
        self.data["HDFC"]     = pd.DataFrame()

        # Timestamp of last successful 15m fetch per index
        self._last_15m_fetch: dict[str, float] = {}

        # Signal-log throttle: tracks last DB-write time per (idx, strategy) for NEUTRAL rows
        self._sig_log_last: dict[tuple, float] = {}

        # Ensure orders table exists with all required columns
        self._ensure_orders_columns()
        self._ensure_strategy_signal_table()

        # Seed historical candles on startup from local DB (1-min candles → resample)
        if _DHAN_STORE_OK:
            seeded = _dhan_seed(self.data, indices=self.indices, min_candles=5)
            # Also populate 1m buffers from DB
            for idx in self.indices:
                if f"{idx}_5m" in self.data and not self.data[f"{idx}_5m"].empty:
                    pass  # 5m already seeded; 1m populated live by NSE feed
            if seeded:
                ready = [k for k, v in seeded.items() if v >= MIN_CANDLES]
                self.logger.info(f"[SEED] Dhan candle store seeded: {seeded}")
                self.logger.info(f"[SEED] Strategies ready immediately: {ready}")
            else:
                self.logger.warning("[SEED] Dhan candle store returned no data — warming from live ticks")
        else:
            self._kite = self._init_kite()
            if self._kite:
                self._seed_from_kite()

        self.logger.info("Dhan Omni Engine v2.0 - Real market data feed active.")

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _ensure_orders_columns(self):
        """Add missing columns to orders table if absent."""
        try:
            db_path = MasterResource.get_trading_db_path()
            conn    = sqlite3.connect(db_path)
            cur     = conn.cursor()
            cur.execute("PRAGMA table_info(orders)")
            existing = {row[1] for row in cur.fetchall()}
            for col, typ in [
                ("security_id",    "TEXT"),
                ("exchange_segment","TEXT"),
                ("strategy_name",  "TEXT"),
            ]:
                if col not in existing:
                    cur.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
                    self.logger.info(f"DB migration: added column orders.{col}")
            conn.commit()
            conn.close()
        except Exception as e:
            self.logger.warning(f"orders column migration skipped: {e}")

    def _ensure_strategy_signal_table(self):
        try:
            conn = sqlite3.connect(MasterResource.get_trading_db_path())
            conn.execute("""
                CREATE TABLE IF NOT EXISTS strategy_signals (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         TEXT    NOT NULL,
                    index_name TEXT    NOT NULL,
                    strategy   TEXT    NOT NULL,
                    signal     TEXT    NOT NULL,
                    spot       REAL,
                    candles_1m INTEGER,
                    candles_5m INTEGER,
                    indicators TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ss_ts ON strategy_signals(ts)")
            conn.commit()
            conn.close()
        except Exception as e:
            self.logger.warning(f"strategy_signals table init failed: {e}")

    def _log_strategy_eval(self, idx: str, strategy: str, signal: str,
                            spot: float, df_1m, df_5m, indicators: dict | None = None):
        """
        Persist one strategy evaluation row to strategy_signals.
        NEUTRAL rows are throttled to once per 5 minutes per (idx, strategy).
        Non-NEUTRAL rows are always written.
        """
        key = (idx, strategy)
        now = time.monotonic()
        if signal == "NEUTRAL":
            last = self._sig_log_last.get(key, 0)
            if now - last < 300:   # 5-minute throttle for NEUTRAL
                return
        self._sig_log_last[key] = now
        try:
            conn = sqlite3.connect(MasterResource.get_trading_db_path())
            conn.execute(
                """INSERT INTO strategy_signals
                       (ts, index_name, strategy, signal, spot, candles_1m, candles_5m, indicators)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(IST).isoformat(),
                    idx,
                    strategy,
                    signal,
                    round(spot, 2) if spot else None,
                    len(df_1m) if df_1m is not None else None,
                    len(df_5m) if df_5m is not None else None,
                    json.dumps(indicators) if indicators else None,
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            self.logger.debug(f"_log_strategy_eval write error: {e}")

    # Max concurrent algo positions per underlying (across ALL strategies).
    # Prevents multiple strategies stacking into the same instrument simultaneously.
    # Paper mode: 100 (let all strategies fire for analysis). Live: 1 (capital protection).
    # TG:SIGNAL is excluded — it gets its own independent slot.
    MAX_ALGO_PER_SYMBOL = 1   # overridden in __init__ based on is_sandbox

    # Max entries per strategy per symbol per calendar day.
    # Configurable via sl_config.json → max_entries_per_strategy_per_day (default 2).
    MAX_ENTRIES_PER_STRATEGY_PER_DAY = 2   # fallback if config unreadable

    # Burst rate limiter: max algo orders placed across ALL strategies per 5-minute window.
    # Prevents the 12:51-style evaluation-cycle burst from flooding the same instrument.
    # Live value from sl_config.json → max_orders_per_5min.
    MAX_ORDERS_PER_5MIN = 4   # fallback if config unreadable

    def _has_open_position(self, symbol: str, strategy_name: str = "") -> bool:
        """
        Return True if a new position for this symbol should be blocked.

        Two gates:
        1. Strategy-specific: this strategy already has an OPEN order here.
        2. Global algo cap: MAX_ALGO_PER_SYMBOL algo orders already OPEN for
           this symbol (TG:SIGNAL excluded — it has its own slot).
        """
        try:
            db_path = MasterResource.get_trading_db_path()
            conn    = sqlite3.connect(db_path)
            cur     = conn.cursor()

            # Gate 1 — strategy-specific slot
            if strategy_name:
                cur.execute(
                    "SELECT COUNT(*) FROM orders "
                    "WHERE symbol=? AND strategy_name=? AND status='OPEN'",
                    (symbol, strategy_name),
                )
                if cur.fetchone()[0] > 0:
                    conn.close()
                    return True

            # Gate 2 — global algo cap (skip for TG:SIGNAL, it's external)
            if strategy_name and strategy_name != "TG:SIGNAL":
                cur.execute(
                    "SELECT COUNT(*) FROM orders "
                    "WHERE symbol=? AND status='OPEN' AND strategy_name != 'TG:SIGNAL'",
                    (symbol,),
                )
                if cur.fetchone()[0] >= self.MAX_ALGO_PER_SYMBOL:
                    self.logger.info(
                        "[POS CAP] %s already has %d algo position(s) open — "
                        "%s blocked", symbol, self.MAX_ALGO_PER_SYMBOL, strategy_name
                    )
                    conn.close()
                    return True

            conn.close()
            return False
        except Exception:
            return False

    def _daily_entry_limit_reached(self, symbol: str, strategy_name: str) -> bool:
        """
        Return True if this strategy has already entered this symbol
        max_entries_per_strategy_per_day times today (open or closed).
        Limit is read from sl_config.json; falls back to MAX_ENTRIES_PER_STRATEGY_PER_DAY.
        """
        try:
            # Read limit from config so it can be changed without a code restart
            _sl_cfg = MasterResource.MASTER_ROOT / "config" / "sl_config.json"
            limit = self.MAX_ENTRIES_PER_STRATEGY_PER_DAY
            if _sl_cfg.exists():
                limit = json.loads(_sl_cfg.read_text()).get(
                    "max_entries_per_strategy_per_day", self.MAX_ENTRIES_PER_STRATEGY_PER_DAY
                )

            db_path = MasterResource.get_trading_db_path()
            conn    = sqlite3.connect(db_path)
            cur     = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM orders "
                "WHERE symbol=? AND strategy_name=? AND DATE(created_at)=DATE('now')",
                (symbol, strategy_name),
            )
            count = cur.fetchone()[0]
            conn.close()
            if count >= limit:
                self.logger.info(
                    "[DAILY CAP] %s/%s already entered %d× today (limit=%d) — blocked",
                    strategy_name, symbol, count, limit,
                )
                return True
            return False
        except Exception:
            return False

    def _is_duplicate_strike_entry(self, tradingsymbol: str, strategy_name: str) -> bool:
        """
        Gate 3 — same strategy must not enter the same exact strike more than once today.
        Catches the VWAP_Slope double-entry bug where one evaluation cycle fires the
        same strategy into the same resolved option symbol twice.
        Checks both OPEN and CLOSED orders (once entered, that's the day's allocation).
        """
        try:
            db_path = MasterResource.get_trading_db_path()
            conn    = sqlite3.connect(db_path)
            cur     = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM orders "
                "WHERE tradingsymbol=? AND strategy_name=? AND DATE(created_at)=DATE('now')",
                (tradingsymbol, strategy_name),
            )
            count = cur.fetchone()[0]
            conn.close()
            if count > 0:
                self.logger.info(
                    "[DEDUP GATE] %s already entered %s today (%d record) — blocked",
                    strategy_name, tradingsymbol, count,
                )
                return True
            return False
        except Exception:
            return False

    def _burst_limit_reached(self) -> bool:
        """
        Burst rate limiter — block if MAX_ORDERS_PER_5MIN algo orders were placed
        in the last 5 minutes across all strategies (TG:SIGNAL excluded).
        Prevents an evaluation-cycle burst from firing N strategies simultaneously
        into correlated positions on the same underlying move.
        """
        try:
            from datetime import datetime, timedelta
            db_path     = MasterResource.get_trading_db_path()
            conn        = sqlite3.connect(db_path)
            cur         = conn.cursor()
            five_min_ago = (datetime.now(IST) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
            cur.execute(
                "SELECT COUNT(*) FROM orders "
                "WHERE created_at >= ? AND strategy_name != 'TG:SIGNAL'",
                (five_min_ago,),
            )
            count = cur.fetchone()[0]
            conn.close()
            _sl_cfg_path = MasterResource.MASTER_ROOT / "config" / "sl_config.json"
            _burst_cap = self.MAX_ORDERS_PER_5MIN
            if _sl_cfg_path.exists():
                try:
                    _burst_cap = int(json.loads(_sl_cfg_path.read_text()).get(
                        "max_orders_per_5min", self.MAX_ORDERS_PER_5MIN))
                except Exception:
                    pass
            if count >= _burst_cap:
                self.logger.info(
                    "[BURST GATE] %d algo orders in last 5 min (cap=%d) — new entry blocked",
                    count, _burst_cap,
                )
                return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Kite historical seeding
    # ------------------------------------------------------------------

    def _init_kite(self):
        """Return a ready KiteConnect client, or None if config/token invalid."""
        try:
            from kiteconnect import KiteConnect
            cfg  = MasterResource.get_kite_config()
            kite = KiteConnect(api_key=cfg["api_key"])
            kite.set_access_token(cfg["access_token"])
            kite.profile()   # quick auth check
            self.logger.info("[KITE] Client ready for historical seeding")
            return kite
        except Exception as e:
            self.logger.warning(f"[KITE] Not available ({e}) — will warm up from live ticks only")
            return None

    def _seed_from_kite(self):
        """
        Pre-populate self.data with multi-day + today's candles.
        Step 1: load last 3 trading days from local DB (gives 200+ 5m bars immediately).
        Step 2: fetch today's fresh data from Kite API and merge on top.
        This path runs only when _DHAN_STORE_OK is False.
        """
        from datetime import date
        from kite_candle_store import load_days_from_db as _load_days
        today = date.today().strftime("%Y-%m-%d")

        if _CANDLE_STORE_OK:
            _cs_ensure()

        seeded = {}
        for idx in self.indices:
            token = KITE_INDEX_TOKENS.get(idx)
            if not token:
                continue
            exch = "BSE" if idx in ("SENSEX", "BANKEX", "SENSEX50") else "NSE"
            try:
                # Step 1: load last 3 days from local DB (instant, no API call)
                df_hist = _load_days(token, n_days=3, interval="minute") if _CANDLE_STORE_OK else pd.DataFrame()

                # Step 2: fetch today fresh from Kite (or local DB fallback)
                if _CANDLE_STORE_OK:
                    df_today, src1 = _cs_get(token, idx, exch, today, "minute")
                else:
                    df_today, src1 = self._fetch_kite_direct(token, today, "minute"), "KITE_API"

                # Merge historical base + today's fresh data
                df1 = _merge_candles(df_hist, df_today, tail=400) if df_today is not None and not df_today.empty else df_hist

                if df1 is not None and not df1.empty:
                    self.data[f"{idx}_1m"] = df1.tail(400).reset_index(drop=True)
                    seeded[f"{idx}_1m"]    = len(self.data[f"{idx}_1m"])
                    # Resample combined 1m → 5m/15m (gives Ichimoku 78+ bars immediately)
                    from dhan_candle_store import _resample
                    df5  = _resample(df1, 5)
                    df15 = _resample(df1, 15)
                    if not df5.empty:
                        self.data[f"{idx}_5m"] = df5.tail(150).reset_index(drop=True)
                        seeded[f"{idx}_5m"]    = len(self.data[f"{idx}_5m"])
                    if not df15.empty:
                        self.data[f"{idx}_15m"]  = df15.tail(100).reset_index(drop=True)
                        seeded[f"{idx}_15m"]     = len(self.data[f"{idx}_15m"])
                        self._last_15m_fetch[idx] = time.time()
                    continue  # 5m/15m already derived above

                # 5-min fallback if no 1m data at all
                if _CANDLE_STORE_OK:
                    df5, src5 = _cs_get(token, idx, exch, today, "5minute")
                else:
                    df5, src5 = self._fetch_kite_direct(token, today, "5minute"), "KITE_API"
                if df5 is not None and not df5.empty:
                    self.data[f"{idx}_5m"] = df5.tail(150).reset_index(drop=True)
                    seeded[f"{idx}_5m"]    = len(self.data[f"{idx}_5m"])

                # 15-min fallback
                if _CANDLE_STORE_OK:
                    df15, src15 = _cs_get(token, idx, exch, today, "15minute")
                else:
                    df15, src15 = self._fetch_kite_direct(token, today, "15minute"), "KITE_API"
                if df15 is not None and not df15.empty:
                    self.data[f"{idx}_15m"]   = df15.tail(100).reset_index(drop=True)
                    seeded[f"{idx}_15m"]       = len(self.data[f"{idx}_15m"])
                    self._last_15m_fetch[idx]  = time.time()

            except Exception as e:
                self.logger.warning(f"[SEED] {idx} failed: {e}")

        if seeded:
            ready = [k for k, v in seeded.items() if v >= MIN_CANDLES]
            self.logger.info(f"[SEED] Candles loaded: {seeded}")
            self.logger.info(f"[SEED] Ready for signals: {ready}")
        else:
            self.logger.warning("[SEED] No candles loaded — warming up from live ticks")

    def _fetch_kite_direct(self, token: int, date_str: str, interval: str):
        """Direct Kite API call (fallback when candle store not available)."""
        if not self._kite:
            return None
        try:
            raw = self._kite.historical_data(
                token, f"{date_str} 09:15:00", f"{date_str} 15:30:00", interval)
            if not raw:
                return None
            df = pd.DataFrame(raw).rename(columns={"date": "timestamp"})
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            return df[["timestamp", "open", "high", "low", "close", "volume"]]
        except Exception:
            return None

    def _refresh_kite_indices(self, indices: list):
        """Refresh 1m/5m/15m candles for given indices via Kite API → local DB fallback."""
        from datetime import date
        today = date.today().strftime("%Y-%m-%d")
        now   = time.time()
        for idx in indices:
            token = KITE_INDEX_TOKENS.get(idx)
            if not token:
                continue
            exch = "BSE" if idx in ("SENSEX", "BANKEX") else "NSE"
            try:
                # 1-min — merge today's Kite data with seeded multi-day history
                df1, _ = _cs_get(token, idx, exch, today, "minute")
                if df1 is not None and not df1.empty:
                    self.data[f"{idx}_1m"] = _merge_candles(
                        self.data.get(f"{idx}_1m", pd.DataFrame()), df1, tail=400
                    )
                    # Resample today's 1m to 5m/15m and merge with seeded history
                    from dhan_candle_store import _resample
                    df5  = _resample(df1, 5)
                    df15 = _resample(df1, 15)
                    if not df5.empty:
                        self.data[f"{idx}_5m"] = _merge_candles(
                            self.data.get(f"{idx}_5m", pd.DataFrame()), df5, tail=150
                        )
                    if not df15.empty:
                        self.data[f"{idx}_15m"] = _merge_candles(
                            self.data.get(f"{idx}_15m", pd.DataFrame()), df15, tail=100
                        )
            except Exception as e:
                self.logger.debug(f"[KITE] {idx} refresh failed: {e}")

    # ------------------------------------------------------------------
    # CPR computation
    # ------------------------------------------------------------------

    def _compute_cpr(self, index_name: str) -> tuple[float | None, float | None]:
        """
        Return (cpr_top, cpr_bot) for today's session, computed from the previous
        trading day's 1-min candles stored in kite_candles.db.

        Results are cached for the day — only one DB read per index per day.
        Returns (None, None) if previous-day data is unavailable.
        """
        from datetime import date as _date, timedelta as _td
        today = _date.today().strftime("%Y-%m-%d")

        cached = self._cpr_cache.get(index_name)
        if cached and cached.get("date") == today:
            return cached["top"], cached["bot"]

        if not _CANDLE_STORE_OK:
            return None, None

        token = KITE_INDEX_TOKENS.get(index_name)
        if not token:
            return None, None

        exch = "BSE" if index_name in ("SENSEX", "BANKEX") else "NSE"

        # Walk back up to 5 days to find the most recent trading day with data
        for offset in range(1, 6):
            prev_day = (_date.today() - _td(days=offset)).strftime("%Y-%m-%d")
            try:
                df_prev, _ = _cs_get(token, index_name, exch, prev_day, "minute")
                if df_prev is None or df_prev.empty:
                    continue
                prev_h = float(df_prev["high"].max())
                prev_l = float(df_prev["low"].min())
                prev_c = float(df_prev["close"].iloc[-1])
                pivot   = (prev_h + prev_l + prev_c) / 3
                cpr_top = (prev_h + prev_l) / 2
                cpr_bot = 2 * pivot - cpr_top
                self._cpr_cache[index_name] = {"date": today, "top": cpr_top, "bot": cpr_bot}
                self.logger.debug(
                    f"[CPR] {index_name}: prev={prev_day} H={prev_h:.0f} L={prev_l:.0f} "
                    f"C={prev_c:.0f} → top={cpr_top:.2f} bot={cpr_bot:.2f}"
                )
                return cpr_top, cpr_bot
            except Exception as e:
                self.logger.debug(f"[CPR] {index_name} prev_day={prev_day} failed: {e}")

        return None, None

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _parse_candle_response(self, raw: dict) -> pd.DataFrame:
        """Convert Dhan's array-of-columns response into a standard OHLCV DataFrame.

        Dhan returns:
          { "open": [...], "high": [...], "low": [...],
            "close": [...], "volume": [...], "timestamp": [...] }

        Timestamps are Unix epoch integers (IST).
        """
        if not raw or not isinstance(raw, dict):
            return pd.DataFrame()

        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(raw.keys()):
            return pd.DataFrame()

        ts_key = next((k for k in ("timestamp", "start_Time", "time") if k in raw), None)

        length = len(raw["open"])
        if length == 0:
            return pd.DataFrame()

        df = pd.DataFrame({
            "open":   raw["open"],
            "high":   raw["high"],
            "low":    raw["low"],
            "close":  raw["close"],
            "volume": raw["volume"],
        })

        if ts_key:
            df["timestamp"] = pd.to_datetime(raw[ts_key], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        else:
            df["timestamp"] = pd.NaT

        return df

    def _fetch_candles(self, name: str, interval: int) -> pd.DataFrame:
        """Fetch intraday candles for a named instrument and return a DataFrame.
        Returns an empty DataFrame on any error so callers can fall back silently.
        """
        meta = INSTRUMENT_REGISTRY.get(name)
        if meta is None:
            self.logger.warning(f"Unknown instrument: {name}")
            return pd.DataFrame()

        fetch_days = FETCH_DAYS_15M if interval >= 15 else FETCH_DAYS_5M
        to_date   = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=fetch_days)).strftime("%Y-%m-%d")

        try:
            resp = self.client.intraday_minute_data(
                security_id    = meta["security_id"],
                exchange_segment = meta["segment"],
                instrument_type  = meta["instrument"],
                from_date = from_date,
                to_date   = to_date,
                interval  = interval,
            )
        except Exception as e:
            self.logger.error(f"API call failed for {name} {interval}m: {e}")
            return pd.DataFrame()

        if resp.get("status") != "success":
            err_msg = ""
            remarks = resp.get("remarks", {})
            if isinstance(remarks, dict):
                err_msg = remarks.get("error_message", str(remarks))
            else:
                err_msg = str(remarks)
            # DH-907 = no data available (expected outside market hours) → DEBUG only
            if "DH-907" in err_msg or "unable to fetch" in err_msg.lower():
                self.logger.debug(f"No candle data for {name} {interval}m (market closed)")
            else:
                self.logger.warning(f"Data fetch failed for {name} {interval}m: {err_msg}")
            return pd.DataFrame()

        df = self._parse_candle_response(resp.get("data", {}))
        if df.empty:
            self.logger.debug(f"Empty candle data returned for {name} {interval}m")
        else:
            self.logger.info(f"Fetched {len(df)} candles for {name} {interval}m")
        return df

    def sync_data(self):
        """Refresh all market-data buffers.

        Priority:
          1. NSE Live Feed  (free, real prices, no Dhan token needed)
          2. Kite API       (fallback — covers all indices incl. SENSEX/MIDCPNIFTY)

        5m feeds are refreshed every cycle.
        15m feeds are refreshed at most once per CACHE_TTL_15M seconds.
        If a fetch fails the existing buffer is kept unchanged.
        """
        now = time.time()

        # --- NSE Live Feed: NIFTY, BANKNIFTY, FINNIFTY (60s HTTP polling) ---
        if self._nse_feed is not None:
            for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
                df1 = self._nse_feed.get_candles(idx, 1)
                if not df1.empty:
                    self.data[f"{idx}_1m"] = _merge_candles(
                        self.data.get(f"{idx}_1m", pd.DataFrame()), df1, tail=400
                    )

                df5 = self._nse_feed.get_candles(idx, 5)
                if not df5.empty:
                    self.data[f"{idx}_5m"] = _merge_candles(
                        self.data.get(f"{idx}_5m", pd.DataFrame()), df5, tail=150
                    )

                last_fetch = self._last_15m_fetch.get(idx, 0)
                if now - last_fetch >= CACHE_TTL_15M:
                    df15 = self._nse_feed.get_candles(idx, 15)
                    if not df15.empty:
                        self.data[f"{idx}_15m"] = _merge_candles(
                            self.data.get(f"{idx}_15m", pd.DataFrame()), df15, tail=100
                        )
                        self._last_15m_fetch[idx] = now

            # --- All 5 indices via Kite (authoritative, stored locally) ---
            self._refresh_kite_indices(["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY"])

            # Persist completed 1-min candles to DB
            if _DHAN_STORE_OK:
                try:
                    from dhan_candle_store import save_from_nse_feed as _save_nse
                    _save_nse(self._nse_feed, symbols=["NIFTY", "BANKNIFTY", "FINNIFTY"])
                except Exception as _e:
                    self.logger.debug(f"[PERSIST] candle save skipped: {_e}")
                try:
                    from dhan_candle_store import save_from_data_dict as _save_bse
                    _save_bse(self.data, symbols=["SENSEX", "MIDCPNIFTY"])
                except Exception as _e:
                    self.logger.debug(f"[PERSIST] BSE/MIDCP candle save skipped: {_e}")

            # Fetch equity leaders for PairLeadership via Kite (reliable, cached locally)
            if _CANDLE_STORE_OK:
                from datetime import date as _date
                _today = _date.today().strftime("%Y-%m-%d")
                for leader, (token, exch) in KITE_EQUITY_TOKENS.items():
                    try:
                        df_eq, _src = _cs_get(token, leader, exch, _today, "5minute")
                        if df_eq is not None and not df_eq.empty:
                            self.data[leader] = df_eq.tail(100).reset_index(drop=True)
                            self.logger.debug(f"[EQUITY] {leader}: {len(df_eq)} 5m candles from {_src}")
                            # Persist to local DB for cross-session continuity
                            try:
                                _cs_save(token, leader, exch, df_eq, "5minute")
                            except Exception as _se:
                                self.logger.debug(f"[EQUITY] {leader} DB save skipped: {_se}")
                    except Exception as _e:
                        self.logger.debug(f"[EQUITY] {leader} fetch skipped: {_e}")

            # Log candle counts periodically
            counts = {k: len(v) for k, v in self.data.items()
                      if isinstance(v, pd.DataFrame) and not v.empty}
            if any(v > 0 for v in counts.values()):
                self.logger.info(f"Candles: { {k:v for k,v in counts.items() if v>0} }")
            else:
                self.logger.info("Candles: warming up -- waiting for 1-min ticks")
            return   # handled -- skip Dhan API fallback

        # --- Fallback: Kite API (NSE Live Feed unavailable) ---
        # Covers all 5 indices (incl. SENSEX + MIDCPNIFTY) with real prices.
        if _CANDLE_STORE_OK:
            self._refresh_kite_indices(self.indices)
            from datetime import date as _date
            _today = _date.today().strftime("%Y-%m-%d")
            for leader, (token, exch) in KITE_EQUITY_TOKENS.items():
                try:
                    df_eq, _ = _cs_get(token, leader, exch, _today, "5minute")
                    if df_eq is not None and not df_eq.empty:
                        self.data[leader] = df_eq.tail(100).reset_index(drop=True)
                        self.logger.debug(f"[KITE] {leader}: {len(df_eq)} 5m candles")
                except Exception as _e:
                    self.logger.debug(f"[KITE] {leader} equity fetch failed: {_e}")
        elif self._kite:
            # Direct Kite API when local candle DB not available
            from datetime import date as _date
            _today = _date.today().strftime("%Y-%m-%d")
            for idx in self.indices:
                tok = KITE_INDEX_TOKENS.get(idx)
                if not tok:
                    continue
                for _interval, _key, _tail in [("minute", f"{idx}_1m", 400),
                                               ("5minute", f"{idx}_5m", 100)]:
                    df = self._fetch_kite_direct(tok, _today, _interval)
                    if df is not None and not df.empty:
                        self.data[_key] = _merge_candles(
                            self.data.get(_key, pd.DataFrame()), df, tail=_tail)
                if now - self._last_15m_fetch.get(idx, 0) >= CACHE_TTL_15M:
                    df15 = self._fetch_kite_direct(tok, _today, "15minute")
                    if df15 is not None and not df15.empty:
                        self.data[f"{idx}_15m"] = df15.tail(100).reset_index(drop=True)
                        self._last_15m_fetch[idx] = now

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        self.logger.info("Starting Omni-Strategy polling loop...")
        while True:
            try:
                if not _is_market_open():
                    self.logger.info("Outside market hours (9:15-15:25 IST) -- strategies paused")
                    time.sleep(60)
                    continue

                self.sync_data()

                # 1. Global Leader Bias (for NIFTY)
                if (len(self.data["RELIANCE"]) >= MIN_CANDLES and
                        len(self.data["HDFC"]) >= MIN_CANDLES):
                    pair_signal = self.strat_pair.check_signal(
                        self.data["RELIANCE"], self.data["HDFC"]
                    )
                    nifty_spot = (
                        float(self.data["NIFTY_1m"]["close"].iloc[-1])
                        if len(self.data.get("NIFTY_1m", [])) > 0
                        else 0.0
                    )
                    self._log_strategy_eval(
                        "NIFTY", "PairLeadership", pair_signal, nifty_spot,
                        self.data.get("NIFTY_1m"), self.data.get("NIFTY_5m"),
                    )
                    if pair_signal != "NEUTRAL":
                        self.execute("NIFTY", pair_signal, "PairLeadership")

                # 2. Per-index strategies
                for idx in self.indices:
                    df_1m  = self.data[f"{idx}_1m"]
                    df_5m  = self.data[f"{idx}_5m"]
                    df_15m = self.data[f"{idx}_15m"]

                    # Gate: wait for 30 × 1-min candles (= 30 min after open)
                    if len(df_1m) < MIN_CANDLES:
                        self.logger.debug(f"[WARMUP] {idx}: {len(df_1m)}/{MIN_CANDLES} 1m candles")
                        continue

                    # Current spot price for logging
                    _spot = float(df_1m["close"].iloc[-1]) if not df_1m.empty else 0.0

                    # Regime detection via ADX on 1m candles
                    # ADX >= 25 → trending  |  ADX < 20 → ranging  |  20-25 → transition (run all)
                    adx_val     = _regime_adx(df_1m)
                    is_trending = adx_val >= 25
                    is_ranging  = adx_val < 20
                    self.logger.debug(f"[REGIME] {idx}: ADX={adx_val:.1f} "
                                      f"({'TRENDING' if is_trending else 'RANGING' if is_ranging else 'TRANSITION'})")

                    # A. EMA 9/21 on 1m — trend only
                    if is_trending or not is_ranging:
                        ema_sig = self.strat_ema_921.check_signal(df_1m, idx)
                        _ema9  = float(df_1m["close"].ewm(span=9,  adjust=False).mean().iloc[-1])
                        _ema21 = float(df_1m["close"].ewm(span=21, adjust=False).mean().iloc[-1])
                        self._log_strategy_eval(idx, "EMA_9_21", ema_sig, _spot, df_1m, df_5m,
                                                {"ema9": round(_ema9, 2), "ema21": round(_ema21, 2),
                                                 "adx": round(adx_val, 1)})
                        if ema_sig != "NEUTRAL":
                            self.execute(idx, ema_sig, "EMA_9_21")

                    # B. Option Scalper: EMA44 on 5m — trend only
                    if (is_trending or not is_ranging) and len(df_5m) >= self.strat_scalp.ema_period + 2:
                        scalp_sig = self.strat_scalp.check_signal(df_5m, idx)
                        self._log_strategy_eval(idx, "OptionScalper_EMA44", scalp_sig, _spot, df_5m, df_15m)
                        if scalp_sig != "NEUTRAL":
                            self.execute(idx, scalp_sig, "OptionScalper_EMA44")

                    # C. Supertrend + MACD on 5m — trend only
                    if (is_trending or not is_ranging) and len(df_5m) >= MIN_CANDLES_5M:
                        st_sig = self.strat_st_macd.check_signal(df_5m, idx)
                        self._log_strategy_eval(idx, "Supertrend_MACD", st_sig, _spot, df_1m, df_5m)
                        if st_sig != "NEUTRAL":
                            self.execute(idx, st_sig, "Supertrend_MACD")

                    # D. EMA_VWAP_SR: 1m signal + 5m S/R — trend only
                    if (is_trending or not is_ranging) and len(df_5m) >= MIN_CANDLES_5M:
                        sup_5m  = df_5m["low"].rolling(window=min(21, len(df_5m))).min().iloc[-1]
                        res_5m  = df_5m["high"].rolling(window=min(21, len(df_5m))).max().iloc[-1]
                        adv_sig = self.strat_adv.check_ema_vwap_sr(df_1m, sup_5m, res_5m, idx)
                        self._log_strategy_eval(idx, "EMA_VWAP_SR", adv_sig, _spot, df_1m, df_5m,
                                                {"support": round(float(sup_5m), 2),
                                                 "resistance": round(float(res_5m), 2)})
                        if adv_sig != "NEUTRAL":
                            self.execute(idx, adv_sig, "EMA_VWAP_SR")

                    # E. ORB + EMA21 on 1m — runs in all regimes (breakout works anywhere)
                    orb_sig = self.strat_adv.check_orb_vwap(df_1m, idx)
                    self._log_strategy_eval(idx, "ORB_VWAP", orb_sig, _spot, df_1m, df_5m)
                    if orb_sig != "NEUTRAL":
                        orb_ctx = self.strat_adv._orb_levels.get(idx, {})
                        self.execute(idx, orb_sig, "ORB_VWAP", strategy_context=orb_ctx)

                    # F. Triple Bottom / Triple Top on 5m — runs in all regimes (reversal pattern)
                    if len(df_5m) >= self.strat_triple.lookback:
                        tri_sig = self.strat_triple.check_signal(df_5m, idx)
                        self._log_strategy_eval(idx, "TriplePattern", tri_sig, _spot, df_1m, df_5m)
                        if tri_sig != "NEUTRAL":
                            self.execute(idx, tri_sig, "TriplePattern")

                    # G. Index Momentum — trend only (BANKNIFTY now included)
                    if (is_trending or not is_ranging) and idx in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"):
                        mom_sig = self.strat_momentum.check_signal(df_1m, idx)
                        ago = self.strat_momentum.last_fired_ago(idx)
                        self._log_strategy_eval(
                            idx, "IndexMomentum", mom_sig, _spot, df_1m, df_5m,
                            {"cooldown_secs_ago": round(ago, 0) if ago else None}
                        )
                        if mom_sig != "NEUTRAL":
                            self.execute(idx, mom_sig, "IndexMomentum")

                    # H. Bollinger Mean Reversion on 5m — ranging markets only
                    if is_ranging and len(df_5m) >= self.strat_bb.bb_period + 2:
                        bb_sig = self.strat_bb.check_signal(df_5m, idx)
                        self._log_strategy_eval(idx, "BB_MeanReversion", bb_sig, _spot, df_1m, df_5m,
                                                {"adx": round(adx_val, 1)})
                        if bb_sig != "NEUTRAL":
                            self.execute(idx, bb_sig, "BB_MeanReversion")

                    # I. VWAP Reclaim / Rejection / Band-Extreme on 1m — all regimes
                    #    (active-window and cooldown gated internally: 9:30-11am, 1:30-3pm)
                    vwap_sig = self.strat_vwap_reclaim.check_signal(df_1m, idx)
                    self._log_strategy_eval(idx, "VWAPReclaim", vwap_sig, _spot, df_1m, df_5m)
                    if vwap_sig != "NEUTRAL":
                        self.execute(idx, vwap_sig, "VWAPReclaim")

                    # J. CPR Breakout on 1m — trend / transition only (narrow CPR = trending day)
                    if is_trending or not is_ranging:
                        cpr_top, cpr_bot = self._compute_cpr(idx)
                        cpr_sig = self.strat_cpr.check_signal(df_1m, cpr_top, cpr_bot, idx)
                        self._log_strategy_eval(idx, "CPRBreakout", cpr_sig, _spot, df_1m, df_5m,
                                                {"cpr_top": round(cpr_top, 2) if cpr_top else None,
                                                 "cpr_bot": round(cpr_bot, 2) if cpr_bot else None})
                        if cpr_sig != "NEUTRAL":
                            self.execute(idx, cpr_sig, "CPRBreakout")

            except Exception as e:
                self.logger.error(f"Engine loop error: {e}")

            time.sleep(10)

    # ------------------------------------------------------------------
    # Execution & logging
    # ------------------------------------------------------------------

    def _get_option_ltp(
        self,
        trading_symbol:   str,
        expiry_date:      str,
        spot:             float,
        security_id:      str = None,
        exchange_segment: str = "NSE_FNO",
    ) -> float:
        """
        Fetch the option's current LTP.  Three-level fallback:

        1. kite_candles.db  — populated by OCC for already-tracked options.
        2. Dhan intraday_minute_data — live 1-min close for this security_id.
        3. DTE-adjusted ATM estimate — spot × 0.5% × √DTE.
           Calibrated empirically:
             DTE=1  → ~0.5% of spot  (NIFTY weekly ATM ≈ correct)
             DTE=15 → ~1.9% of spot  (FINNIFTY monthly ATM ≈ correct)
             DTE=30 → ~2.7% of spot  (far monthly ≈ correct)
        """
        from math import sqrt as _sqrt

        # ── 1. Kite candles DB ────────────────────────────────────────────────
        # IMPORTANT: filter to today's date only — stale candles from previous
        # sessions carry the old underlying level and produce wrong entry prices
        # (e.g. FINNIFTY_25950_CE_2026-05-26 had a May-05 close of 405 which
        # was served as "current LTP" when the real market price was 141).
        try:
            from datetime import date as _date_cls
            today_prefix = _date_cls.today().isoformat()  # "2026-05-25"
            parts = trading_symbol.split("-")   # ['FINNIFTY','May2026','25850','CE']
            candle_sym = f"{parts[0]}_{parts[2]}_{parts[3]}_{expiry_date}"
            db_path = str(Path(MasterResource.MASTER_ROOT) / "data" / "kite_candles.db")
            conn = sqlite3.connect(db_path, timeout=2)
            row = conn.execute(
                "SELECT close FROM candles_1min "
                "WHERE tradingsymbol=? AND dt LIKE ? "
                "ORDER BY dt DESC LIMIT 1",
                (candle_sym, f"{today_prefix}%"),
            ).fetchone()
            conn.close()
            if row and row[0]:
                return float(row[0])
        except Exception:
            pass

        # ── 2. Kite ltp() for this specific option ───────────────────────────────
        if _CANDLE_STORE_OK:
            try:
                _parts = trading_symbol.split("-")
                if len(_parts) >= 4:
                    _base, _mon_str, _strike_str, _otype = (
                        _parts[0], _parts[1], _parts[2], _parts[3])
                    _MON_MAP = {
                        'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4,
                        'May': 5, 'Jun': 6, 'Jul': 7, 'Aug': 8,
                        'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
                    }
                    _mon = _MON_MAP.get(_mon_str[:3])
                    _yr  = int(_mon_str[3:]) if len(_mon_str) > 3 else None
                    _exch = "BFO" if _base in {"SENSEX", "BANKEX", "SENSEX50"} else "NFO"
                    if _mon and _yr:
                        import kite_candle_store as _kcs_mod
                        _df_inst = _kcs_mod._kite_instruments(_exch)
                        if _df_inst is not None and not _df_inst.empty:
                            from datetime import date as _d
                            _td = _d.today()
                            _hits = _df_inst[
                                (_df_inst["name"]            == _base) &
                                (_df_inst["strike"]          == float(_strike_str)) &
                                (_df_inst["instrument_type"] == _otype) &
                                (_df_inst["expiry"].apply(
                                    lambda d: d.month == _mon and d.year == _yr))
                            ].sort_values("expiry")
                            _fut = _hits[_hits["expiry"].apply(lambda d: d >= _td)]
                            _best = (_fut.iloc[0] if not _fut.empty
                                     else (_hits.iloc[-1] if not _hits.empty else None))
                            if _best is not None:
                                _tok = int(_best["instrument_token"])
                                _kite = _kcs_mod.get_kite()
                                _resp = _kite.ltp([str(_tok)])
                                _entry = _resp.get(str(_tok))
                                if _entry:
                                    ltp = float(_entry["last_price"])
                                    if ltp > 0:
                                        return ltp
            except Exception:
                pass

        # ── 3. DTE-adjusted fallback ──────────────────────────────────────────
        try:
            from datetime import date as _date
            dte = max(1, (_date.fromisoformat(expiry_date) - _date.today()).days)
            return round(spot * 0.005 * _sqrt(dte), 2)
        except Exception:
            pass

        return round(spot * 0.01, 2)

    def execute(self, symbol: str, bias: str, strategy_name: str,
                strategy_context: dict | None = None):
        action      = "BUY"
        option_type = "CE"   if bias == "BULLISH" else "PE"

        # --- Circuit breaker: stop new entries if daily realized loss exceeds threshold ---
        try:
            _pnl_file = MasterResource.MASTER_ROOT / "data" / "daily_pnl_state.json"
            _sl_cfg_file = MasterResource.MASTER_ROOT / "config" / "sl_config.json"
            if _pnl_file.exists():
                _state = json.loads(_pnl_file.read_text())
                _today = date.today().isoformat()
                # Skip stale file from a previous day — SL monitor resets it on first exit
                if _state.get("date") == _today:
                    _limit = -20000
                    if _sl_cfg_file.exists():
                        _limit = json.loads(_sl_cfg_file.read_text()).get("daily_loss_limit", -20000)
                    if _state.get("realized_pnl", 0) <= _limit:
                        self.logger.warning(
                            "[CIRCUIT BREAKER] Daily realized PnL %.0f ≤ limit %.0f — "
                            "blocking new entry %s %s", _state["realized_pnl"], _limit, strategy_name, symbol
                        )
                        return
        except Exception:
            pass

        # --- Gate 1 & 2: open-position and global algo cap ---
        if self._has_open_position(symbol, strategy_name):
            self.logger.debug(
                f"[{strategy_name}] Position blocked for {symbol} (open pos or global cap)"
            )
            return
        if self._daily_entry_limit_reached(symbol, strategy_name):
            return

        # --- Spot price from live buffer ---
        df_5m = self.data.get(f"{symbol}_5m")
        if df_5m is None or df_5m.empty:
            self.logger.warning(f"No spot data for {symbol}, skipping execution")
            return
        spot = float(df_5m["close"].iloc[-1])

        # --- Spread routing: 3-tier based on ADX strength ---
        # ADX < 25   → single-leg BUY
        # 25 ≤ ADX < 30 → debit spread (BUY ATM + SELL ATM±1)
        # ADX ≥ 30   → ratio back spread (BUY 2× ATM±2 + SELL 1× ATM±1)
        adx_val    = _regime_adx(df_5m)
        use_rbs    = adx_val >= _RBS_ADX_MIN
        use_spread = adx_val >= _SPREAD_ADX_MIN and not use_rbs

        # --- Momentum gate: underlying must confirm trade direction on last 2 candles ---
        # ORB_VWAP is exempt — it has its own directional confirmation (RSI>50, EMA21, ADX)
        # and fires at breakout moments where the 5m candle may momentarily lag.
        if strategy_name != "ORB_VWAP" and len(df_5m) >= 2:
            last_close = float(df_5m["close"].iloc[-1])
            prev_close = float(df_5m["close"].iloc[-2])
            if option_type == "CE" and last_close <= prev_close:
                self.logger.info(
                    "[MOMENTUM GATE] %s %s CE blocked — underlying not rising "
                    "(%.0f → %.0f)", strategy_name, symbol, prev_close, last_close
                )
                return
            if option_type == "PE" and last_close >= prev_close:
                self.logger.info(
                    "[MOMENTUM GATE] %s %s PE blocked — underlying not falling "
                    "(%.0f → %.0f)", strategy_name, symbol, prev_close, last_close
                )
                return

        # --- Volatility compression gate: mean-reversion scalp strategies only ---
        # BB_MeanReversion and MultiStrikeScalp have edge when volatility is contracting
        # (ATR short-window < ATR long-window). Trend/breakout strategies are exempt.
        # PowerCandle fires ON expanded bars — also exempt.
        _COMPRESSION_STRATEGIES = {"BB_MeanReversion", "MultiStrikeScalp"}
        if strategy_name in _COMPRESSION_STRATEGIES:
            try:
                from strategies.indicators import atr_contracting as _atr_cont
                if len(df_5m) >= 21 and not _atr_cont(df_5m, short=5, long=20):
                    self.logger.debug(
                        "[ATR COMPRESSION GATE] %s %s blocked — ATR expanding, not contracting",
                        strategy_name, symbol,
                    )
                    return
            except Exception:
                pass   # fail open

        # --- Resolve base ATM option (itm_shift disabled — DTE logic below replaces it) ---
        option = self.strike_lookup.get_atm_option(symbol, spot, option_type, itm_shift=False)
        if option is None:
            self.logger.error(
                f"Strike lookup failed for {symbol} ATM {option_type} "
                f"at spot {spot:.0f} — skipping order"
            )
            return

        # --- DTE-based strike selection matrix ---
        # DTE=0 (expiry day)  : 1 step ITM — intrinsic value + delta > 0.65
        # DTE=1 (day before)  : ATM — balanced delta/theta
        # DTE≥2 (normal)      : 1 step OTM — maximum % leverage on trend
        try:
            _exp_dt = datetime.strptime(option["expiry_date"], "%Y-%m-%d").date()
            _dte    = (_exp_dt - date.today()).days
        except Exception:
            _dte = 1

        if _dte == 0:
            _dte_offset = -1 if option_type == "CE" else +1   # ITM
        elif _dte >= 2:
            _dte_offset = +1 if option_type == "CE" else -1   # OTM
        else:
            _dte_offset = 0   # DTE=1: keep ATM

        if _dte_offset != 0:
            _adj = self.strike_lookup.get_atm_option_offset(
                symbol, spot, option_type, _dte_offset, option["expiry_date"]
            )
            if _adj is not None:
                self.logger.info(
                    "[DTE=%d] Strike %s → %s", _dte,
                    option["trading_symbol"], _adj["trading_symbol"]
                )
                option = _adj
            else:
                self.logger.debug(
                    "[DTE=%d] Offset strike not found — keeping ATM %s",
                    _dte, option["trading_symbol"]
                )

        # --- General 15:00 cutoff gate (non-expiry days only) ---
        # On a normal trading day (DTE > 0), block all new entries at or after 15:00 IST.
        # Expiry-day entries (DTE = 0) are exempt — spikes happen right up to 15:25.
        # Config key: general_entry_cutoff (default "15:00", HH:MM IST).
        if _dte > 0:
            try:
                _sl_cfg_path = MasterResource.MASTER_ROOT / "config" / "sl_config.json"
                _cutoff_str  = json.loads(_sl_cfg_path.read_text()).get("general_entry_cutoff", "15:00")
                _ch, _cm     = map(int, _cutoff_str.split(":"))
                _now_t       = datetime.now().time()
                if _now_t >= __import__("datetime").time(_ch, _cm):
                    self.logger.info(
                        "[LATE ENTRY GATE] %s %s blocked — non-expiry day, time %s >= cutoff %s",
                        strategy_name, option["trading_symbol"],
                        _now_t.strftime("%H:%M"), _cutoff_str,
                    )
                    return
            except Exception:
                pass   # fail open — never block on config read error

        # --- Gate 3: same strategy must not re-enter the same resolved strike today ---
        if self._is_duplicate_strike_entry(option["trading_symbol"], strategy_name):
            return

        # --- Burst rate limiter: max MAX_ORDERS_PER_5MIN orders in any 5-min window ---
        if self._burst_limit_reached():
            self.logger.info(
                "[BURST GATE] %s %s blocked by burst limiter", strategy_name, symbol
            )
            return

        # --- Max Pain gate (expiry day, last 2 hours only) ---
        try:
            from agents.pcr_filter import get_instance as _pcr_get
            _pcr = _pcr_get()
            if _pcr is not None and _pcr.is_max_pain_blocked(spot, option_type):
                self.logger.info(
                    "[MAX PAIN] %s %s %s blocked — spot=%.0f far from max_pain=%.0f on expiry",
                    strategy_name, symbol, option_type, spot, _pcr.max_pain,
                )
                return
        except Exception:
            pass

        # --- PCR OI conviction gate (ported from OptionBuyingStrategies) ---
        # CE entry requires institutional put-writing support (PCR_OI > 1.2 = BULLISH).
        # PE entry requires institutional call-writing pressure (PCR_OI < 0.7 = BEARISH).
        # If PCR is NEUTRAL or filter not ready, pass through without blocking.
        try:
            from agents.pcr_filter import get_instance as _pcr_get2
            _pcr2 = _pcr_get2()
            if _pcr2 is not None and _pcr2.is_ready:
                _bias = _pcr2.pcr_bias
                if option_type == "CE" and _bias == "BEARISH":
                    self.logger.info(
                        "[PCR GATE] %s %s CE blocked — PCR bearish (OI<0.7, no put-writing support)",
                        strategy_name, symbol,
                    )
                    return
                if option_type == "PE" and _bias == "BULLISH":
                    self.logger.info(
                        "[PCR GATE] %s %s PE blocked — PCR bullish (OI>1.2, put-writers bullish)",
                        strategy_name, symbol,
                    )
                    return
        except Exception:
            pass

        # --- SL exit blacklist (cross-strategy, intraday) ---
        # If ANY strategy took a stop-loss hit on this exact tradingsymbol today,
        # block all further entries regardless of which strategy is now requesting it.
        # Prevents whipsaw re-entries on options that proved lossy for any strategy.
        if _is_sl_blacklisted(option["trading_symbol"]):
            self.logger.info(
                "[SL BLACKLIST] %s %s blocked — %s hit SL today, cross-strategy re-entry prevented",
                strategy_name, symbol, option["trading_symbol"],
            )
            return

        # --- Position sizing: notional risk targeting + fractional Kelly overlay ---
        try:
            from core.position_sizer import compute_lots as _compute_lots, _get_cfg as _ps_cfg
            _ps_conf = _ps_cfg(str(MasterResource.MASTER_ROOT / "config" / "sl_config.json"))
            _sl_pct  = float(_ps_conf.get("index_sl_percent", 8.0))
            _premium = self._get_option_ltp(
                trading_symbol   = option["trading_symbol"],
                expiry_date      = option["expiry_date"],
                spot             = spot,
                security_id      = option["security_id"],
                exchange_segment = option["exchange_segment"],
            )
            _n_lots = _compute_lots(
                strategy_name = strategy_name,
                premium       = _premium,
                lot_size      = option["lot_size"],
                sl_pct        = _sl_pct,
                db_path       = str(MasterResource.get_trading_db_path()),
                cfg_path      = str(MasterResource.MASTER_ROOT / "config" / "sl_config.json"),
            )
            if _n_lots > 1:
                self.logger.info(
                    "[POSITION SIZE] %s %s → %d lots (premium=%.1f, risk/lot=Rs%.0f)",
                    strategy_name, option["trading_symbol"], _n_lots,
                    _premium, _premium * option["lot_size"] * _sl_pct / 100,
                )
                option["lot_size"] = _n_lots * option["lot_size"]
        except Exception as _ps_err:
            self.logger.debug("[POSITION SIZE] sizing skipped: %s", _ps_err)

        # --- Route to ratio back spread, debit spread, or single-leg ---
        if use_rbs:
            self.logger.info(
                "[RBS] ADX=%.1f >= %.0f — using ratio back spread for %s %s %s",
                adx_val, _RBS_ADX_MIN, strategy_name, symbol, option_type,
            )
            self._execute_ratio_back_spread(
                symbol, option_type, strategy_name, option, spot, strategy_context
            )
            return

        if use_spread:
            self.logger.info(
                "[SPREAD] ADX=%.1f >= %.0f — using debit spread for %s %s %s",
                adx_val, _SPREAD_ADX_MIN, strategy_name, symbol, option_type,
            )
            self._execute_debit_spread(
                symbol, option_type, strategy_name, option, spot, strategy_context
            )
            return

        # --- Single-leg execution (ADX < threshold) ---
        self.logger.info(
            f"[SIGNAL] {strategy_name} → {action} {option['trading_symbol']} "
            f"(spot={spot:.0f}, strike={option['strike']}, "
            f"expiry={option['expiry_date']}, lot={option['lot_size']})"
        )

        option_ltp = self._get_option_ltp(
            option["trading_symbol"], option["expiry_date"], spot,
            security_id=option["security_id"],
            exchange_segment=option["exchange_segment"],
        )
        order_id = self.placer.place_market_order(
            security_id      = option["security_id"],
            exchange_segment = option["exchange_segment"],
            transaction_type = action,
            quantity         = option["lot_size"],
            ltp              = option_ltp,
        )

        if not order_id:
            # Dhan rejected — always paper-record (sandbox DH-905 or live BSE_FNO limit)
            order_id = f"SANDBOX_OMNI_{time.time_ns() // 1000}"
            self.logger.warning(
                f"[PAPER] Dhan rejected {option['trading_symbol']} → local paper trade {order_id}"
            )
            self.placer.failed_attempts = 0

        if order_id:
            fill_price = option_ltp if _is_paper_order(order_id) else self._fetch_fill_price(order_id, option)
            self.log_to_master(
                symbol, action, strategy_name, order_id, option,
                entry_price=fill_price,
                quantity=option["lot_size"],
                strategy_context=strategy_context,
            )

    def _execute_debit_spread(
        self,
        symbol:           str,
        option_type:      str,
        strategy_name:    str,
        long_option:      dict,
        spot:             float,
        strategy_context: dict | None,
    ) -> None:
        """
        Place a debit spread: BUY ATM CE/PE (long_option) + SELL 1-strike OTM CE/PE.

        For a CE bull spread  : long = ATM CE, short = ATM+1 strike CE.
        For a PE bear spread  : long = ATM PE, short = ATM-1 strike PE.
        OTM short leg caps max loss by ~35-45% vs naked long; max gain = width minus debit.
        Falls back to single-leg if the OTM short leg is not found in the scrip master.
        Both legs are paper-recorded on Dhan rejection (sandbox DH-905 is expected).
        Both legs are logged to the orders table so the SL monitor can track them.
        """
        # ── Resolve OTM (short) leg ───────────────────────────────────────────
        short_offset = +1 if option_type == "CE" else -1
        neighbors    = self.strike_lookup.get_atm_neighbors(
            symbol, spot, long_option["expiry_date"]
        )
        short_option = next(
            (n for n in neighbors
             if n["trading_symbol"].split("-")[-1] == option_type
             and n.get("atm_offset") == short_offset),
            None,
        )

        if short_option is None:
            self.logger.warning(
                "[SPREAD] OTM short leg not found for %s %s — falling back to single-leg BUY",
                symbol, option_type,
            )
            long_ltp = self._get_option_ltp(
                long_option["trading_symbol"], long_option["expiry_date"], spot,
                security_id=long_option["security_id"],
                exchange_segment=long_option["exchange_segment"],
            )
            order_id = self.placer.place_market_order(
                security_id=long_option["security_id"],
                exchange_segment=long_option["exchange_segment"],
                transaction_type="BUY",
                quantity=long_option["lot_size"],
                ltp=long_ltp,
            )
            if not order_id:
                order_id = f"SANDBOX_OMNI_{time.time_ns() // 1000}"
                self.placer.failed_attempts = 0
            fill = long_ltp if _is_paper_order(order_id) else self._fetch_fill_price(order_id, long_option)
            self.log_to_master(symbol, "BUY", strategy_name, order_id, long_option,
                               entry_price=fill, quantity=long_option["lot_size"],
                               strategy_context=strategy_context)
            return

        # ── Fetch LTPs for both legs ──────────────────────────────────────────
        long_ltp = self._get_option_ltp(
            long_option["trading_symbol"], long_option["expiry_date"], spot,
            security_id=long_option["security_id"],
            exchange_segment=long_option["exchange_segment"],
        )
        short_ltp = self._get_option_ltp(
            short_option["trading_symbol"], short_option["expiry_date"], spot,
            security_id=short_option["security_id"],
            exchange_segment=short_option["exchange_segment"],
        )
        net_debit = long_ltp - short_ltp

        self.logger.info(
            "[SPREAD] %s %s | LONG %s @ %.2f  SHORT %s @ %.2f  net_debit=%.2f",
            strategy_name, symbol,
            long_option["trading_symbol"],  long_ltp,
            short_option["trading_symbol"], short_ltp,
            net_debit,
        )

        # ── Place both legs ───────────────────────────────────────────────────
        long_id, short_id = self.placer.place_debit_spread(
            long_security_id  = long_option["security_id"],
            short_security_id = short_option["security_id"],
            exchange_segment  = long_option["exchange_segment"],
            quantity          = long_option["lot_size"],
            long_ltp          = long_ltp,
            short_ltp         = short_ltp,
        )

        # Paper fallback for each leg independently
        if not long_id:
            long_id = f"SANDBOX_OMNI_{time.time_ns() // 1000}_L"
            self.logger.warning("[PAPER SPREAD] Long leg papered: %s", long_id)
            self.placer.failed_attempts = 0

        if not short_id:
            short_id = f"SANDBOX_OMNI_{time.time_ns() // 1000}_S"
            self.logger.warning("[PAPER SPREAD] Short leg papered: %s", short_id)
            self.placer.failed_attempts = 0

        # ── Log both legs to DB ───────────────────────────────────────────────
        spread_ctx = dict(strategy_context or {})
        spread_ctx.update({"spread_mode": True, "net_debit": round(net_debit, 2)})

        long_fill  = long_ltp  if _is_paper_order(long_id)  else self._fetch_fill_price(long_id,  long_option)
        short_fill = short_ltp if _is_paper_order(short_id) else self._fetch_fill_price(short_id, short_option)

        self.log_to_master(
            symbol, "BUY",  strategy_name, long_id,  long_option,
            entry_price=long_fill,  quantity=long_option["lot_size"],  strategy_context=spread_ctx,
        )
        self.log_to_master(
            symbol, "SELL", strategy_name, short_id, short_option,
            entry_price=short_fill, quantity=short_option["lot_size"], strategy_context=spread_ctx,
        )

    def _execute_ratio_back_spread(
        self,
        symbol:           str,
        option_type:      str,
        strategy_name:    str,
        atm_option:       dict,
        spot:             float,
        strategy_context: dict | None,
    ) -> None:
        """
        Place a ratio back spread:
          CE (bullish): SELL 1 lot ATM+1 CE  +  BUY 2 lots ATM+2 CE
          PE (bearish): SELL 1 lot ATM-1 PE  +  BUY 2 lots ATM-2 PE

        Net: credit from short leg partially funds 2 long legs.
        Profits on large directional move; bounded loss if market stays flat.
        Gate: ADX >= _RBS_ADX_MIN (30) — triggered from execute().

        Falls back to debit spread if ATM±2 is not in the scrip master.
        Safe placement order: BUY 2× long leg first, then SELL 1× short leg.
        """
        expiry    = atm_option["expiry_date"]
        short_off = +1 if option_type == "CE" else -1
        long_off  = +2 if option_type == "CE" else -2

        short_option = self.strike_lookup.get_atm_option_offset(
            symbol, spot, option_type, short_off, expiry
        )
        long_option  = self.strike_lookup.get_atm_option_offset(
            symbol, spot, option_type, long_off, expiry
        )

        if short_option is None or long_option is None:
            missing = "ATM+1" if short_option is None else "ATM+2"
            self.logger.warning(
                "[RBS] %s leg not found for %s %s — falling back to debit spread",
                missing, symbol, option_type,
            )
            self._execute_debit_spread(
                symbol, option_type, strategy_name, atm_option, spot, strategy_context
            )
            return

        long_ltp  = self._get_option_ltp(
            long_option["trading_symbol"],  long_option["expiry_date"],  spot,
            security_id=long_option["security_id"],
            exchange_segment=long_option["exchange_segment"],
        )
        short_ltp = self._get_option_ltp(
            short_option["trading_symbol"], short_option["expiry_date"], spot,
            security_id=short_option["security_id"],
            exchange_segment=short_option["exchange_segment"],
        )
        lot_size  = atm_option["lot_size"]
        net_cost  = round(long_ltp * 2 - short_ltp, 2)   # 2× long premium − short credit

        self.logger.info(
            "[RBS] %s %s | BUY 2x %s @ %.2f  SELL 1x %s @ %.2f  net_cost=%.2f",
            strategy_name, symbol,
            long_option["trading_symbol"],  long_ltp,
            short_option["trading_symbol"], short_ltp,
            net_cost,
        )

        buy_id, sell_id = self.placer.place_ratio_back_spread(
            long_security_id  = long_option["security_id"],
            short_security_id = short_option["security_id"],
            exchange_segment  = long_option["exchange_segment"],
            lot_size          = lot_size,
            long_ltp          = long_ltp,
            short_ltp         = short_ltp,
        )

        if not buy_id:
            buy_id = f"SANDBOX_RBS_{time.time_ns() // 1000}_L"
            self.logger.warning("[PAPER RBS] Long legs papered: %s", buy_id)
            self.placer.failed_attempts = 0

        if not sell_id:
            sell_id = f"SANDBOX_RBS_{time.time_ns() // 1000}_S"
            self.logger.warning("[PAPER RBS] Short leg papered: %s", sell_id)
            self.placer.failed_attempts = 0

        rbs_ctx = dict(strategy_context or {})
        rbs_ctx.update({
            "spread_mode": True,
            "spread_type": "RBS",
            "net_cost":    net_cost,
        })

        long_fill  = long_ltp  if _is_paper_order(buy_id)  else self._fetch_fill_price(buy_id,  long_option)
        short_fill = short_ltp if _is_paper_order(sell_id) else self._fetch_fill_price(sell_id, short_option)

        # BUY leg: 2× lot_size
        self.log_to_master(
            symbol, "BUY",  strategy_name, buy_id,  long_option,
            entry_price=long_fill,  quantity=lot_size * 2, strategy_context=rbs_ctx,
        )
        # SELL leg: 1× lot_size
        self.log_to_master(
            symbol, "SELL", strategy_name, sell_id, short_option,
            entry_price=short_fill, quantity=lot_size,     strategy_context=rbs_ctx,
        )

        self.logger.info(
            "[RBS] Orders placed — BUY %s  SELL %s  net_cost=%.2f",
            buy_id, sell_id, net_cost,
        )

    def execute_gamma_directional(
        self,
        index_name: str,
        direction:  str,   # "BULLISH" → buy CE | "BEARISH" → buy PE
        option:     dict,  # StrikeLookup.get_atm_option() result for the chosen leg
        score:      int,
        signals:    list,
    ):
        """
        Buy the directional ATM option leg for a confirmed gamma blast.
        Called directly by GammaBlastWorker (bypasses MetaAgent — time-sensitive).
        Always BUY (never sell) regardless of direction.

        direction = BULLISH → BUY CE (market rallying, dealers buy spot to hedge)
        direction = BEARISH → BUY PE (market falling, dealers sell spot to hedge)
        """
        strategy_name = "GammaBlast"
        leg           = "CE" if direction == "BULLISH" else "PE"

        if self._has_open_position(index_name, strategy_name):
            self.logger.debug(
                "[GammaBlast] Open position for %s — skipping", index_name
            )
            return

        spot = self._get_spot_from_data(index_name)

        self.logger.info(
            "[GAMMA BLAST] %s score=%d/10 | %s → BUY %s (%s) ATM=%s",
            index_name, score, direction, leg,
            option["trading_symbol"], option["strike"],
        )

        option_ltp = self._get_option_ltp(
            option["trading_symbol"], option["expiry_date"], spot or 0,
            security_id=option["security_id"],
            exchange_segment=option["exchange_segment"],
        )
        order_id = self.placer.place_market_order(
            security_id      = option["security_id"],
            exchange_segment = option["exchange_segment"],
            transaction_type = "BUY",
            quantity         = option["lot_size"],
            ltp              = option_ltp,
        )

        if not order_id:  # Always paper-record when Dhan rejects
            order_id = f"SANDBOX_OMNI_{time.time_ns() // 1000}"
            self.logger.warning(
                f"[PAPER] Dhan rejected {option['trading_symbol']} → paper trade {order_id}"
            )
            self.placer.failed_attempts = 0

        if order_id:
            fill_price = option_ltp if _is_paper_order(order_id) else self._fetch_fill_price(order_id, option)
            self.log_to_master(
                index_name, "BUY", strategy_name, order_id, option,
                entry_price=fill_price, quantity=option["lot_size"],
            )
            self.logger.info(
                "[GAMMA BLAST] Order placed — id=%s  %s  signals=%s",
                order_id, option["trading_symbol"], signals,
            )

    def execute_vix_straddle(self, index_name: str, pct60: float, pct120: float):
        """
        Buy ATM straddle (CE + PE) for the VIX Percentile Straddle strategy (Worker W).
        Called directly by VIXStraddleWorker when both VIX percentiles < 25.
        Both legs use the same ATM strike and nearest weekly expiry.

        SL/Target (50%) is tracked by the SL monitor against the combined entry cost.
        """
        strat_ce = "VIXStraddle_CE"
        strat_pe = "VIXStraddle_PE"

        if self._has_open_position(index_name, strat_ce):
            self.logger.debug("[VIXStraddle] Open CE leg for %s — skipping", index_name)
            return

        spot = self._get_spot_from_data(index_name)
        if not spot:
            self.logger.warning("[VIXStraddle] No spot data for %s", index_name)
            return

        ce_opt = self.strike_lookup.get_atm_option(index_name, spot, "CE")
        pe_opt = self.strike_lookup.get_atm_option(index_name, spot, "PE")
        if not (ce_opt and pe_opt):
            self.logger.warning(
                "[VIXStraddle] ATM strike lookup failed for %s at spot=%.0f", index_name, spot
            )
            return

        self.logger.info(
            "[VIX STRADDLE] %s spot=%.0f | ATM=%s | pct60=%.1f pct120=%.1f | "
            "BUY %s + BUY %s",
            index_name, spot, ce_opt["strike"], pct60, pct120,
            ce_opt["trading_symbol"], pe_opt["trading_symbol"],
        )

        def _buy_leg(opt: dict, strat: str, tag: str) -> None:
            ltp      = self._get_option_ltp(
                opt["trading_symbol"], opt["expiry_date"], spot,
                security_id=opt["security_id"], exchange_segment=opt["exchange_segment"],
            )
            order_id = self.placer.place_market_order(
                security_id      = opt["security_id"],
                exchange_segment = opt["exchange_segment"],
                transaction_type = "BUY",
                quantity         = opt["lot_size"],
                ltp              = ltp,
            )
            if not order_id:  # Always paper-record when Dhan rejects
                order_id = f"SANDBOX_STRADDLE_{tag}_{int(time.time())}"
                self.logger.warning(
                    "[SANDBOX] VIXStraddle %s → paper trade %s", opt["trading_symbol"], order_id
                )
                self.placer.failed_attempts = 0
            if order_id:
                fill = ltp if (order_id or "").startswith("SANDBOX_") else self._fetch_fill_price(order_id, opt)
                self.log_to_master(index_name, "BUY", strat, order_id, opt,
                                   entry_price=fill, quantity=opt["lot_size"])
                self.logger.info(
                    "[VIX STRADDLE] %s placed — id=%s ltp=%.2f", opt["trading_symbol"], order_id, ltp
                )

        _buy_leg(ce_opt, strat_ce, "CE")
        _buy_leg(pe_opt, strat_pe, "PE")

    def execute_iron_condor(self, index_name: str, pct60: float):
        """
        Sell OTM iron condor for the IV Crush strategy (Worker X).
        Called directly by IronCondorWorker when VIX pct60 > 75 and regime = RANGING.

        Structure (all legs on nearest weekly expiry):
          SELL  CE  ~OTM_OFFSET pts above spot      (delta ≈ 0.20)
          SELL  PE  ~OTM_OFFSET pts below spot      (delta ≈ 0.20)
          BUY   CE  ~OTM_OFFSET + HEDGE_OFFSET above (caps max loss, cuts margin)
          BUY   PE  ~OTM_OFFSET + HEDGE_OFFSET below

        OTM_OFFSET = 100 pts on NIFTY (50-pt grid = 2 strikes OTM).
        HEDGE_OFFSET = 100 pts additional (so hedge CE is 200 pts OTM from ATM).
        """
        OTM_OFFSET   = 100
        HEDGE_OFFSET = 100

        strat_sc = "IronCondor_ShortC"
        strat_sp = "IronCondor_ShortP"
        strat_hc = "IronCondor_HedgeC"
        strat_hp = "IronCondor_HedgeP"

        if self._has_open_position(index_name, strat_sc):
            self.logger.debug("[IronCondor] Open short-CE leg for %s — skipping", index_name)
            return

        spot = self._get_spot_from_data(index_name)
        if not spot:
            self.logger.warning("[IronCondor] No spot data for %s", index_name)
            return

        # Shift "spot" to select OTM/hedge strikes via ATM lookup
        short_ce = self.strike_lookup.get_atm_option(index_name, spot + OTM_OFFSET,             "CE")
        short_pe = self.strike_lookup.get_atm_option(index_name, spot - OTM_OFFSET,             "PE")
        hedge_ce = self.strike_lookup.get_atm_option(index_name, spot + OTM_OFFSET + HEDGE_OFFSET, "CE")
        hedge_pe = self.strike_lookup.get_atm_option(index_name, spot - OTM_OFFSET - HEDGE_OFFSET, "PE")

        if not all([short_ce, short_pe, hedge_ce, hedge_pe]):
            self.logger.warning(
                "[IronCondor] Strike lookup incomplete for %s spot=%.0f", index_name, spot
            )
            return

        self.logger.info(
            "[IRON CONDOR] %s spot=%.0f | pct60=%.1f%% | "
            "SELL CE@%s + SELL PE@%s | HEDGE CE@%s + HEDGE PE@%s",
            index_name, spot, pct60,
            short_ce["strike"], short_pe["strike"],
            hedge_ce["strike"], hedge_pe["strike"],
        )

        def _place_leg(opt: dict, action: str, strat: str, tag: str) -> None:
            ltp = self._get_option_ltp(
                opt["trading_symbol"], opt["expiry_date"], spot,
                security_id=opt["security_id"], exchange_segment=opt["exchange_segment"],
            )
            order_id = self.placer.place_market_order(
                security_id      = opt["security_id"],
                exchange_segment = opt["exchange_segment"],
                transaction_type = action,
                quantity         = opt["lot_size"],
                ltp              = ltp,
            )
            if not order_id:  # Always paper-record when Dhan rejects
                order_id = f"SANDBOX_CONDOR_{tag}_{int(time.time())}"
                self.logger.warning(
                    "[SANDBOX] IronCondor %s %s → paper trade %s",
                    action, opt["trading_symbol"], order_id,
                )
                self.placer.failed_attempts = 0
            if order_id:
                fill = ltp if (order_id or "").startswith("SANDBOX_") else self._fetch_fill_price(order_id, opt)
                self.log_to_master(index_name, action, strat, order_id, opt,
                                   entry_price=fill, quantity=opt["lot_size"])
                self.logger.info(
                    "[IRON CONDOR] %s %s placed — id=%s ltp=%.2f",
                    action, opt["trading_symbol"], order_id, ltp,
                )

        _place_leg(short_ce, "SELL", strat_sc, "SC")
        _place_leg(short_pe, "SELL", strat_sp, "SP")
        _place_leg(hedge_ce, "BUY",  strat_hc, "HC")
        _place_leg(hedge_pe, "BUY",  strat_hp, "HP")

    def execute_intraday_theta(
        self,
        index_name: str,
        ce_opt:     dict,
        pe_opt:     dict,
        ce_ltp:     float,
        pe_ltp:     float,
        spot_range: float,
        prem_decay: float,
        intrinsic:  float,
    ):
        """
        Sell ATM CE + ATM PE for the IntradayThetaDecayWorker (OBS Bot pattern).
        Both legs use paper-order IDs prefixed SANDBOX_THETA_ when Dhan rejects.
        SL monitor tracks them as SELL positions; forced exit at 15:25 IST.
        """
        strat_ce = "ThetaDecay_ShortC"
        strat_pe = "ThetaDecay_ShortP"

        if self._has_open_position(index_name, strat_ce):
            self.logger.debug("[ThetaDecay] Already in trade for %s — skipping", index_name)
            return

        spot = self._get_spot_from_data(index_name)
        self.logger.info(
            "[THETA DECAY] %s spot=%.0f | ATM=%s | SELL %s (%.1f) + SELL %s (%.1f) | "
            "range=%.1f decay=%.1f intrinsic=%.1f",
            index_name, spot or 0, ce_opt.get("strike"),
            ce_opt["trading_symbol"], ce_ltp,
            pe_opt["trading_symbol"], pe_ltp,
            spot_range, prem_decay, intrinsic,
        )

        def _sell_leg(opt: dict, ltp: float, strat: str, tag: str) -> None:
            order_id = self.placer.place_market_order(
                security_id      = opt["security_id"],
                exchange_segment = opt["exchange_segment"],
                transaction_type = "SELL",
                quantity         = opt["lot_size"],
                ltp              = ltp,
            )
            if not order_id:
                order_id = f"SANDBOX_THETA_{tag}_{int(time.time_ns() // 1000)}"
                self.logger.warning(
                    "[SANDBOX] ThetaDecay SELL %s → paper %s", opt["trading_symbol"], order_id,
                )
                self.placer.failed_attempts = 0
            if order_id:
                fill = ltp if (order_id or "").startswith("SANDBOX_") else self._fetch_fill_price(order_id, opt)
                self.log_to_master(index_name, "SELL", strat, order_id, opt,
                                   entry_price=fill, quantity=opt["lot_size"])
                self.logger.info(
                    "[THETA DECAY] SELL %s placed — id=%s ltp=%.2f", opt["trading_symbol"], order_id, ltp,
                )

        _sell_leg(ce_opt, ce_ltp, strat_ce, "CE")
        _sell_leg(pe_opt, pe_ltp, strat_pe, "PE")

    def exit_intraday_theta(self, index_name: str):
        """
        Signal the SL monitor to exit ThetaDecay positions at 15:25 IST.
        The SL monitor's hard-cutoff already handles this automatically
        (15:25 cutoff applies to all OPEN positions), so this is a belt-and-suspenders
        log entry confirming the intent.
        """
        self.logger.info(
            "[THETA DECAY] 15:25 EXIT signal for %s — SL monitor will close via hard_cutoff",
            index_name,
        )

    def execute_weekly_strangle(
        self,
        index_name:  str,
        expiry_str:  str,       # "YYYY-MM-DD" — next week's Thursday expiry
        otm_offset:  int,       # pts OTM from ATM for both legs (≈ delta 0.25)
        on_entered,             # callback(ce_opt, pe_opt, ce_ltp, pe_ltp, expiry_str)
    ):
        """
        Sell OTM strangle (CE + PE) at the specified expiry for the weekly cycle.
        Called by WeeklyStrangleWorker on Thursday 15:25 when VIX < 20.
        Invokes on_entered() callback to store entry state in the worker.
        """
        strat_sc = "WklyStrangle_ShortC"
        strat_sp = "WklyStrangle_ShortP"

        if self._has_open_position(index_name, strat_sc):
            self.logger.debug("[WklyStrangle] Active cycle already open — skipping entry")
            return

        spot = self._get_spot_from_data(index_name)
        if not spot:
            self.logger.warning("[WklyStrangle] No spot data for %s", index_name)
            return

        ce_opt = self.strike_lookup.get_atm_option(index_name, spot + otm_offset, "CE",
                                                   expiry_date=expiry_str)
        pe_opt = self.strike_lookup.get_atm_option(index_name, spot - otm_offset, "PE",
                                                   expiry_date=expiry_str)
        if not (ce_opt and pe_opt):
            self.logger.warning(
                "[WklyStrangle] Strike lookup failed for %s spot=%.0f expiry=%s",
                index_name, spot, expiry_str,
            )
            return

        self.logger.info(
            "[WKLY STRANGLE] %s spot=%.0f | SELL CE=%s + SELL PE=%s | expiry=%s",
            index_name, spot, ce_opt["strike"], pe_opt["strike"], expiry_str,
        )

        def _sell_leg(opt: dict, strat: str, tag: str) -> tuple:
            ltp = self._get_option_ltp(
                opt["trading_symbol"], opt["expiry_date"], spot,
                security_id=opt["security_id"], exchange_segment=opt["exchange_segment"],
            )
            order_id = self.placer.place_market_order(
                security_id=opt["security_id"], exchange_segment=opt["exchange_segment"],
                transaction_type="SELL", quantity=opt["lot_size"], ltp=ltp,
            )
            if not order_id:  # Always paper-record when Dhan rejects
                order_id = f"SANDBOX_WKLY_{tag}_{int(time.time())}"
                self.logger.warning("[SANDBOX] WklyStrangle %s → paper trade %s",
                                    opt["trading_symbol"], order_id)
                self.placer.failed_attempts = 0
            if order_id:
                fill = ltp if (order_id or "").startswith("SANDBOX_") else self._fetch_fill_price(order_id, opt)
                self.log_to_master(index_name, "SELL", strat, order_id, opt,
                                   entry_price=fill, quantity=opt["lot_size"])
                self.logger.info("[WKLY STRANGLE] %s SELL placed — id=%s ltp=%.2f",
                                 opt["trading_symbol"], order_id, ltp)
            return ltp

        ce_ltp = _sell_leg(ce_opt, strat_sc, "CE")
        pe_ltp = _sell_leg(pe_opt, strat_sp, "PE")
        on_entered(ce_opt, pe_opt, ce_ltp, pe_ltp, expiry_str)

    def execute_weekly_strangle_adjust(
        self,
        index_name:  str,
        expiry_str:  str,
        old_ce_opt:  dict,
        old_pe_opt:  dict,
        otm_offset:  int,
        on_adjusted,
    ):
        """
        Close existing strangle legs (buy to cover) and sell fresh OTM strikes
        at current spot ± otm_offset. Called when either leg hits 2× entry premium.
        At most one adjustment per weekly cycle.
        """
        strat_sc = "WklyStrangle_ShortC"
        strat_sp = "WklyStrangle_ShortP"
        spot     = self._get_spot_from_data(index_name)
        if not spot:
            return

        def _close_leg(opt: dict, tag: str):
            ltp = self._get_option_ltp(
                opt["trading_symbol"], opt["expiry_date"], spot,
                security_id=opt["security_id"], exchange_segment=opt["exchange_segment"],
            )
            order_id = self.placer.place_market_order(
                security_id=opt["security_id"], exchange_segment=opt["exchange_segment"],
                transaction_type="BUY", quantity=opt["lot_size"], ltp=ltp,
            )
            if not order_id:  # Always paper-record when Dhan rejects
                order_id = f"SANDBOX_WKLY_CLOSE_{tag}_{int(time.time())}"
                self.placer.failed_attempts = 0
            self.logger.info("[WKLY STRANGLE] Closed %s (BUY) ltp=%.2f id=%s",
                             opt["trading_symbol"], ltp, order_id)

        _close_leg(old_ce_opt, "CE")
        _close_leg(old_pe_opt, "PE")

        # Reopen at current spot
        new_ce = self.strike_lookup.get_atm_option(index_name, spot + otm_offset, "CE",
                                                   expiry_date=expiry_str)
        new_pe = self.strike_lookup.get_atm_option(index_name, spot - otm_offset, "PE",
                                                   expiry_date=expiry_str)
        if not (new_ce and new_pe):
            self.logger.warning("[WKLY STRANGLE] Reopen strike lookup failed — adjustment aborted")
            return

        def _sell_new(opt: dict, tag: str) -> float:
            ltp = self._get_option_ltp(
                opt["trading_symbol"], opt["expiry_date"], spot,
                security_id=opt["security_id"], exchange_segment=opt["exchange_segment"],
            )
            order_id = self.placer.place_market_order(
                security_id=opt["security_id"], exchange_segment=opt["exchange_segment"],
                transaction_type="SELL", quantity=opt["lot_size"], ltp=ltp,
            )
            if not order_id:  # Always paper-record when Dhan rejects
                order_id = f"SANDBOX_WKLY_ADJ_{tag}_{int(time.time())}"
                self.placer.failed_attempts = 0
            if order_id:
                self.log_to_master(index_name, "SELL", strat_sc if tag == "CE" else strat_sp,
                                   order_id, opt, entry_price=ltp, quantity=opt["lot_size"])
                self.logger.info("[WKLY STRANGLE] Adjusted %s SELL ltp=%.2f id=%s",
                                 opt["trading_symbol"], ltp, order_id)
            return ltp

        new_ce_ltp = _sell_new(new_ce, "CE")
        new_pe_ltp = _sell_new(new_pe, "PE")
        on_adjusted(new_ce, new_pe, new_ce_ltp, new_pe_ltp)

    def execute_red_day_sell(
        self, index_name: str, drop_pct: float, rsi: float
    ):
        """
        Sell OTM CE on a confirmed red day (market down, RSI weak).
        Called by RedDaySellerWorker. Bypasses MetaAgent — execution is
        conditional on real-time market state already verified by the worker.

        Target 50% premium decay; SL 2× premium — consistent with the 3-rule system.
        """
        strat = "RedDaySeller_CE"

        if self._has_open_position(index_name, strat):
            self.logger.debug("[RedDaySeller] Open CE position for %s — skipping", index_name)
            return

        spot = self._get_spot_from_data(index_name)
        if not spot:
            return

        otm_offset = 100
        ce_opt = self.strike_lookup.get_atm_option(index_name, spot + otm_offset, "CE")
        if not ce_opt:
            self.logger.warning("[RedDaySeller] CE strike lookup failed — skipping")
            return

        self.logger.info(
            "[RED DAY SELL] %s spot=%.0f drop=%.2f%% RSI=%.1f | "
            "SELL CE@%s (%s)",
            index_name, spot, drop_pct, rsi,
            ce_opt["strike"], ce_opt["trading_symbol"],
        )

        ltp = self._get_option_ltp(
            ce_opt["trading_symbol"], ce_opt["expiry_date"], spot,
            security_id=ce_opt["security_id"], exchange_segment=ce_opt["exchange_segment"],
        )
        order_id = self.placer.place_market_order(
            security_id=ce_opt["security_id"], exchange_segment=ce_opt["exchange_segment"],
            transaction_type="SELL", quantity=ce_opt["lot_size"], ltp=ltp,
        )
        if not order_id:  # Always paper-record when Dhan rejects
            order_id = f"SANDBOX_REDDAYSELL_{int(time.time())}"
            self.logger.warning("[SANDBOX] RedDaySell %s → paper trade %s",
                                ce_opt["trading_symbol"], order_id)
            self.placer.failed_attempts = 0

        if order_id:
            fill = ltp if (order_id or "").startswith("SANDBOX_") else self._fetch_fill_price(order_id, ce_opt)
            self.log_to_master(index_name, "SELL", strat, order_id, ce_opt,
                               entry_price=fill, quantity=ce_opt["lot_size"])
            self.logger.info("[RED DAY SELL] Order placed — id=%s ltp=%.2f tgt=50%% SL=2×",
                             order_id, ltp)

    def execute_expiry_blast(
        self,
        spot:       float,
        rsi_before: float,
        rsi_after:  float,
        atr14:      float,
        body_pct:   float,
        move_atr:   float,
    ):
        """
        Buy NIFTY ATM CE on confirmed expiry blast (NiftyBlast1 pattern).
        Called by ExpiryBlastWorker at 15:00 candle close on expiry day.
        Always CE (call) — blast is an upside squeeze.
        1 lot; SL monitor handles exit with hard 15:25 cutoff.
        """
        strat = "ExpiryBlast"

        if self._has_open_position("NIFTY", strat):
            self.logger.debug("[ExpiryBlast] Open position exists — skip")
            return

        ce_opt = self.strike_lookup.get_atm_option("NIFTY", spot, "CE")
        if not ce_opt:
            self.logger.warning("[ExpiryBlast] ATM CE lookup failed at spot=%.0f", spot)
            return

        self.logger.info(
            "[EXPIRY BLAST] NIFTY spot=%.0f | ATM=%s (%s) | "
            "body%%=%.2f move=%.1fxATR RSI %.1f->%.1f",
            spot, ce_opt["strike"], ce_opt["trading_symbol"],
            body_pct, move_atr, rsi_before, rsi_after,
        )

        ltp = self._get_option_ltp(
            ce_opt["trading_symbol"], ce_opt["expiry_date"], spot,
            security_id=ce_opt["security_id"],
            exchange_segment=ce_opt["exchange_segment"],
        )
        order_id = self.placer.place_market_order(
            security_id      = ce_opt["security_id"],
            exchange_segment = ce_opt["exchange_segment"],
            transaction_type = "BUY",
            quantity         = ce_opt["lot_size"],
            ltp              = ltp,
        )
        if not order_id:
            order_id = f"SANDBOX_EXPBLAST_{int(time.time())}"
            self.logger.warning("[PAPER] ExpiryBlast %s -> paper trade %s",
                                ce_opt["trading_symbol"], order_id)
            self.placer.failed_attempts = 0

        if order_id:
            fill = ltp if (order_id or "").startswith("SANDBOX_") else self._fetch_fill_price(order_id, ce_opt)
            self.log_to_master("NIFTY", "BUY", strat, order_id, ce_opt,
                               entry_price=fill, quantity=ce_opt["lot_size"])
            self.logger.info(
                "[EXPIRY BLAST] Order placed — id=%s %s ltp=%.2f | EXIT at 15:25",
                order_id, ce_opt["trading_symbol"], fill,
            )

    def _get_spot_from_data(self, index_name: str) -> float:
        """Latest spot price from candle buffer (fallback 0.0)."""
        df = self.data.get(f"{index_name}_5m") or self.data.get(f"{index_name}_1m")
        if df is None or df.empty:
            return 0.0
        return float(df.iloc[-1]["close"])

    def _fetch_fill_price(self, order_id: str, option: dict | None = None) -> float:
        """Get option fill price: tries Kite 1-min LTP first, then Dhan order status.
        Dhan sandbox always returns tradedPrice=0, so Kite is the reliable path.
        Returns 0.0 only if both sources fail.
        """
        # 1. Kite 1-min last close — accurate even on Dhan sandbox
        if option:
            try:
                import sys as _sys
                _sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
                from kite_candle_store import resolve_option_token, get_candles as _kite_candles
                _BSE     = {"SENSEX", "BANKEX", "SENSEX50"}
                sym      = option.get("trading_symbol", "").split("-")[0]
                strike   = float(option.get("strike", 0))
                opt_type = option.get("trading_symbol", "").rsplit("-", 1)[-1]
                expiry   = option.get("expiry_date", "")
                if sym and strike and opt_type in ("CE", "PE") and expiry:
                    token = resolve_option_token(sym, strike, opt_type, expiry)
                    if token:
                        exchange   = "BFO" if sym in _BSE else "NFO"
                        today      = datetime.now().strftime("%Y-%m-%d")
                        tradingsym = f"{sym}_{int(strike)}_{opt_type}_{expiry}"
                        df, _      = _kite_candles(token, tradingsym, exchange, today, interval="minute")
                        if df is not None and not df.empty:
                            ltp = float(df["close"].iloc[-1])
                            if ltp > 0:
                                self.logger.debug(f"[execute] Kite LTP {tradingsym}: {ltp}")
                                return ltp
            except Exception as e:
                self.logger.debug(f"[execute] Kite LTP fetch failed: {e}")
        # 2. Dhan order status (returns 0 on sandbox, but try for live accounts)
        try:
            import time as _t; _t.sleep(0.5)
            resp  = self.client.get_order_by_id(order_id)
            data  = resp.get("data", {}) if isinstance(resp, dict) else {}
            price = float(data.get("tradedPrice", 0) or data.get("averagePrice", 0) or 0)
            if price > 0:
                self.logger.debug(f"[execute] Dhan fill price {order_id}: {price}")
                return price
        except Exception as e:
            self.logger.debug(f"[execute] fill price fetch failed for {order_id}: {e}")
        return 0.0

    def log_to_master(
        self,
        symbol:           str,
        action:           str,
        strategy:         str,
        order_id:         str,
        option:           dict,
        entry_price:      float       = 0.0,
        quantity:         int         = 1,
        strategy_context: dict | None = None,
    ):
        try:
            db_path = MasterResource.get_trading_db_path()
            conn    = sqlite3.connect(db_path)
            now     = datetime.now().isoformat()
            parsed_data = {
                "symbol":         symbol,
                "action":         action,
                "strategy":       strategy,
                "trading_symbol": option["trading_symbol"],
                "strike":         option["strike"],
                "expiry_date":    option["expiry_date"],
                "security_id":    option["security_id"],
            }
            strategy_params_json = json.dumps(strategy_context) if strategy_context else None

            # Ensure strategy_params column exists (safe migration — runs once)
            try:
                conn.execute("ALTER TABLE orders ADD COLUMN strategy_params TEXT")
                conn.commit()
            except Exception:
                pass   # column already exists

            # 1. Log to signals table (processed=1 — already handled)
            conn.execute(
                """INSERT INTO signals
                       (channel_name, raw_text, parsed_data, timestamp, processed, order_id, order_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    strategy,
                    f"Omni-Engine: {strategy} | {option['trading_symbol']}",
                    json.dumps(parsed_data),
                    now,
                    1,
                    order_id,
                    "PLACED",
                ),
            )
            # 2. Insert into orders table so the SL monitor picks it up.
            # entry_placed_at must be set here — SL monitor's phantom filter
            # skips any order where entry_placed_at IS NULL.
            conn.execute(
                """INSERT INTO orders
                       (order_id, symbol, action, quantity, entry_price, status,
                        tradingsymbol, security_id, exchange_segment,
                        strategy_name, created_at, entry_placed_at, strategy_params,
                        actual_entry_price)
                   VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    symbol,
                    action,
                    quantity,
                    entry_price,
                    option["trading_symbol"],
                    option["security_id"],
                    option["exchange_segment"],
                    strategy,
                    now,
                    now,            # entry_placed_at = same as created_at
                    strategy_params_json,
                    entry_price if entry_price else None,  # actual_entry_price = fill price
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            self.logger.error(f"Master DB log error: {e}")


if __name__ == "__main__":
    engine = DhanOmniEngine(is_sandbox=True)
    engine.run()
