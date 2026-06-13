"""
telegram_reader_production.py - ENHANCED VERSION
WITH FUTURES SUPPORT + EXPIRY DATE DISPLAY + TIMESTAMPED LOGS
+ MULTI-MESSAGE SIGNAL COMBINING + NOISE FILTERING

Features:
- Graceful shutdown on SIGTERM/SIGINT
- Rate limiting to prevent API bans
- Explicit timezone handling (IST)
- Multi-message signal combining for split signals (any channel)
- Noise filtering for non-trading messages
"""

import asyncio
import json
import logging
import sqlite3
import signal
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.functions.channels import GetParticipantRequest
import sys
import io
import os
import time
import pytz

def should_skip_non_trading_message(message_text):
    """
    CONSERVATIVE pre-filter - only skip OBVIOUS non-trading messages
    Be very careful not to filter actual trading signals
    """
    # DISABLING PRE-FILTER TO RESTORE VERBOSITY (to match user preference)
    # return False
    
    # Softer pre-filter: only skip obvious non-trading messages if user wants some focus, 
    # but for now we'll log more info like the old version.
    return False


def needs_claude_api(message_text):
    """
    Determine if message might be a trading signal worth using Claude API
    Always return True if it has a symbol and some numbers to be safe.
    """
    return True

try:
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
except ImportError:
    IST = None
    logging.warning("[WARN] pytz not installed - using local timezone")

# Fix Windows console encoding issues
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 0. Add Master Hub to path immediately
import sys
from pathlib import Path
master_lib = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib"
if master_lib not in sys.path:
    sys.path.append(master_lib)
from master_resource import MasterResource, get_telegram_config, get_claude_key, get_trading_db_path, get_parsing_rules_path

# Yaatra channel dedicated parser
try:
    from yaatra_parser import parse_yaatra_message
    YAATRA_PARSER_AVAILABLE = True
except ImportError:
    YAATRA_PARSER_AVAILABLE = False

# Context-aware LLM parser (STOCK MARKET TRADING TIPS + conversational channels)
try:
    from context_aware_parser import parse_with_context, seed_from_db, CONTEXT_CHANNELS
    CONTEXT_PARSER_AVAILABLE = True
except ImportError:
    CONTEXT_PARSER_AVAILABLE = False
    CONTEXT_CHANNELS = set()
    def seed_from_db(*a, **kw): pass

# Dedicated parsers: ShortTerm / WealthWorld / Sidharth / JP
try:
    from channel_parsers import get_channel_parser
    CHANNEL_PARSERS_AVAILABLE = True
except ImportError:
    get_channel_parser = lambda cid: None
    CHANNEL_PARSERS_AVAILABLE = False

# Generate timestamped log filename in centralized directory
log_ts = datetime.now().strftime('%d%b%Y_%H_%M_%S').upper()
log_dir = MasterResource.MASTER_ROOT / 'logs'
log_dir.mkdir(exist_ok=True)
log_filename = str(log_dir / f"telegram_reader_production_{log_ts}.log")

# Setup logger with DEBUG level for maximum verbosity
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logging.info(f"[LOG] Writing to centralized Master logs: {log_filename}")

# SUPPRESS THIRD-PARTY DEBUG LOGS (Noise reduction)
# We keep our app at DEBUG but hide the library internal networking details
logging.getLogger('telethon').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)
logging.getLogger('numexpr').setLevel(logging.WARNING)

logger = logging.getLogger("TELEGRAM_READER")
logging.info(f"[LOG] Writing to: {log_filename}")

def load_telegram_config():
    """Load Telegram credentials from Master Hub"""
    config = get_telegram_config()
    if config:
        return (
            config['api_id'],
            config['api_hash'],
            config.get('phone') or config.get('phone_number')
        )
    raise RuntimeError("Telegram credentials not found in Master Hub!")

TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE = load_telegram_config()

# Load Claude API key from Master Hub
claude_api_key = get_claude_key()
if not claude_api_key:
    logging.warning("[WARNING] Claude API key not found in Master Hub - fallback disabled")

# Initialize Telegram client
client = TelegramClient('trading_bot', TELEGRAM_API_ID, TELEGRAM_API_HASH)

# Initialize thread-safe database (Master Hub)
from db_utils import ThreadSafeDB
db = ThreadSafeDB(get_trading_db_path())
db.init_signals_table(include_instrument_type=True)
logging.info(f"[OK] Thread-safe database initialized at: {get_trading_db_path()}")

# Channels to monitor (INTEGER format!)
MONITORED_CHANNELS = [
    -1002498088029,  # RJ - STUDENT PRACTICE CALLS
    -1002770917134,  # MCX PREMIUM
    -1002842854743,  # VIP RJ Paid Education Purpose
    -1003089362819,  # Paid Premium group
    -1001903138387,  # COPY MY TRADES BANKNIFTY
    -1002380215256,  # PREMIUM_GROUP
    -1002201480769,  # Trader ayushi
    -1003282204738,  # JP Paper trade - May-2026
    -1003658135032,  # SIDHARTH SINGH PREMIUM
    -1003053351657,  # Investing Korner
    -1001815528606,  # MULTIBAGGER PENNY STOCK
    -1001967914715,  # COMMODITY OPTIONS PRIME
    -1001822833953,  # COMMODITY OPTIONS PRIME  (inactive since May-2026; was labelled "Short Term Batch")
    -1001858110716,  # INDEX OPTIONS PRIME
    -1001553033593,  # STOCK OPTIONS PRIME      (was labelled "Long Term Equity Batch")
    -1001670038276,  # STOCK OPTIONS PRIME      (second channel with same TG name)
    -1001542890753,  # INDEX OPTIONS PRIME      (was labelled "BTST EQUITY CASH AND FUTURES SERVICE")
    -1001404315099,  # FUTURES SEGMENT BATCH
    -1003770951544,  # Market Yaatra Official
    -1003800707569,  # STOCK MARKET TRADING TIPS
    # ── New channels (IDs confirmed via fetch_channel_history.py 02-Jun-2026) ─────
    -1001294857397,   # Mcx Trading King Official Group  (username: freemcxcalls1)
    -1003853936992,   # EXPIRY KING                      (username: expiryking06)
    -1001893868490,   # MCX TRADERS                      (username: mcx_forex_strong_level)
    -1003115553842,   # Premium Nifty Banknifty group no 3 (added 2026-06-05)
    -1002670475451,   # Premium Group Stock Option (added 2026-06-06)
]

