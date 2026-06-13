"""
sl_monitor_with_trailing_ATR.py - Professional Grade SL Monitor with ATR

Features:
- MCX hours support (9 AM - 11:55 PM)
- NSE hours support (9:15 AM - 3:30 PM)
- ATR-based initial SL (wider stops on volatile stocks)
- Fixed 5% minimum (safety floor)
- 3% trailing after +5% profit
- Breakeven at +3% profit
- Re-entry prevention
- Works for ALL positions

PRO Features (merged from v3_PRO):
- Telegram notifications for critical alerts
- State reconciliation (detect unprotected positions)
- Forced auto-exit at end of session
- Exponential backoff on errors
- Graceful shutdown handling
"""

import json
import logging
import time
import argparse
import pytz
import os
import signal
import sys
import requests
import pandas as pd
from datetime import datetime, date, timedelta, timezone, time as dtime
from kiteconnect import KiteConnect

# 0. Add Master Hub to path for configuration and logging
import sys
from pathlib import Path
master_lib = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib"
if master_lib not in sys.path:
    sys.path.append(master_lib)
from master_resource import MasterResource, get_sl_config_path, get_sl_exits_path

# Configure logging with centralized Master Hub directory
log_ts = datetime.now().strftime('%d%b%Y_%H_%M_%S').upper()
log_dir = MasterResource.MASTER_ROOT / 'logs'
log_dir.mkdir(exist_ok=True)
log_filename = str(log_dir / f"sl_monitor_with_trailing_ATR_v2_{log_ts}.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - SL_MONITOR - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logging.info(f"[LOG] Writing to centralized Master logs: {log_filename}")

# ========================================
# TELEGRAM NOTIFICATIONS (PRO Feature)
# ========================================
TELEGRAM_TOKEN = None
TELEGRAM_CHAT_ID = None

def load_telegram_config():
    """Load Telegram credentials from Master Configuration Hub"""
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
    import sys
    from pathlib import Path
    sys.path.append(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
    from master_resource import get_kite_config

    try:
        config = get_kite_config()
        TELEGRAM_TOKEN = config.get('bot_token') or config.get('telegram_token')
        TELEGRAM_CHAT_ID = config.get('chat_id') or config.get('telegram_chat_id')

        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            logging.info("[INIT] Telegram notifications enabled via Master Hub")
            return True
        else:
            logging.warning("[INIT] Telegram not configured in Master Hub")
            return False
    except Exception as e:
        logging.warning(f"[INIT] Could not load Master Telegram config: {e}")
        return False

def send_telegram(message):
    """Send real-time mobile alerts via Telegram bot"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message}, timeout=5)
        return True
    except Exception as e:
        logging.error(f"[TELEGRAM] Failed to send: {e}")
        return False

# Load Telegram config at startup
load_telegram_config()

# ========================================
# GRACEFUL SHUTDOWN (PRO Feature)
# ========================================
_shutdown_requested = False

def request_shutdown(signum=None, frame=None):
    """Handle shutdown signals gracefully"""
    global _shutdown_requested
    if _shutdown_requested:
        logging.warning("[SHUTDOWN] Force quit...")
        sys.exit(1)
    _shutdown_requested = True
    sig_name = signal.Signals(signum).name if signum else "UNKNOWN"
    logging.info(f"\n[SHUTDOWN] Received {sig_name}, shutting down gracefully...")
    send_telegram("🛑 SL Monitor shutting down gracefully")

# Register signal handlers
if sys.platform != 'win32':
    signal.signal(signal.SIGTERM, request_shutdown)
signal.signal(signal.SIGINT, request_shutdown)

def is_market_open():
    """
    Check if market is open for trading
    NSE/NFO: 9:15 AM to 3:30 PM IST (Mon-Fri)
    MCX: 9:00 AM to 11:55 PM IST (Mon-Fri)
    """
    try:
        ist = pytz.timezone('Asia/Kolkata')
        now = datetime.now(ist)
        
        # Check if weekend
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        
        # NSE/NFO hours
        nse_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        nse_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        
        # MCX hours
        mcx_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
        mcx_close = now.replace(hour=23, minute=55, second=0, microsecond=0)
        
        nse_is_open = nse_open <= now <= nse_close
        mcx_is_open = mcx_open <= now <= mcx_close

        return nse_is_open or mcx_is_open
    except (ValueError, TypeError, AttributeError) as e:
        logging.debug(f"Market hours check error: {e}")
        return True  # Safe default - assume market is open

class EnhancedSLMonitor:
    def __init__(self, check_interval=30, test_mode=False, use_atr=True, atr_multiplier=1.5):
        self.check_interval = check_interval
        self.test_mode = test_mode
        self.use_atr = use_atr  # Toggle ATR feature
        self.atr_multiplier = atr_multiplier  # Configurable ATR multiplier
        
        # Load Kite config from Master Hub
        from master_resource import get_kite_config
        self.config = get_kite_config()
        self.kite = KiteConnect(api_key=self.config['api_key'])
        self.kite.set_access_token(self.config['access_token'])
        
        # Load SL config (ATR settings, commodity settings, etc.)
        self._load_sl_config()
        
        self.protected_positions = set()
        self.initial_sl_prices = {}
        self.sl_placement_time = {}  # {tradingsymbol: timestamp}
        self.sl_exits_today = {}     # {tradingsymbol: date_iso}
        self.atr_cache = {}          # {tradingsymbol: atr_value}
        self.exited_today = set()    # Positions force-exited at EOD
        self.last_sync = 0           # Last state reconciliation time
        self.ist = pytz.timezone('Asia/Kolkata')

        # ── CHANGE 4: Daily loss circuit breaker ──────────────────────────────
        self.daily_realised_loss = 0.0   # Running ₹ total of losses today
        self.circuit_breaker_fired = False
        self._last_known_pnl = {}        # {tradingsymbol: pnl} snapshot for exit accounting

        # PRO Settings (merged from v3_PRO)
        self.pro_rules = {
            "equity_exit": "15:25",     # Force exit NSE/NFO positions
            "commodity_exit": "23:25",  # Force exit MCX positions
            "sync_interval": 300,       # State reconciliation every 5 minutes
            "commodities": ["CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER", "ZINC", "NICKEL", "LEAD", "ALUMINIUM"]
        }

        self._load_sl_exits()
        
        logging.info(f"[INIT] SL Monitor started (Mode: {'TEST' if test_mode else 'LIVE'})")
        logging.info(f"[INIT] Market hours: NSE 9:15-15:30, MCX 9:00-23:55")
        logging.info(f"[INIT] ATR Enhancement: {'ENABLED' if use_atr else 'DISABLED'}")
        if use_atr:
            logging.info(f"[INIT] ATR Multiplier: {self.atr_multiplier}x")
            logging.info(f"[INIT] ATR cap for options: {self.sl_config.get('atr_max_pct_options', 8.0)}%  [CHANGE 1]")
            logging.info(f"[INIT] Commodities SL: {self.sl_config['commodity_sl_pct']}%")
            logging.info(f"[INIT] Stocks/Indices SL: {self.sl_config['default_sl_pct']}%")
        cooldown = self.sl_config.get('reentry_cooldown_minutes', 120)
        logging.info(f"[INIT] SL blacklist cooldown: {'all day' if cooldown == 0 else f'{cooldown} min'}  [CHANGE 3]")
        daily_limit = self.sl_config.get('daily_loss_limit', 10000)
        logging.info(f"[INIT] Daily loss circuit breaker: ₹{daily_limit:,.0f}  [CHANGE 4]")
        logging.info(f"[INIT] Trailing SL: 3.0% AM (before 13:00) | 1.5% PM (after 13:00)  [CHANGE 2]")
        logging.info(f"[INIT] Works for ALL positions (manual + automatic)")
        logging.info(f"[INIT] PRO Features: Telegram alerts, State reconciliation, Forced exit, Exponential backoff")
        logging.info(f"[INIT] Forced exit times: Equity {self.pro_rules['equity_exit']}, MCX {self.pro_rules['commodity_exit']}")

    def _load_sl_config(self):
        """Load SL configuration from file or use defaults"""
        default_config = {
            "default_sl_pct": 5.0,      # 5% for stocks/indices
            "commodity_sl_pct": 4.0,    # 4% for commodities (avoid circuit limits)
            "atr_multiplier": self.atr_multiplier,  # ATR multiplier (1.5x default)
            "atr_period": 14,            # ATR calculation period
            "atr_candle_interval": "5minute",  # Candle interval for ATR
            "atr_lookback_hours": 2,     # Hours of historical data
            "atr_cache_minutes": 5,      # Cache ATR for N minutes
            "commodities": [             # List of commodity keywords
                "CRUDE", "CRUDEOIL", "GOLD", "SILVER", "COPPER",
                "ZINC", "NICKEL", "LEAD", "ALUMINIUM", "NATURALGAS"
            ],

            # ── CHANGE 1: Cap ATR SL width for options ────────────────────────
            # ATR is computed on the UNDERLYING (NIFTY spot / stock price), not
            # on the option premium. Applied raw to a cheap OTM premium it
            # produces 15-21% stops that allow the option to lose nearly all
            # value before the SL fires. Cap: the ATR-derived SL will never be
            # more than this % away from the option's entry price.
            # Set to 0 to disable the cap and restore the old uncapped behaviour.
            "atr_max_pct_options": 8.0,  # max ATR SL width for CE/PE options (%)

            # ── CHANGE 3: Blacklist duration after SL hit ─────────────────────
            # 0 = block all day | N = block for N minutes after SL hit
            # Was 30 min — raised to 120 min so the same instrument cannot be
            # re-entered within 2 hours of being stopped out.
            "reentry_cooldown_minutes": 120,

            # ── CHANGE 4: Daily max-loss circuit breaker ──────────────────────
            # Once total realised losses for the day exceed this amount (₹),
            # the SL monitor will refuse to place any further new SL orders,
            # effectively halting all new position protection.
            # Set to 0 to disable the circuit breaker.
            "daily_loss_limit": 10000,  # ₹10,000 daily loss limit
        }
        
        try:
            sl_config_path = get_sl_config_path()
            if os.path.exists(sl_config_path):
                with open(sl_config_path, 'r') as f:
                    user_config = json.load(f)
                    # Merge with defaults (user config overrides)
                    self.sl_config = {**default_config, **user_config}
                    # Update ATR multiplier from user config
                    if 'atr_multiplier' in user_config:
                        self.atr_multiplier = user_config['atr_multiplier']
                    logging.info(f"[INIT] Loaded custom SL config from {sl_config_path}")
            else:
                self.sl_config = default_config
                # Save default config for user to edit
                with open(sl_config_path, 'w') as f:
                    json.dump(default_config, f, indent=2)
                logging.info(f"[INIT] Created default sl_config.json at {sl_config_path} - customize as needed!")
        except Exception as e:
            logging.warning(f"[INIT] Could not load sl_config.json: {e}, using defaults")
            self.sl_config = default_config

    def _load_sl_exits(self):
        """Load SL exits from file to persist across restarts"""
        try:
            sl_exits_path = get_sl_exits_path()
            if os.path.exists(sl_exits_path):
                with open(sl_exits_path, 'r') as f:
                    data = json.load(f)
                    today = date.today().isoformat()
                    # Keep today's exits; handle both old str format and new dict format
                    self.sl_exits_today = {
                        k: v for k, v in data.items()
                        if (v == today if isinstance(v, str) else v.get('date') == today)
                    }
                    if self.sl_exits_today:
                        logging.info(f"[INIT] Loaded {len(self.sl_exits_today)} SL exits from today")
                        for symbol in self.sl_exits_today:
                            logging.info(f"  - {symbol} (blocked from re-entry)")
        except Exception as e:
            logging.warning(f"[INIT] Could not load sl_exits: {e}")

    def _refresh_sl_exits_from_file(self):
        """
        FIX: Re-read sl_exits.json on every check cycle so the SL monitor
        always has the latest blacklist - even entries added AFTER startup.

        Previously sl_exits_today was only loaded at __init__, so positions
        that hit SL later in the day were written to the file by record_sl_exit()
        but the ORDER PLACER's in-memory copy was never updated -> re-entries
        were allowed when they shouldn't be (observed 23-Feb: GOLD165000CE
        blacklisted at 11:24 but re-entered at 20:19).
        """
        try:
            sl_exits_path = get_sl_exits_path()
            if os.path.exists(sl_exits_path):
                with open(sl_exits_path, 'r') as f:
                    data = json.load(f)
                    today = date.today().isoformat()
                    fresh = {
                        k: v for k, v in data.items()
                        if (v == today if isinstance(v, str) else v.get('date') == today)
                    }
                    new_entries = set(fresh.keys()) - set(self.sl_exits_today.keys())
                    if new_entries:
                        logging.info(f"[BLACKLIST-REFRESH] {len(new_entries)} new SL exits loaded: {new_entries}")
                    self.sl_exits_today = fresh
        except Exception:
            pass  # Silent - don't disrupt main loop

    def _save_sl_exits(self):
        """Save SL exits to file"""
        try:
            with open(get_sl_exits_path(), 'w') as f:
                json.dump(self.sl_exits_today, f, indent=2)
        except Exception as e:
            logging.warning(f"[WARN] Could not save sl_exits: {e}")

    def record_sl_exit(self, tradingsymbol):
        """Record SL hit with timestamp so cooldown-based re-entry can be checked."""
        today = date.today().isoformat()
        self.sl_exits_today[tradingsymbol] = {
            "date": today,
            "sl_time": datetime.now().isoformat(timespec='seconds')
        }
        self._save_sl_exits()
        cooldown = self.sl_config.get('reentry_cooldown_minutes', 120)
        cooldown_str = "all day" if cooldown == 0 else f"{cooldown} min"
        logging.warning(f"[BLACKLIST] {tradingsymbol} hit SL - re-entry blocked for {cooldown_str}")

        # ── CHANGE 4: Accumulate realised loss and check circuit breaker ──────
        # Use the last PnL snapshot captured during the monitoring loop.
        # PnL is negative for a loss; convert to a positive loss amount.
        last_pnl = self._last_known_pnl.pop(tradingsymbol, None)
        if last_pnl is not None and last_pnl < 0:
            loss_amount = abs(last_pnl)
            self.daily_realised_loss += loss_amount
            daily_limit = self.sl_config.get('daily_loss_limit', 10000)
            logging.warning(
                f"[LOSS-TRACKER] {tradingsymbol} exited at loss ₹{loss_amount:,.0f} | "
                f"Day total: ₹{self.daily_realised_loss:,.0f} / limit ₹{daily_limit:,.0f}"
            )
            if daily_limit > 0 and self.daily_realised_loss >= daily_limit and not self.circuit_breaker_fired:
                self.circuit_breaker_fired = True
                msg = (
                    f"🚨 CIRCUIT BREAKER FIRED — daily loss ₹{self.daily_realised_loss:,.0f} "
                    f"reached limit ₹{daily_limit:,.0f}. "
                    f"No new SL orders will be placed for the rest of the day."
                )
                logging.warning(f"[CIRCUIT BREAKER] {msg}")
                send_telegram(msg)
        # ─────────────────────────────────────────────────────────────────────

    def is_blocked_from_reentry(self, tradingsymbol):
        """Check if instrument is still within the re-entry cooldown window after an SL hit."""
        if tradingsymbol not in self.sl_exits_today:
            return False

        entry = self.sl_exits_today[tradingsymbol]
        today = date.today().isoformat()
        cooldown = self.sl_config.get('reentry_cooldown_minutes', 120)

        # Parse both old format (plain date string) and new format (dict with sl_time)
        if isinstance(entry, str):
            date_iso, sl_time_str = entry, None
        else:
            date_iso, sl_time_str = entry.get('date'), entry.get('sl_time')

        if date_iso != today:
            return False  # Yesterday's entry - not blocked

        if cooldown == 0 or not sl_time_str:
            return True   # All-day block (cooldown=0 or old format with no timestamp)

        elapsed_min = (datetime.now() - datetime.fromisoformat(sl_time_str)).total_seconds() / 60
        blocked = elapsed_min < cooldown
        if not blocked:
            logging.info(f"[REENTRY-OK] {tradingsymbol} cooldown expired ({elapsed_min:.0f}/{cooldown} min)")
        return blocked

    # ========================================
    # PRO FEATURES: State Reconciliation & Forced Exit
    # ========================================

    def get_ist_now(self):
        """Get current time in IST"""
        return datetime.now(self.ist)

    def get_order_variety(self, exchange):
        """Return VARIETY_REGULAR if exchange is currently open, else VARIETY_AMO.
        NSE/NFO/BFO: 09:15–15:30 IST | MCX: 09:00–23:55 IST
        """
        IST = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(IST).time()
        if exchange == 'MCX':
            is_open = dtime(9, 0) <= now <= dtime(23, 55)
        else:  # NSE, NFO, BFO
            is_open = dtime(9, 15) <= now <= dtime(15, 30)
        variety = self.kite.VARIETY_REGULAR if is_open else self.kite.VARIETY_AMO
        logging.info(f"[VARIETY] Exchange: {exchange} | Market open: {is_open} | Variety: {'REGULAR' if is_open else 'AMO'}")
        return variety

    def is_forced_exit_time(self, pos):
        """Check if segment-specific square-off time has passed.

        CHANGE 2: CNC/delivery positions are NEVER force-exited.
        All other equity/options positions are exited at 15:25 IST.
        MCX positions at 23:25 IST.
        """
        # Never force-exit delivery positions
        if pos.get('product') == 'CNC':
            return False

        symbol = pos['tradingsymbol']
        now = self.get_ist_now()
        is_mcx = any(c in symbol.upper() for c in self.pro_rules['commodities'])
        target = self.pro_rules['commodity_exit'] if is_mcx else self.pro_rules['equity_exit']
        h, m = map(int, target.split(':'))
        return now >= now.replace(hour=h, minute=m, second=0)

    def reconcile_state(self):
        """
        STATE RECONCILIATION: Cross-check Broker Orders vs Positions
        Finds positions without SL orders and places backup SLs
        """
        try:
            positions = self.kite.positions()['net']
            orders = self.kite.orders()

            # Find symbols with pending SL orders
            active_sl_symbols = [
                o['tradingsymbol'] for o in orders
                if o['status'] in ['TRIGGER PENDING', 'OPEN'] and o['order_type'] in ['SL', 'SL-M']
            ]

            unprotected_count = 0
            for pos in [p for p in positions if p['quantity'] != 0]:
                symbol = pos['tradingsymbol']
                # CHANGE 1: Never attempt backup SL for CNC delivery positions
                if pos.get('product') == 'CNC':
                    continue
                if symbol not in active_sl_symbols:
                    unprotected_count += 1
                    logging.warning(f"[RECONCILE] {symbol} is UNPROTECTED. Placing backup SL.")
                    send_telegram(f"⚠️ Security Alert: {symbol} had no SL order. Placing one now.")
                    self.place_sl_order(pos)
                    self.protected_positions.add(symbol)

            if unprotected_count == 0:
                logging.info(f"[RECONCILE] All {len([p for p in positions if p['quantity'] != 0])} positions are protected ✓")

            self.last_sync = time.time()
        except Exception as e:
            logging.error(f"[SYNC ERROR] Could not reconcile state: {e}")

    def execute_square_off(self, pos):
        """
        Forced Market Exit: Cancel pending SLs and exit at market
        Used for mandatory end-of-session square-off
        """
        symbol = pos['tradingsymbol']
        try:
            # 1. Cancel all pending SLs for this symbol
            orders = self.kite.orders()
            for o in orders:
                if o['tradingsymbol'] == symbol and o['status'] == 'TRIGGER PENDING':
                    try:
                        self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=o['order_id'])
                        logging.info(f"[CANCEL] Cancelled SL order {o['order_id']} for {symbol}")
                    except Exception as cancel_err:
                        logging.warning(f"[CANCEL] Could not cancel {o['order_id']}: {cancel_err}")

            # 2. Market Exit
            product_type = pos.get('product', 'MIS')
            if product_type == 'NRML':
                kite_product = self.kite.PRODUCT_NRML
            elif product_type == 'MIS':
                kite_product = self.kite.PRODUCT_MIS
            else:
                kite_product = self.kite.PRODUCT_MIS

            if not self.test_mode:
                order_id = self.kite.place_order(
                    variety=self.get_order_variety(pos['exchange']),
                    exchange=pos['exchange'],
                    tradingsymbol=symbol,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL if pos['quantity'] > 0 else self.kite.TRANSACTION_TYPE_BUY,
                    quantity=abs(pos['quantity']),
                    product=kite_product,
                    order_type=self.kite.ORDER_TYPE_MARKET
                )
                logging.warning(f"[FORCED EXIT] {symbol} closed at market | Order: {order_id}")
                send_telegram(f"🛑 MANDATORY EXIT: Closed {symbol} at end-of-session")
            else:
                logging.info(f"[TEST] Would force exit {symbol} at market")

        except Exception as e:
            error_msg = f"❌ ERROR: Failed to force-close {symbol}: {e}"
            logging.error(error_msg)
            send_telegram(error_msg)

    def get_atr(self, instrument_token, tradingsymbol):
        """
        Calculate ATR using configurable settings
        Default: 5-minute candles over 2 hours, 14-period ATR
        """
        try:
            # Check cache first (configurable cache duration)
            cache_seconds = self.sl_config['atr_cache_minutes'] * 60
            if tradingsymbol in self.atr_cache:
                cached_time, cached_atr = self.atr_cache[tradingsymbol]
                if (time.time() - cached_time) < cache_seconds:
                    return cached_atr
            
            # Fetch historical data based on config
            to_date = datetime.now()
            from_date = to_date - timedelta(hours=self.sl_config['atr_lookback_hours'])
            
            data = self.kite.historical_data(
                instrument_token, 
                from_date, 
                to_date, 
                self.sl_config['atr_candle_interval']
            )
            
            atr_period = self.sl_config['atr_period']
            if len(data) < atr_period:
                logging.debug(f"[ATR] Insufficient data for {tradingsymbol} ({len(data)} candles, need {atr_period})")
                return None
            
            # Calculate True Range
            df = pd.DataFrame(data)
            df['h-l'] = df['high'] - df['low']
            df['h-pc'] = abs(df['high'] - df['close'].shift(1))
            df['l-pc'] = abs(df['low'] - df['close'].shift(1))
            df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
            
            # N-period ATR (configurable)
            atr = df['tr'].tail(atr_period).mean()
            
            # Cache it
            self.atr_cache[tradingsymbol] = (time.time(), atr)
            
            logging.debug(f"[ATR] {tradingsymbol} = {atr:.2f} ({atr_period}-period on {self.sl_config['atr_candle_interval']})")
            return atr
            
        except Exception as e:
            logging.debug(f"[ATR] Failed for {tradingsymbol}: {e}")
            return None

    def _is_futures_position(self, tradingsymbol):
        """Detect futures contracts (no CE/PE suffix, product is NRML/MIS).
        CHANGE 3: Futures need ATR+swing SL, not a flat % (exposure too large).
        """
        ts = tradingsymbol.upper()
        return not ts.endswith('CE') and not ts.endswith('PE')

    def calculate_futures_sl(self, position):
        """CHANGE 3: ATR + recent swing low/high SL for futures contracts.

        Fetches 15-min candles, computes 14-period ATR and the 5-candle
        swing low (for longs) / swing high (for shorts), then uses:
            SL = max(swing_low - 0.25*ATR, ltp - 1.5*ATR)
        This prevents the 1%-of-futures = massive-rupee-loss problem.
        Returns (sl_price, method_label).
        """
        tradingsymbol = position['tradingsymbol']
        ltp = abs(position.get('last_price', position['average_price']))
        is_long = position['quantity'] > 0
        instrument_token = position['instrument_token']

        try:
            from datetime import timedelta
            to_date = datetime.now()
            from_date = to_date - timedelta(days=2)

            candles = self.kite.historical_data(
                instrument_token, from_date, to_date, '15minute'
            )

            if len(candles) < 10:
                raise ValueError(f"Only {len(candles)} candles — need at least 10")

            df = pd.DataFrame(candles)
            df['h-l'] = df['high'] - df['low']
            df['h-pc'] = abs(df['high'] - df['close'].shift(1))
            df['l-pc'] = abs(df['low'] - df['close'].shift(1))
            df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
            atr = df['tr'].tail(14).mean()

            # Swing reference from last 5 candles
            recent_low  = df['low'].tail(5).min()
            recent_high = df['high'].tail(5).max()

            if is_long:
                swing_sl = recent_low - (atr * 0.25)
                atr_sl   = ltp - (atr * 1.5)
                sl_price = max(swing_sl, atr_sl)   # less aggressive wins
            else:
                swing_sl = recent_high + (atr * 0.25)
                atr_sl   = ltp + (atr * 1.5)
                sl_price = min(swing_sl, atr_sl)

            tick = self._get_tick_size(tradingsymbol, position.get('exchange', 'NFO'))
            sl_price = round(round(sl_price / tick) * tick, 2)

            pct = abs(ltp - sl_price) / ltp * 100
            logging.info(
                f"   [FUTURES ATR SL] {tradingsymbol} | ATR: {atr:.1f} | "
                f"SwingRef: {recent_low if is_long else recent_high:.1f} | "
                f"Final SL: {sl_price:.2f} ({pct:.1f}%)"
            )
            return sl_price, f"FUTURES_ATR_{pct:.1f}%"

        except Exception as e:
            # Fallback: tight 0.5% SL so we don't place a runaway stop
            logging.warning(f"   [FUTURES ATR SL] Fallback for {tradingsymbol}: {e}")
            pct = 0.5
            sl_price = ltp * (1 - pct / 100) if is_long else ltp * (1 + pct / 100)
            tick = self._get_tick_size(tradingsymbol, position.get('exchange', 'NFO'))
            sl_price = round(round(sl_price / tick) * tick, 2)
            return sl_price, f"FUTURES_FALLBACK_{pct}%"

    def calculate_initial_sl(self, position):
        """Calculate initial SL using ATR + Fixed minimum.
        CHANGE 3: Futures contracts use ATR+swing SL (not flat %).
        """
        tradingsymbol = position['tradingsymbol']
        avg_price = abs(position['average_price'])
        is_long = position['quantity'] > 0

        # ── CHANGE 3: Route futures to dedicated ATR+swing calculator ──
        if self._is_futures_position(tradingsymbol):
            return self.calculate_futures_sl(position)
        # ───────────────────────────────────────────────────────────────

        # Check if commodity
        is_commodity = any(keyword in tradingsymbol.upper() for keyword in self.sl_config['commodities'])
        
        # Fixed % SL (baseline)
        sl_pct = self.sl_config['commodity_sl_pct'] if is_commodity else self.sl_config['default_sl_pct']
        sl_factor = (100 - sl_pct) / 100 if is_long else (100 + sl_pct) / 100
        fixed_sl = avg_price * sl_factor
        
        # Try ATR if enabled
        if self.use_atr:
            try:
                atr = self.get_atr(position['instrument_token'], tradingsymbol)
                
                if atr and atr > 0:
                    # Calculate ATR-based SL (configurable multiplier)
                    atr_distance = atr * self.atr_multiplier
                    
                    if is_long:
                        atr_sl = avg_price - atr_distance
                    else:
                        atr_sl = avg_price + atr_distance
                    
                    # ── CHANGE 1: Cap ATR SL width for CE/PE options ──────────
                    # ATR is derived from the underlying (e.g. NIFTY spot), not
                    # the option premium. The same ₹ distance is a much larger %
                    # of a cheap OTM premium → uncapped it produces 15-21% stops
                    # that let the option lose nearly all value before firing.
                    # Cap the ATR SL so it is never more than atr_max_pct_options
                    # away from the entry price.
                    is_option = tradingsymbol.upper().endswith('CE') or tradingsymbol.upper().endswith('PE')
                    max_pct = self.sl_config.get('atr_max_pct_options', 8.0)
                    if is_option and max_pct > 0:
                        if is_long:
                            floor_sl = avg_price * (1 - max_pct / 100)
                            if atr_sl < floor_sl:
                                raw_pct = (avg_price - atr_sl) / avg_price * 100
                                atr_sl = floor_sl
                                logging.info(
                                    f"   [ATR-CAP] {tradingsymbol}: raw ATR SL was {raw_pct:.1f}%, "
                                    f"capped at {max_pct}% → ₹{atr_sl:.2f}"
                                )
                        else:
                            ceil_sl = avg_price * (1 + max_pct / 100)
                            if atr_sl > ceil_sl:
                                raw_pct = (atr_sl - avg_price) / avg_price * 100
                                atr_sl = ceil_sl
                                logging.info(
                                    f"   [ATR-CAP] {tradingsymbol}: raw ATR SL was {raw_pct:.1f}%, "
                                    f"capped at {max_pct}% → ₹{atr_sl:.2f}"
                                )
                    # ─────────────────────────────────────────────────────────
                    
                    # Use ATR if it's WIDER than fixed %
                    # (gives more room on volatile stocks)
                    if is_long:
                        if atr_sl < fixed_sl:  # ATR is wider (lower SL)
                            sl_pct_actual = ((avg_price - atr_sl) / avg_price) * 100
                            logging.info(f"   [ATR] Using ATR-based SL: {atr_sl:.2f} ({sl_pct_actual:.1f}% = {self.atr_multiplier}x ATR vs {sl_pct}% fixed)")
                            return (atr_sl, f"ATR_{sl_pct_actual:.1f}%")
                    else:
                        if atr_sl > fixed_sl:  # ATR is wider (higher SL)
                            sl_pct_actual = ((atr_sl - avg_price) / avg_price) * 100
                            logging.info(f"   [ATR] Using ATR-based SL: {atr_sl:.2f} ({sl_pct_actual:.1f}% = {self.atr_multiplier}x ATR vs {sl_pct}% fixed)")
                            return (atr_sl, f"ATR_{sl_pct_actual:.1f}%")
            except Exception as e:
                logging.debug(f"[ATR] Failed, falling back to {sl_pct}%: {e}")
        
        # Fallback to fixed % (4% for commodities, 5% for others)
        commodity_label = f"COMMODITY_{sl_pct}%" if is_commodity else f"FIXED_{sl_pct}%"
        return (fixed_sl, commodity_label)

    def calculate_trailing_sl(self, position, current_price, current_sl_trigger):
        """
        BALANCED SL SYSTEM:
        - Initial: ATR-based (capped at 8% for options) or 5% fixed (whichever is wider)
        - Breakeven: At +3% profit
        - Trailing (CHANGE 2):
            Before 13:00 IST  →  3.0% below LTP  (opening momentum, give room)
            After  13:00 IST  →  1.5% below LTP  (afternoon chop, lock in profits)
        """
        tradingsymbol = position['tradingsymbol']
        quantity = position['quantity']
        avg_price = abs(position['average_price'])
        is_long = quantity > 0
        
        # Calculate percentage from entry
        if is_long:
            pnl_percent = ((current_price - avg_price) / avg_price) * 100
        else:
            pnl_percent = ((avg_price - current_price) / avg_price) * 100
        
        # Store initial SL (ATR or 5%)
        if tradingsymbol not in self.initial_sl_prices:
            initial_sl, method = self.calculate_initial_sl(position)
            self.initial_sl_prices[tradingsymbol] = initial_sl
            logging.info(f"   [INIT SL] {tradingsymbol}: ₹{initial_sl:.2f} ({method})")
        
        initial_sl = self.initial_sl_prices[tradingsymbol]
        
        # Rule 1: Below +3% profit -> Keep initial SL
        if pnl_percent < 3:
            return (initial_sl, "INITIAL")
        
        # Rule 2: At +3% to +5% profit -> Move to breakeven
        if 3 <= pnl_percent < 5:
            tick = self._get_tick_size(tradingsymbol, position.get('exchange', 'NFO'))
            new_sl = round(round(avg_price / tick) * tick, 2)
            if (is_long and new_sl > current_sl_trigger) or (not is_long and new_sl < current_sl_trigger):
                return (new_sl, "BREAKEVEN")
            return (current_sl_trigger, "NO_CHANGE")
        
        # Rule 3: Above +5% profit -> Trail (time-of-day aware)  [CHANGE 2]
        if pnl_percent >= 5:
            # ── CHANGE 2: tighter trail in afternoon session ──────────────────
            now_ist = self.get_ist_now()
            if now_ist.hour >= 13:
                trail_factor = 0.015   # 1.5% trail after 13:00 – chop protection
                trail_label  = "TRAILING_PM"
            else:
                trail_factor = 0.030   # 3.0% trail during morning session
                trail_label  = "TRAILING"
            # ─────────────────────────────────────────────────────────────────

            tick = self._get_tick_size(tradingsymbol, position.get('exchange', 'NFO'))
            calculated_sl = (current_price * (1 - trail_factor) if is_long
                             else current_price * (1 + trail_factor))
            calculated_sl = round(round(calculated_sl / tick) * tick, 2)
            
            # Floor at entry price (breakeven)
            if (is_long and calculated_sl < avg_price) or (not is_long and calculated_sl > avg_price):
                calculated_sl = round(round(avg_price / tick) * tick, 2)
            
            should_trail = (is_long and calculated_sl > current_sl_trigger) or (not is_long and calculated_sl < current_sl_trigger)
            
            if should_trail:
                # 30-second transition grace period
                if tradingsymbol in self.sl_placement_time:
                    if (time.time() - self.sl_placement_time[tradingsymbol]) <= 30:
                        return (calculated_sl, "TRANSITION")
                return (calculated_sl, trail_label)
        
        return (current_sl_trigger, "NO_CHANGE")

    def _get_tick_size(self, tradingsymbol, exchange):
        """
        Return correct tick size for rounding SL/limit prices.
        GOLD/SILVER options: ₹1 tick. NatGas: ₹0.10. Base metals/default: ₹0.05.
        Using the wrong tick causes order rejection ("invalid price").
        """
        ts = tradingsymbol.upper()
        if exchange == 'MCX' or any(c in ts for c in self.pro_rules.get('commodities', [])):
            if ts.startswith('CRUDEOIL') or ts.startswith('CRUDE'):
                return 1.0
            elif ts.startswith('GOLD') or ts.startswith('SILVER'):
                return 1.0
            elif ts.startswith('NATURALGAS') or ts.startswith('NATGAS'):
                return 0.10
            else:
                return 0.05   # COPPER, ZINC, ALUMINIUM, LEAD, NICKEL
        return 0.05            # NFO/BFO equity options

    def _use_slm_order(self, tradingsymbol, exchange):
        """
        Zerodha does NOT allow SL-M for commodity OPTIONS (only for futures).
        Attempting SL-M on MCX options gives:
          "SL-M orders are blocked for commodity options."

        So we ALWAYS use SL-L, but with a WIDE limit buffer for MCX:
          - MCX options: limit = trigger * 0.85  (15% gap protection)
          - NFO options: limit = trigger - 1 tick (normal, liquid market)

        The wide buffer means the order will execute even if price gaps 15%
        through the trigger, which covers realistic GOLD/SILVER gap scenarios.
        """
        return False  # Always SL-L; buffer width is controlled in place_sl_order

    def _sl_limit_buffer(self, tradingsymbol, exchange, trigger_price, is_long):
        """
        Calculate limit price for SL-L orders.

        MCX commodity options gap heavily on international news.
        A tight 1-tick buffer gets REJECTED when market is far below trigger.
        Use 15% buffer for MCX so limit is always reachable even after a big gap.

        Example: GOLD trigger=959, limit=959*0.85=815 -> survives even a 15% crash.
        NFO options are liquid - 1 tick is fine.
        """
        import re
        mcx_syms = {'GOLD', 'SILVER', 'CRUDEOIL', 'NATURALGAS',
                    'COPPER', 'ZINC', 'ALUMINIUM', 'LEAD', 'NICKEL'}
        base = re.match(r'^([A-Z]+)', tradingsymbol.upper())
        is_mcx = (exchange == 'MCX') or (base and base.group(1) in mcx_syms)

        tick = self._get_tick_size(tradingsymbol, exchange)

        if is_mcx:
            # Wide buffer: 15% below trigger for sells, 15% above for buys
            gap_factor = 0.85 if is_long else 1.15
            raw = trigger_price * gap_factor
            # Round to tick
            limit = round(round(raw / tick) * tick, 2)
            return limit
        else:
            # NFO: 1 tick buffer
            return round((trigger_price - tick) if is_long else (trigger_price + tick), 2)

    def place_sl_order(self, position):
        """Place stop-loss order (BROKER SL - not software!)

        FIX 1: Uses SL-M (market) for MCX/GOLD/SILVER - prevents REJECTION
                when price gaps through the trigger. SL-L was failing on GOLD
                because the limit price ended up above the gapped-down market.
        FIX 2: Tick-aware rounding using _get_tick_size().
        """
        try:
            tradingsymbol = position['tradingsymbol']
            quantity = abs(position['quantity'])
            avg_price = abs(position['average_price'])
            exchange = position['exchange']
            product_type = position.get('product', 'MIS')
            is_long = position['quantity'] > 0
            transaction_type = self.kite.TRANSACTION_TYPE_SELL if is_long else self.kite.TRANSACTION_TYPE_BUY

            # Get correct tick size for this instrument
            tick = self._get_tick_size(tradingsymbol, exchange)

            # Calculate initial SL (ATR or fixed %)
            sl_price, method = self.calculate_initial_sl(position)
            sl_price = round(round(sl_price / tick) * tick, 2)

            # ISSUE 2 FIX: If LTP has already fallen below our calculated SL
            # (e.g. position entered during volatility and SL monitor first sees it
            # after price has moved), use 95% of current LTP as the SL instead.
            # This ensures we can always place the SL without rejection.
            current_ltp = abs(position.get('last_price', avg_price))
            if is_long and sl_price >= current_ltp:
                fallback_sl = round(round(current_ltp * 0.95 / tick) * tick, 2)
                logging.warning(f"   [SL ADJUST] Calculated SL ₹{sl_price} >= LTP ₹{current_ltp}. "
                                f"Using 95% of LTP: ₹{fallback_sl}")
                sl_price = fallback_sl
                method = method + "_ADJUSTED"
            elif not is_long and sl_price <= current_ltp:
                fallback_sl = round(round(current_ltp * 1.05 / tick) * tick, 2)
                logging.warning(f"   [SL ADJUST] Calculated SL ₹{sl_price} <= LTP ₹{current_ltp}. "
                                f"Using 105% of LTP: ₹{fallback_sl}")
                sl_price = fallback_sl
                method = method + "_ADJUSTED"

            # Always SL-L (Zerodha blocks SL-M for commodity options)
            # Wide 15% buffer for MCX to survive gap moves; 1-tick for NFO
            limit_price = self._sl_limit_buffer(tradingsymbol, exchange, sl_price, is_long)

            # Convert product type to Kite constant
            if product_type == 'NRML':
                kite_product = self.kite.PRODUCT_NRML
            elif product_type == 'MIS':
                kite_product = self.kite.PRODUCT_MIS
            else:
                kite_product = self.kite.PRODUCT_MIS  # Fallback

            logging.info(f"   [PRODUCT] {product_type} | Entry: ₹{avg_price:.2f} | SL: ₹{sl_price:.2f} ({method}) | Type: SL-L | Limit: ₹{limit_price:.2f}")

            if not self.test_mode:
                order_id = self.kite.place_order(
                    variety=self.get_order_variety(exchange),
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    product=kite_product,
                    order_type=self.kite.ORDER_TYPE_SL,
                    trigger_price=sl_price,
                    price=limit_price
                )
                self.sl_placement_time[tradingsymbol] = time.time()
                logging.info(f"   [✓ PLACED] SL-L order | Trigger: ₹{sl_price:.2f} | Limit: ₹{limit_price:.2f} | Order ID: {order_id}")
                send_telegram(f"🛡️ SL Placed: {tradingsymbol} @ ₹{sl_price:.2f} ({method}) [Limit: ₹{limit_price:.2f}]")
                return order_id
            else:
                logging.info(f"   [TEST] Would place SL-L trigger=₹{sl_price:.2f} limit=₹{limit_price:.2f}")
                return f"TEST_{tradingsymbol}"
        except Exception as e:
            logging.error(f"   [✗ FAILED] Could not place SL: {e}")
            return None

    def get_existing_sl_order(self, tradingsymbol):
        try:
            orders = self.kite.orders()
            return next((o for o in orders if o['tradingsymbol'] == tradingsymbol and o['order_type'] in ['SL', 'SL-M'] and o['status'] in ['TRIGGER PENDING', 'OPEN']), None)
        except Exception as e:
            logging.error(f"[ERROR] Failed to get orders: {e}")
            return None

    def modify_sl_order(self, order_id, tradingsymbol, new_sl_price, quantity, transaction_type, exchange, sl_type="TRAILING"):
        try:
            tick = self._get_tick_size(tradingsymbol, exchange)
            new_sl_price = round(round(new_sl_price / tick) * tick, 2)
            is_long = (transaction_type == self.kite.TRANSACTION_TYPE_SELL)
            limit_price = self._sl_limit_buffer(tradingsymbol, exchange, new_sl_price, is_long)
            if not self.test_mode:
                self.kite.modify_order(
                    variety=self.kite.VARIETY_REGULAR,
                    order_id=order_id,
                    trigger_price=new_sl_price,
                    price=limit_price
                )
                self.sl_placement_time[tradingsymbol] = time.time()  # Reset grace period
                logging.info(f"   [✓ MODIFIED] Order ID: {order_id} | New SL: {new_sl_price:.2f}")
                # Telegram alert for significant SL changes (breakeven/trailing)
                if sl_type == "BREAKEVEN":
                    send_telegram(f"🟡 BREAKEVEN: {tradingsymbol} SL moved to ₹{new_sl_price:.2f}")
                elif sl_type in ("TRAILING", "TRAILING_PM"):
                    label = "TRAILING AM" if sl_type == "TRAILING" else "TRAILING PM"
                    send_telegram(f"🟢 {label}: {tradingsymbol} SL trailed to ₹{new_sl_price:.2f}")
            else:
                logging.info(f"   [TEST] Would modify SL to {new_sl_price:.2f}")
            return True
        except Exception as e:
            logging.error(f"   [✗ FAILED] Could not modify SL: {e}")
            return False

    def monitor_open_positions(self):
        try:
            if not is_market_open():
                logging.info(f"[PAUSED] Market closed. Active: NSE 9:15-15:30, MCX 9:00-23:55")
                return
            
            positions = self.kite.positions()['net']
            open_positions = [p for p in positions if p['quantity'] != 0]
            
            if not open_positions:
                logging.info("[STATUS] No open positions.")
                return

            logging.info("---" + "#"*72 + "---")
            logging.info(f"--- MONITORING {len(open_positions)} POSITIONS ---")
            logging.info("---" + "#"*72 + "---")
            
            # Fetch live LTP for all open positions in one API call
            # positions()['last_price'] can be STALE for low-liquidity MCX options.
            # kite.ltp() always returns the latest tick from the exchange.
            live_ltps = {}
            try:
                instrument_keys = [f"{p['exchange']}:{p['tradingsymbol']}" for p in open_positions]
                ltp_data = self.kite.ltp(instrument_keys)
                for key, val in ltp_data.items():
                    sym = key.split(':')[1]
                    live_ltps[sym] = val['last_price']
            except Exception as e:
                logging.warning(f"[LTP FETCH] Could not get live LTP: {e}. Using positions() last_price.")

            for pos in open_positions:
                symbol = pos['tradingsymbol']

                # ── CHANGE 1: Skip CNC/delivery positions completely ──────────
                # Long-term delivery buys (product=CNC) should never have an
                # automated SL placed or trailed. Zerodha also rejects SL-L as
                # AMO after market hours for CNC, so we skip them entirely.
                if pos.get('product') == 'CNC':
                    ltp = live_ltps.get(symbol, pos['last_price'])
                    lot_size = abs(pos.get('lot_size', 1)) or 1
                    pnl = (ltp - abs(pos['average_price'])) * pos['quantity'] * lot_size
                    logging.info(f"[{symbol}] LTP: ₹{ltp:.2f} | PnL: ₹{pnl:.2f} | [CNC - delivery hold, no SL managed]")
                    continue
                # ─────────────────────────────────────────────────────────────

                # Use live LTP if available, else fall back to positions() last_price
                if symbol in live_ltps:
                    ltp = live_ltps[symbol]
                    pos['last_price'] = ltp  # Update so downstream functions use fresh price
                else:
                    ltp = pos['last_price']
                # Recalculate PnL with live LTP (lot-size aware via Kite pos['pnl'] formula)
                lot_size = abs(pos.get('lot_size', 1)) or 1
                pnl = (ltp - abs(pos['average_price'])) * pos['quantity'] * lot_size
                pos['pnl'] = pnl  # update for downstream
                
                logging.info(f"[{symbol}] LTP: ₹{ltp:.2f} | PnL: ₹{pnl:.2f}")
                self._last_known_pnl[symbol] = pnl  # CHANGE 4: snapshot for exit loss accounting
                
                # Check for existing SL order
                existing_sl = self.get_existing_sl_order(symbol)
                
                if existing_sl:
                    # Calculate new trailing SL
                    new_sl, sl_type = self.calculate_trailing_sl(pos, ltp, existing_sl['trigger_price'])
                    self.log_status(pos, new_sl, sl_type)
                    
                    # Modify SL if needed
                    should_modify = False
                    if sl_type in ["TRAILING", "TRAILING_PM", "TRANSITION", "BREAKEVEN"]:  # CHANGE 2: TRAILING_PM
                        if pos['quantity'] > 0:  # LONG
                            should_modify = new_sl > existing_sl['trigger_price']
                        else:  # SHORT
                            should_modify = new_sl < existing_sl['trigger_price']
                    
                    if should_modify:
                        self.modify_sl_order(
                            existing_sl['order_id'],
                            symbol,
                            new_sl,
                            abs(pos['quantity']),
                            existing_sl['transaction_type'],
                            pos['exchange'],
                            sl_type=sl_type
                        )
                else:
                    # No SL exists - place one
                    if symbol not in self.protected_positions:
                        # ── CHANGE 4: Circuit breaker gate ───────────────────
                        if self.circuit_breaker_fired:
                            daily_limit = self.sl_config.get('daily_loss_limit', 10000)
                            logging.warning(
                                f"   [CIRCUIT BREAKER] Skipping SL for {symbol} — "
                                f"daily loss limit ₹{daily_limit:,.0f} already reached."
                            )
                        else:
                            logging.info(f"   [NEW POSITION] No SL found - placing initial SL...")
                            if self.place_sl_order(pos):
                                self.protected_positions.add(symbol)
                        # ─────────────────────────────────────────────────────

            # Check for exits (SL hit or manual close)
            current_symbols = {p['tradingsymbol'] for p in open_positions}
            for protected in list(self.protected_positions):
                if protected not in current_symbols:
                    logging.warning(f"[EXIT] {protected} position closed")
                    send_telegram(f"📤 Position Closed: {protected}")
                    self.record_sl_exit(protected)
                    self.protected_positions.remove(protected)
                    # Clean up tracking data
                    self.initial_sl_prices.pop(protected, None)
                    self.sl_placement_time.pop(protected, None)
                    self.atr_cache.pop(protected, None)
                    
        except Exception as e:
            logging.error(f"[ERROR] Monitor failed: {e}")

    def log_status(self, position, sl_price, sl_type):
        avg_price = abs(position['average_price'])
        ltp = position['last_price']
        pnl_pct = ((ltp - avg_price) / avg_price * 100) if position['quantity'] > 0 else ((avg_price - ltp) / avg_price * 100)
        
        status_map = {
            "INITIAL":     "🔴 INITIAL",
            "BREAKEVEN":   "🟡 BREAKEVEN (Risk-Free)",
            "TRAILING":    f"🟢 TRAILING AM (3.0% from ₹{ltp:.2f})",
            "TRAILING_PM": f"🟢 TRAILING PM (1.5% from ₹{ltp:.2f})",   # CHANGE 2
            "TRANSITION":  "🟠 TRANSITION (Grace)",
            "NO_CHANGE":   "⚪ NO CHANGE"
        }
        logging.info(f"   [SL] {status_map.get(sl_type, sl_type)} | PnL: {pnl_pct:+.1f}% | SL: ₹{sl_price:.2f}")

    def run(self):
        """Main monitoring loop with PRO features: exponential backoff, forced exit, reconciliation"""
        global _shutdown_requested
        retry_delay = 5

        # Startup notification
        send_telegram(f"🚀 SL Monitor Online (ATR: {'ON' if self.use_atr else 'OFF'} | Mode: {'TEST' if self.test_mode else 'LIVE'})")
        logging.info(f"[START] SL Monitor running with PRO features enabled")

        while not _shutdown_requested:
            try:
                # 0. Refresh blacklist from file (catches SL exits added since startup)
                self._refresh_sl_exits_from_file()

                # 1. Periodic State Reconciliation (every 5 minutes)
                if time.time() - self.last_sync > self.pro_rules['sync_interval']:
                    self.reconcile_state()

                # 2. Check for forced exit times
                positions = self.kite.positions()['net']
                open_positions = [p for p in positions if p['quantity'] != 0]

                for pos in open_positions:
                    symbol = pos['tradingsymbol']
                    # CHANGE 2: pass full pos so CNC check works inside is_forced_exit_time
                    if self.is_forced_exit_time(pos) and symbol not in self.exited_today:
                        logging.warning(f"[FORCED EXIT] {symbol} - End of session square-off triggered")
                        self.execute_square_off(pos)
                        self.exited_today.add(symbol)

                # 3. Standard SL monitoring
                self.monitor_open_positions()

                retry_delay = 5  # Reset delay on success

            except KeyboardInterrupt:
                logging.info("\n[STOP] Shutting down SL monitor...")
                break
            except Exception as e:
                logging.error(f"[CRITICAL] Loop error: {e}. Retrying in {retry_delay}s...")
                send_telegram(f"⚠️ SL Monitor Error: {e}")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 300)  # Exponential backoff up to 5 mins
                continue

            time.sleep(self.check_interval)

        # Graceful shutdown
        logging.info("[SHUTDOWN] SL Monitor stopped")
        send_telegram("🛑 SL Monitor stopped")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='SL Monitor with ATR Enhancement')
    parser.add_argument('--interval', type=int, default=30, help='Check interval in seconds (default: 30)')
    parser.add_argument('--test', action='store_true', help='Test mode (no real orders)')
    parser.add_argument('--no-atr', action='store_true', help='Disable ATR (use fixed % only)')
    parser.add_argument('--atr-multiplier', type=float, default=1.5, 
                       help='ATR multiplier (default: 1.5). Try 2.0 for more room, 1.0 for tighter stops')
    args = parser.parse_args()
    
    EnhancedSLMonitor(
        check_interval=args.interval, 
        test_mode=args.test,
        use_atr=not args.no_atr,
        atr_multiplier=args.atr_multiplier
    ).run()
