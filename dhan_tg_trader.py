import asyncio
import json
import logging
import sqlite3
import os
import time
import pytz
from datetime import datetime
from telethon import TelegramClient, events

# Import Dhan and Strategy modules
import sqlite3
from core.dhan_client import DhanClient
from core.order_placer import OrderPlacer
from core.strike_lookup import StrikeLookup
from core.mcx_lookup import get_mcx_lookup
from agents.mcx_worker import MCXWorker, init_instance as _mcx_init
from yaatra_parser import parse_yaatra_message
from channel_parsers import get_channel_parser
from master_resource import MasterResource

# Setup Logging — file per restart + console
logger = MasterResource.setup_shared_logger("dhan_tg_trader")

# Load Telegram credentials from central config (avoids duplication with .env)
_tg_cfg = MasterResource.get_telegram_config()
if not _tg_cfg:
    raise FileNotFoundError(
        "telegram_config.json not found at "
        f"{MasterResource.MASTER_ROOT / 'config' / 'telegram_config.json'}"
    )
API_ID   = int(_tg_cfg["api_id"])
API_HASH = _tg_cfg["api_hash"]
PHONE    = _tg_cfg.get("phone") or _tg_cfg.get("phone_number")

logger.info(f"Telegram config loaded — phone={PHONE}")

# Initialize Dhan Components
is_sandbox    = True  # Set to False for Live
dhan_client   = DhanClient(is_sandbox=is_sandbox)
order_placer  = OrderPlacer(is_sandbox=is_sandbox)
strike_lookup = StrikeLookup()
mcx_lookup    = get_mcx_lookup()

# MCX guard daemon — polls DXY, enforces EIA blackout windows
_mcx_worker = MCXWorker()
_mcx_init(_mcx_worker)
_mcx_worker.start()
logger.info("MCXWorker started (DXY polling + EIA blackout guard)")

# IST Timezone
IST = pytz.timezone('Asia/Kolkata')