# Import the FUTURES-ENABLED parser
try:
    from signal_parser_with_futures import SignalParserWithFutures
    PARSER_TYPE = "futures_enabled"
    FUTURES_SUPPORT = True
except ImportError:
    # Fallback to old parser if new one not found
    try:
        from signal_parser_with_claude_fallback import SignalParserWithClaudeFallback as SignalParserWithFutures
        PARSER_TYPE = "claude_fallback"
        FUTURES_SUPPORT = False
        logging.warning("[WARNING] Using old parser - futures support disabled")
    except ImportError:
        print("ERROR: Parser not found!")
        exit(1)

# Import multi-message signal combiner
try:
    from multi_message_signal_combiner import MultiMessageSignalCombiner, ChannelSpecificRules
    MULTI_MESSAGE_SUPPORT = True
except ImportError:
    MULTI_MESSAGE_SUPPORT = False

# Initialize parser with futures support
parser = SignalParserWithFutures(
    claude_api_key=claude_api_key,
    rules_file=get_parsing_rules_path()
)

# ========================================
# CRITICAL FIX: Patch parser for correct tradingsymbol format
# ========================================
from tradingsymbol_utils import get_correct_tradingsymbol

original_parse = parser.parse

def patched_parse(message, **kwargs):
    """Wrapper that fixes tradingsymbol format after parsing"""
    result = original_parse(message, **kwargs)

    if result and 'tradingsymbol' in result:
        # Regenerate tradingsymbol with correct format for OPTIONS
        if 'strike' in result and result.get('instrument_type', 'OPTIONS') == 'OPTIONS':
            try:
                correct_ts = get_correct_tradingsymbol(
                    symbol=result['symbol'],
                    strike=result['strike'],
                    option_type=result['option_type'],
                    expiry_date=result['expiry_date']
                )
                if correct_ts != result['tradingsymbol']:
                    logging.info(f"[FIX] Corrected: {result['tradingsymbol']} -> {correct_ts}")
                    result['tradingsymbol'] = correct_ts
            except Exception as e:
                logging.warning(f"[WARN] Could not fix tradingsymbol: {e}")

    return result

parser.parse = patched_parse
logging.info("[OK] Parser patched with correct tradingsymbol format (NIFTY/SENSEX weekly fix applied)")


if FUTURES_SUPPORT:
    logging.info(f"[OK] Using SignalParserWithFutures (OPTIONS + FUTURES support)")
else:
    logging.info(f"[OK] Using legacy parser (OPTIONS only)")

# ========================================
# MULTI-MESSAGE SIGNAL COMBINER SETUP
# ========================================
signal_combiner = None

if MULTI_MESSAGE_SUPPORT:
    signal_combiner = MultiMessageSignalCombiner(
        parser=parser,
        combination_window_seconds=30,
        max_messages_to_combine=5,
    )
    logging.info("[OK] Multi-message signal combiner initialized (window=30s, max=5)")

    # ---- Channel-specific rules ----

    # Active channels - single-message only (send complete signals)
    signal_combiner.add_channel_rules("-1002498088029", ChannelSpecificRules(
        channel_name="RJ - STUDENT PRACTICE CALLS",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1002770917134", ChannelSpecificRules(
        channel_name="MCX PREMIUM",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1002842854743", ChannelSpecificRules(
        channel_name="VIP RJ Paid Education Purpose",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1003089362819", ChannelSpecificRules(
        channel_name="Paid Premium group",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1001903138387", ChannelSpecificRules(
        channel_name="COPY MY TRADES BANKNIFTY",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1002380215256", ChannelSpecificRules(
        channel_name="PREMIUM_GROUP",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1002201480769", ChannelSpecificRules(
        channel_name="Trader ayushi",
        always_single_message=True,
    ))
    # New Channel 1 — REMOVED (permission denied, 20-Feb-26)
    # signal_combiner.add_channel_rules("-1001801974768", ChannelSpecificRules(
    #     channel_name="New Channel 1",
    #     always_single_message=True,
    # ))

    # JP Paper trade - single-message only (has dedicated parser)
    signal_combiner.add_channel_rules("-1003282204738", ChannelSpecificRules(
        channel_name="JP Paper trade - Dec-25",
        always_single_message=True,
    ))

    # Additional Active Channels (Single-message only)
    signal_combiner.add_channel_rules("-1003658135032", ChannelSpecificRules(
        channel_name="SIDHARTH SINGH PREMIUM",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1003053351657", ChannelSpecificRules(
        channel_name="Investing Korner",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1001815528606", ChannelSpecificRules(
        channel_name="MULTIBAGGER PENNY STOCK",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1001967914715", ChannelSpecificRules(
        channel_name="COMMODITY OPTIONS PRIME",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1001822833953", ChannelSpecificRules(
        channel_name="Short Term Batch",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1001858110716", ChannelSpecificRules(
        channel_name="INDEX OPTIONS PRIME",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1001553033593", ChannelSpecificRules(
        channel_name="Long Term Equity Batch",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1001670038276", ChannelSpecificRules(
        channel_name="STOCK OPTIONS PRIME",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1001542890753", ChannelSpecificRules(
        channel_name="BTST EQUITY CASH AND FUTURES SERVICE",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1001404315099", ChannelSpecificRules(
        channel_name="FUTURES SEGMENT BATCH",
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1003770951544", ChannelSpecificRules(
        channel_name="Investing Korner",  # confirmed ID 02-Jun-2026
        always_single_message=True,
    ))
    signal_combiner.add_channel_rules("-1003800707569", ChannelSpecificRules(
        channel_name="STOCK MARKET TRADING TIPS",
        always_single_message=True,
    ))
    # ── New channels ─────────────────────────────────────────────────────────────
    signal_combiner.add_channel_rules("-1001294857397", ChannelSpecificRules(
        channel_name="Mcx Trading King Official Group",
        always_single_message=True,
    ))
    # Expiry King uses drip-style multi-message (like Sidharth) — allow combining
    signal_combiner.add_channel_rules("-1003853936992", ChannelSpecificRules(
        channel_name="EXPIRY KING",
        combination_window_seconds=60,
        max_messages_to_combine=3,
    ))
    signal_combiner.add_channel_rules("-1001893868490", ChannelSpecificRules(
        channel_name="MCX TRADERS",
        always_single_message=True,
    ))

    # New Channel 2 — REMOVED (permission denied, 20-Feb-26)
    # signal_combiner.add_channel_rules("-1001200390337", ChannelSpecificRules(
    #     channel_name="New Channel 2",
    #     combination_window_seconds=30,
    #     max_messages_to_combine=5,
    # ))

    # Noisy channels (currently disabled, rules ready for when re-enabled)
    signal_combiner.add_channel_rules("-1001456128948", ChannelSpecificRules(
        channel_name="Ashish Kyal Trading Gurukul",
        always_single_message=True,
        noise_patterns=[
            r'(?i)^(dear\s+(students?|members?|traders?)|class\s+|session\s+|lecture)',
            r'(?i)(gurukul|workshop|webinar|seminar|course|enroll)',
        ],
        min_message_length=10,
    ))
    signal_combiner.add_channel_rules("-1001389090145", ChannelSpecificRules(
        channel_name="Stockpro Online",
        always_single_message=True,
        noise_patterns=[
            r'(?i)(visit\s+(our|www)|stockpro|free\s+trial|accuracy\s+\d+%)',
            r'(?i)^(follow\s+us|share\s+with|forward\s+this)',
        ],
        min_message_length=10,
    ))
    signal_combiner.add_channel_rules("-1002431924245", ChannelSpecificRules(
        channel_name="MCX JACKPOT TRADING",
        always_single_message=True,
        noise_patterns=[
            r'(?i)(jackpot|bumper\s+profit|100%\s+sure|guaranteed)',
            r'(?i)^(profit\s+booking|booked\s+profit|see\s+our\s+result)',
        ],
        min_message_length=10,
    ))
    signal_combiner.add_channel_rules("-1001294857397", ChannelSpecificRules(
        channel_name="Mcx Trading King Official Group",
        always_single_message=True,
        noise_patterns=[
            r'(?i)(trading\s+king|king\s+of\s+mcx|join\s+paid|premium\s+member)',
            r'(?i)^(profit\s+earned|today.s\s+result|check\s+screenshot)',
        ],
        min_message_length=10,
    ))

    logging.info("[OK] Channel rules configured for all channels")
else:
    logging.info("[INFO] Multi-message combining disabled (module not found)")

# Statistics
stats = {
    'total_messages': 0,
    'parsed_signals': 0,
    'stored_signals': 0,
    'parsing_failures': 0,
    'options_signals': 0,
    'futures_signals': 0,
    'noise_filtered': 0,
    'combined_signals': 0,
}

# ========================================
# RATE LIMITING
# ========================================
import time
from collections import deque

class RateLimiter:
    """Simple rate limiter to prevent API bans"""
    def __init__(self, max_calls=30, window_seconds=60):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.calls = deque()

    def acquire(self):
        """Wait if rate limit exceeded, return True when ready"""
        now = time.time()
        # Remove calls outside the window
        while self.calls and self.calls[0] < now - self.window_seconds:
            self.calls.popleft()

        if len(self.calls) >= self.max_calls:
            # Wait until oldest call expires
            sleep_time = self.calls[0] + self.window_seconds - now
            if sleep_time > 0:
                logging.warning(f"[RATE LIMIT] Waiting {sleep_time:.1f}s...")
                time.sleep(sleep_time)

        self.calls.append(time.time())
        return True

# Rate limiter: max 30 messages per minute
rate_limiter = RateLimiter(max_calls=30, window_seconds=60)

# ========================================
# GRACEFUL SHUTDOWN
# ========================================
shutdown_event = asyncio.Event() if hasattr(asyncio, 'Event') else None
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

    # Flush any pending multi-message buffers before shutting down
    if signal_combiner:
        logging.info("[SHUTDOWN] Flushing multi-message buffers...")
        signal_combiner.flush_all()

    print_stats()

    # Try to set asyncio event if available
    try:
        if shutdown_event and not shutdown_event.is_set():
            shutdown_event.set()
    except Exception:
        pass