class DhanTGTrader:
    def __init__(self):
        self.client = TelegramClient('trading_bot', API_ID, API_HASH)
        self.active_positions = {} # {symbol: {order_id, entry_price, sl, type}}

    async def start(self):
        print("--- 🤖 Dhan Telegram Trader Initialized (Sandbox) ---")
        await self.client.start(phone=PHONE)
        print("✅ Telegram Client Connected.")
        
        @self.client.on(events.NewMessage())
        async def handler(event):
            message_text = event.message.message
            chat_id = event.chat_id
            # Public channels (e.g. @luxurywithtrading) deliver a numeric chat_id
            # but can also be identified by their username from event.chat.
            username = getattr(event.chat, "username", None) or ""

            signal = self.parse_message(message_text, chat_id, username)

            if signal:
                print(f"SIGNAL DETECTED: {signal}")
                await self.execute_signal(signal)

        print("🚀 Listening for signals on Telegram...")
        await self.client.run_until_disconnected()

    def parse_message(self, text, chat_id, username: str = ""):
        """Uses existing parsers to extract signal details."""
        # Try Yaatra Parser
        signal = parse_yaatra_message(text)
        if signal and signal.get('symbol'):
            return signal

        # Try Channel Parsers.
        # Telethon delivers chat_id as an int. For public channels the map also
        # stores the username string as a key (e.g. "luxurywithtrading"), so we
        # try numeric ID first, then fall back to the username.
        parser = get_channel_parser(str(chat_id)) or get_channel_parser(username)
        if parser:
            return parser(text)

        return None

    async def execute_signal(self, signal):
        """Executes the signal on Dhan and writes to orders table for SL Monitor."""
        symbol      = signal.get("symbol", "")
        action      = (signal.get("action") or "BUY").upper()
        # Bug 1 fix: parsers store entry_price, not price
        entry_price = float(signal.get("entry_price") or signal.get("price") or 0)
        # Bug 2 fix: use option_type from signal (parsers always set it)
        opt_type    = (signal.get("option_type") or "CE").upper()
        source      = signal.get("source", "Telegram")

        # --- Duplicate position guard (by symbol — one position per index at a time) ---
        if self._has_open_position(symbol):
            logger.info(f"[TG] {symbol} already has an open position — signal skipped")
            return

        # --- Classify and resolve instrument ---
        _INDEX_SYMBOLS = {
            "NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX",
            "MIDCPNIFTY", "BANKEX", "SENSEX50",
        }
        _MCX_SYMBOLS = {
            "GOLD", "GOLDM", "GOLDGUINEA", "GOLDPETAL",
            "SILVER", "SILVERM", "SILVERMICRO",
            "CRUDEOIL", "CRUDEOILM",
            "NATURALGAS", "NATGASMINI",
            "COPPER", "ZINC", "NICKEL", "LEAD", "ALUMINIUM", "TIN",
        }

        if symbol in _INDEX_SYMBOLS:
            # Bug 2 fix: use the signal's specific strike as the spot price so
            # get_atm_option returns the exact strike the channel recommended,
            # not the current market ATM. Fall back to entry_price if no strike.
            strike = signal.get("strike") or signal.get("strike_price") or 0
            spot   = float(strike) if strike else entry_price
            option = strike_lookup.get_atm_option(symbol, spot, opt_type)
            if option is None:
                logger.error(
                    f"[TG] Strike lookup failed: {symbol} {strike} {opt_type} — skipping"
                )
                return
            security_id      = option["security_id"]
            exchange_segment = option["exchange_segment"]
            tradingsymbol    = option["trading_symbol"]
            quantity         = option["lot_size"]

        elif symbol in _MCX_SYMBOLS:
            # MCX futures execution — buy/sell front-month contract
            contract = mcx_lookup.get_front_month(symbol)
            if contract is None:
                logger.error(
                    f"[TG] MCX: no active front-month contract for '{symbol}' — skipping"
                )
                return

            # EIA blackout guard
            safe, block_reason = _mcx_worker.is_safe_to_trade(symbol)
            if not safe:
                logger.warning(
                    f"[TG] MCX: {symbol} blocked — {block_reason} "
                    f"entry={entry_price} sl={signal.get('stop_loss')}"
                )
                return

            security_id      = contract["security_id"]
            exchange_segment = contract["exchange_segment"]
            tradingsymbol    = contract["trading_symbol"]
            quantity         = contract["lot_size"]

            # Log CME session warning (higher volatility — widen SL mentally)
            if _mcx_worker.is_cme_session():
                logger.info(
                    f"[TG] MCX: CME session active — elevated volatility for {symbol}"
                )

            logger.info(
                f"[TG] MCX {action} {tradingsymbol} qty={quantity} "
                f"entry={entry_price} sl={signal.get('stop_loss')} "
                f"DXY={_mcx_worker.dxy:.2f}"
            )

        else:
            logger.warning(f"[TG] Unknown symbol '{symbol}' — skipping")
            return

        logger.info(
            f"[TG] {action} {tradingsymbol} "
            f"entry={entry_price} sl={signal.get('stop_loss')} "
            f"(source={source})"
        )

        order_id = order_placer.place_market_order(
            security_id      = security_id,
            exchange_segment = exchange_segment,
            transaction_type = action,
            quantity         = quantity,
        )

        if order_id:
            logger.info(f"[TG] Order {order_id} placed for {tradingsymbol}")
            try:
                conn = sqlite3.connect(MasterResource.get_trading_db_path(), timeout=30)
                conn.execute(
                    """INSERT INTO orders
                           (order_id, symbol, action, quantity, entry_price, status,
                            tradingsymbol, security_id, exchange_segment,
                            strategy_name, created_at)
                       VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)""",
                    (
                        order_id, symbol, action, quantity, entry_price,
                        tradingsymbol, security_id, exchange_segment,
                        source, datetime.now().isoformat(),
                    ),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"[TG] Failed to write order to DB: {e}")

            self.active_positions[symbol] = {
                "order_id":    order_id,
                "entry_price": entry_price,
                "action":      action,
            }
        else:
            logger.error(f"[TG] Order placement failed for {tradingsymbol}")

    def _has_open_position(self, symbol: str) -> bool:
        try:
            conn  = sqlite3.connect(MasterResource.get_trading_db_path(), timeout=10)
            cur   = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM orders WHERE symbol=? AND status='OPEN'", (symbol,)
            )
            count = cur.fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False

if __name__ == "__main__":
    trader = DhanTGTrader()
    asyncio.run(trader.start())