# Register signal handlers
if sys.platform != 'win32':
    signal.signal(signal.SIGTERM, request_shutdown)
signal.signal(signal.SIGINT, request_shutdown)

# ========================================
# TIMEZONE UTILITIES
# ========================================
def get_ist_now():
    """Get current time in IST timezone"""
    if IST:
        return datetime.now(IST)
    return datetime.now()

def format_ist_timestamp(dt=None):
    """Format datetime as IST timestamp string"""
    if dt is None:
        dt = get_ist_now()
    if IST and dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt.strftime('%Y-%m-%d %H:%M:%S IST')


def get_expiry_dates_from_csv():
    """
    Extract and display expiry dates for major indices from CSV
    Returns dict of {symbol: [expiry_dates]}
    """
    try:
        import pandas as pd
        from datetime import datetime

        # Try to load CSV or Parquet
        try:
            from master_resource import get_instruments_path, get_parquet_path
            parquet_path = get_parquet_path()
            csv_path = get_instruments_path()
            
            if os.path.exists(parquet_path):
                df = pd.read_parquet(parquet_path)
                logging.info(f"[EXPIRY] Loaded {parquet_path}")
            else:
                df = pd.read_csv(csv_path)
                logging.info(f"[EXPIRY] Loaded {csv_path}")
        except Exception as err:
            logging.warning(f"[EXPIRY] Could not load instruments file: {err}")
            return None

        current_month = datetime.now().month
        current_year = datetime.now().year
        
        logging.info(f"[EXPIRY] Scanning for expiries in {datetime.now().strftime('%B %Y')}...")

        # Symbols to check
        symbols_to_check = ['NIFTY', 'SENSEX', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'BANKEX']

        expiry_info = {}

        for symbol in symbols_to_check:
            # Find instruments for this symbol
            logging.debug(f"[EXPIRY] Searching instruments for: {symbol}")
            symbol_instruments = df[df['symbol'].str.contains(symbol, case=False, na=False)].copy()

            if len(symbol_instruments) > 0:
                logging.debug(f"[EXPIRY] Found {len(symbol_instruments)} total records for {symbol}")
                # Convert expiry_date to datetime
                symbol_instruments['expiry_dt'] = pd.to_datetime(symbol_instruments['expiry_date'])

                # Filter for current and future months
                # To be more verbose, let's look at all upcoming expiries, not just current month
                upcoming_expiries = symbol_instruments[
                    (symbol_instruments['expiry_dt'] >= pd.Timestamp.now().normalize())
                ].copy()
                
                logging.debug(f"[EXPIRY] Found {len(upcoming_expiries)} upcoming expiries for {symbol}")

                # Get unique expiry dates
                unique_expiries = sorted(upcoming_expiries['expiry_dt'].unique())

                if len(unique_expiries) > 0:
                    # Convert to string dates
                    expiry_dates = [dt.strftime('%Y-%m-%d (%A)') for dt in unique_expiries]
                    expiry_info[symbol] = expiry_dates
                    logging.info(f"[EXPIRY] {symbol}: Identified {len(expiry_dates)} upcoming expiry dates")
                else:
                    logging.warning(f"[EXPIRY] No upcoming expiries found for {symbol}")
            else:
                logging.warning(f"[EXPIRY] Symbol {symbol} not found in instrument master!")

        return expiry_info

    except Exception as e:
        logging.error(f"[EXPIRY] Error extracting expiry dates: {e}")
        import traceback
        traceback.print_exc()
        return None


def display_expiry_info():
    """Display expiry dates for major indices"""
    logging.info("")
    logging.info("="*80)
    logging.info("CURRENT MONTH EXPIRY DATES")
    logging.info("="*80)

    expiry_info = get_expiry_dates_from_csv()

    if expiry_info:
        current_month_name = datetime.now().strftime('%B %Y')
        logging.info(f"Month: {current_month_name}")
        logging.info("")

        # Display NIFTY and SENSEX first
        for symbol in ['NIFTY', 'SENSEX']:
            if symbol in expiry_info:
                logging.info(f"{symbol}:")
                for expiry in expiry_info[symbol]:
                    logging.info(f"  > {expiry}")
                logging.info("")

        # Display BANKNIFTY, FINNIFTY, MIDCPNIFTY
        for symbol in ['BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY']:
            if symbol in expiry_info:
                logging.info(f"{symbol}:")
                for expiry in expiry_info[symbol]:
                    logging.info(f"  > {expiry}")
                logging.info("")

        # Display any other symbols found
        other_symbols = [s for s in expiry_info.keys()
                        if s not in ['NIFTY', 'SENSEX', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY']]
        for symbol in other_symbols:
            logging.info(f"{symbol}:")
            for expiry in expiry_info[symbol]:
                logging.info(f"  > {expiry}")
            logging.info("")
    else:
        logging.warning("Could not load expiry information from CSV/Parquet")

    logging.info("="*80)
    logging.info("")



# MCX commodity symbols - used to fix exchange field that parser may get wrong
_MCX_SYMBOLS = {
    'CRUDEOIL', 'CRUDEOILM', 'GOLD', 'GOLDM', 'GOLDPETAL', 'GOLDGUINEA',
    'SILVER', 'SILVERMIC', 'SILVERM', 'NATURALGAS', 'NATURALGASM',
    'COPPER', 'COPPERM', 'ZINC', 'ZINCMINI', 'ALUMINIUM', 'ALUMINIUMM',
    'LEAD', 'LEADMINI', 'NICKEL', 'NICKELM',
}


def _sanitize_for_json(data: dict) -> dict:
    """Convert any non-JSON-serializable values (e.g. pandas Timestamp, datetime) to strings.
    Called before inserting parsed_data into the database to prevent serialization errors.

    FIX 1: expiry_date stored as DATE-ONLY string 'YYYY-MM-DD' (not datetime) so it
            matches the CSV format used by order_placer for instrument lookup.
    FIX 2: exchange field corrected to 'MCX' for known commodity symbols so the order
            placer routes to the right exchange instead of defaulting to NFO.
    """
    sanitized = {}
    for key, value in data.items():
        # pandas Timestamp and Python datetime/date — store as DATE-ONLY (YYYY-MM-DD)
        # This ensures order_placer CSV lookup matches: CSV has "2026-03-17", not "2026-03-17 00:00:00"
        if hasattr(value, 'strftime'):
            sanitized[key] = value.strftime('%Y-%m-%d')   # date-only, no time component
        elif isinstance(value, str) and key == 'expiry_date' and len(value) > 10:
            # Truncate any "2026-03-17 00:00:00" strings already stored as text
            sanitized[key] = value[:10]
        else:
            sanitized[key] = value

    # FIX 2: Correct exchange for MCX commodity symbols
    # Parser defaults everything to NFO — override for known MCX instruments
    symbol = str(sanitized.get('symbol', '')).upper()
    # Strip trailing digits/month codes to get base symbol (e.g. "CRUDEOIL" from "CRUDEOIL26MAR")
    import re as _re
    base_symbol = _re.sub(r'(CE|PE|FUT|\d+|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)$', '', symbol).strip()
    if base_symbol in _MCX_SYMBOLS:
        if sanitized.get('exchange') != 'MCX':
            logging.info(f"[EXCHANGE FIX] {symbol}: correcting exchange {sanitized.get('exchange')} -> MCX")
            sanitized['exchange'] = 'MCX'

    return sanitized


# ── SAFE TRADER trail helpers ─────────────────────────────────────────────────
import re as _re

_SAFE_TRADER_RE = _re.compile(
    r"safe\s*trader['']?s?\s*(can\s*)?book\s*profit",
    _re.IGNORECASE,
)

def _is_safe_trader_message(text: str) -> bool:
    """Return True if this is a 'SAFE TRADER'S BOOK PROFITS' update message."""
    return bool(_SAFE_TRADER_RE.search(text))


def _apply_safe_trader_trail(channel_name: str, raw_text: str):
    """
    Move SL to breakeven (actual_entry_price) for all OPEN orders from this channel.
    The channel posts this when the trade hits the first target — remaining position
    should trail SL to cost so downside is zero.
    """
    try:
        from master_resource import get_trading_db_path
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(get_trading_db_path())
        cur  = conn.cursor()

        # Find OPEN orders from this channel that still have room to trail
        cur.execute("""
            SELECT order_id, channel_name, entry_price, actual_entry_price, stop_loss
            FROM   orders
            WHERE  status = 'OPEN'
              AND  (channel_name = ? OR channel_name LIKE ?)
        """, (channel_name, f"%{channel_name[:20]}%"))
        rows = cur.fetchall()

        if not rows:
            logging.info(f"[SAFE_TRAIL] No OPEN positions from '{channel_name}' — nothing to trail")
            conn.close()
            return

        updated = 0
        for order_id, ch, entry_price, actual_entry, current_sl in rows:
            cost = actual_entry if actual_entry else entry_price
            if cost is None:
                continue
            # Only trail upward (never make SL worse)
            if current_sl is not None and current_sl >= cost:
                logging.info(f"[SAFE_TRAIL] {order_id}: SL={current_sl} already ≥ cost={cost}, skip")
                continue
            cur.execute("""
                UPDATE orders
                SET    stop_loss  = ?,
                       sl_stage   = 'SAFE_TRAIL',
                       updated_at = CURRENT_TIMESTAMP
                WHERE  order_id   = ?
            """, (cost, order_id))
            logging.info(
                f"[SAFE_TRAIL] {order_id} ({ch}): SL {current_sl} → {cost} (breakeven trail)"
            )
            updated += 1

        conn.commit()
        conn.close()
        logging.info(f"[SAFE_TRAIL] Updated {updated}/{len(rows)} positions from '{channel_name}'")

    except Exception as e:
        logging.warning(f"[SAFE_TRAIL] Failed: {e}")


# ── COPY MY TRADES partial-exit at Target 1 ──────────────────────────────────
# Channels that use this rule: COPY MY TRADES BANKNIFTY, INDEX OPTIONS PRIME,
# STOCK OPTIONS PRIME (same "SAFE TRADER / TARGET" protocol).
_TARGET1_RE = _re.compile(
    r'(target\s*1\s*(done|hit|achieved|all[\s_]*most|almost)|'
    r'approaching\s*target\s*1|'
    r'almost\s+at\s+target\s*1|'
    r'1st\s*target\s*(done|hit)|'
    r'safe\s*trader.*target)',
    _re.IGNORECASE,
)

def _is_target1_message(text: str) -> bool:
    return bool(_TARGET1_RE.search(text))


def _apply_partial_exit(channel_name: str, raw_text: str):
    """
    When a channel signals Target 1 is near/done, close 50% of open position
    from that channel and move SL to breakeven on the remaining quantity.

    Sets partial_qty_remaining = floor(original_qty / 2) and sl_stage = 'PARTIAL_T1'
    so the SL monitor knows the position has been partially exited.
    """
    try:
        from master_resource import get_trading_db_path
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(get_trading_db_path())
        cur  = conn.cursor()

        cur.execute("""
            SELECT order_id, channel_name, quantity, entry_price, actual_entry_price,
                   stop_loss, partial_qty_remaining
            FROM   orders
            WHERE  status = 'OPEN'
              AND  sl_stage != 'PARTIAL_T1'
              AND  (channel_name = ? OR channel_name LIKE ?)
        """, (channel_name, f"%{channel_name[:20]}%"))
        rows = cur.fetchall()

        if not rows:
            logging.info(f"[PARTIAL_T1] No eligible OPEN positions from '{channel_name}'")
            conn.close()
            return

        updated = 0
        for order_id, ch, qty, entry_price, actual_entry, current_sl, partial_rem in rows:
            # Already partially exited
            if partial_rem is not None:
                continue

            full_qty    = qty or 1
            half_qty    = max(1, full_qty // 2)
            remain_qty  = full_qty - half_qty
            cost        = actual_entry if actual_entry else entry_price

            cur.execute("""
                UPDATE orders
                SET    partial_qty_remaining = ?,
                       stop_loss            = ?,
                       sl_stage             = 'PARTIAL_T1',
                       updated_at           = CURRENT_TIMESTAMP
                WHERE  order_id = ?
            """, (remain_qty, cost, order_id))

            logging.info(
                f"[PARTIAL_T1] {order_id} ({ch}): exited {half_qty}/{full_qty} lots at T1 "
                f"| remaining={remain_qty} lots | SL moved to cost={cost}"
            )
            updated += 1

        conn.commit()
        conn.close()
        logging.info(f"[PARTIAL_T1] Partial exit applied to {updated}/{len(rows)} positions from '{channel_name}'")

    except Exception as e:
        logging.warning(f"[PARTIAL_T1] Failed: {e}")


def insert_signal(channel_id, channel_name, message_id, raw_text, parsed_data):
    """Insert signal into database (thread-safe with retry logic)"""
    try:
        # Sanitize parsed_data to remove non-JSON-serializable types (e.g. pandas Timestamp)
        parsed_data = _sanitize_for_json(parsed_data)

        # Get instrument type
        instrument_type = parsed_data.get('instrument_type', 'OPTIONS')

        # Use thread-safe insert with retry logic
        signal_id = db.insert_signal(
            channel_id=channel_id,
            channel_name=channel_name,
            message_id=message_id,
            raw_text=raw_text,
            parsed_data=parsed_data,
            instrument_type=instrument_type
        )

        if signal_id:
            stats['stored_signals'] += 1

            # Track by type
            if instrument_type == 'FUTURES':
                stats['futures_signals'] += 1
            else:
                stats['options_signals'] += 1

            logging.info(f"[STORED] Signal ID: {signal_id} | Type: {instrument_type}")
            return signal_id
        else:
            logging.info(f"[SKIP] Duplicate message (Channel: {channel_name}, Msg ID: {message_id})")
            return None

    except Exception as e:
        logging.error(f"[DB ERROR] {e}")
        return None


# ========================================
# SIGNAL PROCESSING HELPERS
# ========================================

def _log_and_store_signal(parsed_data, channel_id, channel_name, message_id,
                          raw_text, was_combined=False, source_ids=None):
    """Log a parsed signal and insert it into the database.

    Shared logic used by both the single-message path and the combiner callback.
    """
    stats['parsed_signals'] += 1
    if was_combined:
        stats['combined_signals'] += 1

    instrument_type = parsed_data.get('instrument_type', 'OPTIONS')

    # Validate required fields
    if instrument_type == 'FUTURES':
        required_fields = ['symbol', 'action', 'entry_price', 'stop_loss',
                         'expiry_date', 'quantity', 'instrument_type']
    else:
        required_fields = ['symbol', 'strike', 'option_type', 'action',
                         'entry_price', 'stop_loss', 'expiry_date', 'quantity']

    missing = [f for f in required_fields if f not in parsed_data or parsed_data[f] is None]

    if missing:
        logging.warning(f"[INCOMPLETE] Missing fields: {missing}")
        logging.warning(f"   Message: {raw_text[:100]}")
    else:
        logging.info(f"[COMPLETE] All required fields present")

    if was_combined and source_ids:
        logging.info(f"[COMBINED] Signal from {len(source_ids)} messages: {source_ids}")

    # Log based on type
    if instrument_type == 'FUTURES':
        logging.info(f"[PARSED FUTURES] {parsed_data.get('symbol')} "
                   f"{parsed_data.get('expiry_month', 'FUT')}")
        logging.info(f"   Action: {parsed_data.get('action')} | "
                   f"Entry: {parsed_data.get('entry_price')} | "
                   f"SL: {parsed_data.get('stop_loss')}")
        logging.info(f"   Expiry: {parsed_data.get('expiry_date')} | "
                   f"Qty: {parsed_data.get('quantity')}")
    else:
        logging.info(f"[PARSED OPTIONS] {parsed_data.get('symbol')} "
                   f"{parsed_data.get('strike')} {parsed_data.get('option_type')}")
        logging.info(f"   Action: {parsed_data.get('action')} | "
                   f"Entry: {parsed_data.get('entry_price')} | "
                   f"SL: {parsed_data.get('stop_loss')}")
        logging.info(f"   Expiry: {parsed_data.get('expiry_date')} | "
                   f"Qty: {parsed_data.get('quantity')}")

    insert_signal(channel_id, channel_name, message_id, raw_text, parsed_data)


# ========================================
# COMBINER FLUSH CALLBACK
# ========================================
# Cache channel names so the flush callback (which fires on a timer) can log them
_channel_name_cache = {}


def _combiner_flush_callback(channel_id, combine_result):
    """Called when the combiner's timer fires and produces a signal from buffered messages."""
    if combine_result.parsed_data:
        channel_name = _channel_name_cache.get(channel_id, channel_id)
        msg_ids = combine_result.source_message_ids
        logging.info("")
        logging.info("=" * 60)
        logging.info(f"[FLUSH-COMBINE] Delayed signal from channel {channel_name}")
        logging.info(f"[TIME] {format_ist_timestamp()}")
        logging.info("=" * 60)
        _log_and_store_signal(
            parsed_data=combine_result.parsed_data,
            channel_id=channel_id,
            channel_name=channel_name,
            message_id=msg_ids[-1] if msg_ids else 0,
            raw_text=combine_result.combined_text,
            was_combined=combine_result.was_combined,
            source_ids=msg_ids,
        )


# Register flush callback if combiner is available
if signal_combiner:
    signal_combiner.set_flush_callback(_combiner_flush_callback)


# ========================================
# MESSAGE HANDLER
# ========================================

async def handle_message(event):
    """Handle incoming Telegram messages with rate limiting and multi-message combining."""
    if _shutdown_requested:
        return

    try:
        message_text = event.message.message
        if not message_text:
            return

        # Apply rate limiting
        rate_limiter.acquire()

        channel = await event.get_chat()
        channel_id = str(event.chat_id)
        channel_name = channel.title if hasattr(channel, 'title') else channel_id
        message_id = event.message.id

        # Cache channel name for flush callback
        _channel_name_cache[channel_id] = channel_name

        stats['total_messages'] += 1

        # Log preview with IST timestamp
        logging.info("")
        logging.info("="*60)
        logging.info(f"[NEW] Message from: {channel_name} (ID: {channel_id})")
        logging.info(f"[TIME] {format_ist_timestamp()}")
        preview = message_text[:80].replace('\n', ' ')
        if len(message_text) > 80:
            preview += '...'
        logging.info(f"[PREVIEW] {preview}")
        logging.info("="*60)

        # CONSERVATIVE PRE-FILTER: Only skip VERY obvious non-trading messages
        if should_skip_non_trading_message(message_text):
            stats['parsing_failures'] += 1
            logging.info(f"[SKIP] Message filtered by conservative pre-filter (NOT a potential signal)")
            return
        else:
            logging.debug(f"[DEBUG] Message passed pre-filter, proceeding to parse/combine")

        # ---- SAFE TRADER trail-to-cost rule ─────────────────────────────────────
        if _is_safe_trader_message(message_text):
            _apply_safe_trader_trail(channel_name, message_text)

        # ---- COPY MY TRADES partial exit at Target 1 ─────────────────────────
        if _is_target1_message(message_text):
            _apply_partial_exit(channel_name, message_text)

        # ---- Yaatra dedicated parser (bypasses combiner + Claude API) ----
        # Note: -1003770951544 is Investing Korner (confirmed 02-Jun-2026 via fetch).
        # Real Market Yaatra ID not yet confirmed; yaatra_parser kept as dead-letter fallback.
        if channel_id == "-1003770951544" and False and YAATRA_PARSER_AVAILABLE:  # disabled: now handled by InvestingKorner parser
            parsed_data = parse_yaatra_message(message_text)
            if parsed_data:
                _log_and_store_signal(
                    parsed_data=parsed_data,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    message_id=message_id,
                    raw_text=message_text,
                )
            else:
                stats['parsing_failures'] += 1
                logging.info(f"[YAATRA] No actionable signal in this message")
            return

        # ---- Context-aware LLM parser (conversational channels) ─────────────────
        if CONTEXT_PARSER_AVAILABLE and channel_id in CONTEXT_CHANNELS:
            parsed_data = parse_with_context(channel_id, message_text, claude_api_key or "")
            if parsed_data:
                _log_and_store_signal(
                    parsed_data=parsed_data,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    message_id=message_id,
                    raw_text=message_text,
                )
            else:
                stats['parsing_failures'] += 1
                logging.info(f"[CTX_PARSER] No actionable signal — {channel_name}")
            return

        # ---- Dedicated channel parsers (ShortTerm / WealthWorld / Sidharth / JP) ----
        _dedicated_parser = get_channel_parser(channel_id)
        if _dedicated_parser is not None:
            parsed_data = _dedicated_parser(message_text)
            if parsed_data:
                _log_and_store_signal(
                    parsed_data=parsed_data,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    message_id=message_id,
                    raw_text=message_text,
                )
            else:
                stats['parsing_failures'] += 1
                logging.info(f"[CHANNEL_PARSER] No actionable signal — {channel_name}")
            return

        # ---- Multi-message combiner path ----
        if signal_combiner:
            result = await signal_combiner.process_message(
                channel_id=channel_id,
                message_text=message_text,
                message_id=message_id,
            )

            if result is None:
                # Message buffered, waiting for more
                # Note: combiner buffer size is tracked internally per channel
                logging.info(f"[BUFFERED] Message {message_id} buffered (Wait for follow-up)")
                return

            if result.was_noise:
                stats['noise_filtered'] += 1
                stats['parsing_failures'] += 1
                logging.info(f"[NOISE] Message {message_id} identified as noise/non-trading by combiner")
                return

            if result.parsed_data:
                _log_and_store_signal(
                    parsed_data=result.parsed_data,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    message_id=message_id,
                    raw_text=result.combined_text,
                    was_combined=result.was_combined,
                    source_ids=result.source_message_ids,
                )
            else:
                stats['parsing_failures'] += 1
                logging.info(f"[REJECT] Combiner could not extract a valid signal from msg(s) {result.source_message_ids}")
            return

        # ---- Fallback: original single-message path (if combiner not available) ----
        parsed_data = parser.parse(message_text, channel_id=channel_id)

        if parsed_data:
            _log_and_store_signal(
                parsed_data=parsed_data,
                channel_id=channel_id,
                channel_name=channel_name,
                message_id=message_id,
                raw_text=message_text,
            )
        else:
            stats['parsing_failures'] += 1
            logging.info(f"[SKIP] Not a trading signal")

    except Exception as e:
        logging.error(f"[ERROR] Error handling message: {e}")
        import traceback
        traceback.print_exc()


async def main():
    """Main function"""
    await client.start(TELEGRAM_PHONE)

    # Get user info
    me = await client.get_me()
    logging.info(f"[OK] Connected to Telegram as {me.phone}")

    # Display expiry information BEFORE starting monitoring
    display_expiry_info()

    # Get channel entities and FORCE CATCH-UP for each
    channel_entities = []
    for channel_id in MONITORED_CHANNELS:
        try:
            entity = await client.get_entity(channel_id)
            channel_entities.append(entity)

            # IMPORTANT: Fetch recent messages to "wake up" the channel
            # This forces Telegram to send us new messages from this channel
            try:
                await client.get_messages(entity, limit=1)
                logging.info(f"[OK] Monitoring: {entity.title} (synced)")
            except Exception as sync_err:
                logging.info(f"[OK] Monitoring: {entity.title} (sync skipped: {type(sync_err).__name__})")

        except Exception as e:
            logging.error(f"[ERROR] Failed to get channel {channel_id}: {e}")

    # Seed context-aware parser with recent DB history for conversational channels
    if CONTEXT_PARSER_AVAILABLE:
        for ctx_id in CONTEXT_CHANNELS:
            seed_from_db(ctx_id)
            logging.info(f"[CTX] History seeded for channel {ctx_id}")

    logging.info("="*80)
    logging.info(f"[START] Monitoring {len(channel_entities)} channels")
    logging.info(f"[MODE] {PARSER_TYPE} - {'OPTIONS + FUTURES' if FUTURES_SUPPORT else 'OPTIONS only'}")
    if signal_combiner:
        logging.info(f"[COMBINE] Multi-message combining enabled (window=30s, max=5)")
    else:
        logging.info(f"[COMBINE] Multi-message combining disabled")
    logging.info(f"[LOG] Output: {log_filename}")
    logging.info("Press Ctrl+C to stop")
    logging.info("="*80)

    # Register event handler for ALL entities
    @client.on(events.NewMessage(chats=channel_entities))
    async def handler(event):
        await handle_message(event)

    # Heartbeat so dashboard log-age check shows RUNNING during quiet periods
    async def _heartbeat():
        while True:
            await asyncio.sleep(120)
            logging.info(f"[HEARTBEAT] Telegram Reader alive — monitoring {len(channel_entities)} channels")

    asyncio.ensure_future(_heartbeat())

    # Run until disconnected
    await client.run_until_disconnected()


def print_stats():
    """Print statistics including combiner stats"""
    logging.info("")
    logging.info("="*80)
    logging.info("STATISTICS")
    logging.info("="*80)
    logging.info(f"Total Messages:     {stats['total_messages']}")
    logging.info(f"Noise Filtered:     {stats['noise_filtered']}")
    logging.info(f"Parsed Signals:     {stats['parsed_signals']}")
    logging.info(f"  - Options:        {stats['options_signals']}")
    logging.info(f"  - Futures:        {stats['futures_signals']}")
    logging.info(f"  - Combined:       {stats['combined_signals']}")
    logging.info(f"Stored Signals:     {stats['stored_signals']}")
    logging.info(f"Parse Failures:     {stats['parsing_failures']}")
    logging.info("="*80)

    # Print combiner-specific stats if available
    if signal_combiner:
        signal_combiner.log_stats()


if __name__ == '__main__':
    import traceback as _tb

    MAX_RETRIES    = 999        # effectively infinite
    RETRY_DELAY_S  = 30        # wait 30s before each reconnect attempt
    attempt        = 0

    while attempt < MAX_RETRIES:
        attempt += 1
        logging.info(f"[START] Telegram Reader starting — attempt #{attempt} at {format_ist_timestamp()}")
        try:
            asyncio.run(main())
            # main() returned cleanly (e.g. graceful shutdown) — exit loop
            break
        except KeyboardInterrupt:
            if not _shutdown_requested:
                logging.info("\n[STOP] Shutting down (KeyboardInterrupt)...")
                print_stats()
            break   # user pressed Ctrl+C — do not restart
        except Exception as e:
            logging.error(f"[FATAL] Unexpected error (attempt #{attempt}): {e}")
            _tb.print_exc()
            if attempt < MAX_RETRIES:
                logging.info(f"[RETRY] Reconnecting in {RETRY_DELAY_S}s ...")
                import time as _time
                _time.sleep(RETRY_DELAY_S)
            else:
                logging.error("[FATAL] Max retries reached — giving up.")
        finally:
            logging.info(f"[END] Telegram Reader stopped at {format_ist_timestamp()}")
    # Note: No db.close() needed - ThreadSafeDB uses connection-per-operation
