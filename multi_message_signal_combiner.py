"""
multi_message_signal_combiner.py - Multi-Message Signal Combining

Handles trading signals that arrive split across 2-3 separate Telegram messages.
Buffers partial messages per channel within a time window, combines them, and
re-parses to extract complete signals.

Also provides noise filtering to skip non-trading messages.

Usage:
    combiner = MultiMessageSignalCombiner(parser, combination_window_seconds=30)
    combiner.add_channel_rules(channel_id, ChannelSpecificRules(...))
    result = await combiner.process_message(channel_id, message_text, message_id)
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Callable, Any

logger = logging.getLogger(__name__)


# ========================================
# NOISE FILTERING
# ========================================

# Default patterns that indicate a message is NOT a trading signal
DEFAULT_NOISE_PATTERNS = [
    # Greetings and social messages
    r'(?i)^(good\s*(morning|evening|night|afternoon)|gm\s+everyone|hello\s+everyone)',
    r'(?i)^(welcome|congrats|congratulations|well\s+done|great\s+trade)',
    # Market commentary (no actionable data)
    r'(?i)^(market\s+(is|looks?|seems?|will|may|might)\s)',
    r'(?i)^(today\s+market|tomorrow\s+market|weekly\s+view|monthly\s+view)',
    # Admin / promotional
    r'(?i)(join\s+(our|my|premium|vip)|subscribe|payment|discount|offer\s+valid)',
    r'(?i)(contact\s+(us|me|admin)|whatsapp|telegram\s+link)',
    # P&L screenshots / boasting
    r'(?i)^(today[\'s]*\s+p[&/]?l|profit\s+booked|total\s+profit)',
    r'(?i)^(check\s+(our|my)\s+results)',
    # Single emoji or very short non-signal
    r'^[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF\u2600-\u26FF\u2700-\u27BF\s]{1,10}$',
    # Pure numbers without context (timestamps, etc.)
    r'^\d{1,2}:\d{2}\s*(am|pm|AM|PM)?$',
]

# Patterns that suggest a message contains partial signal data
PARTIAL_SIGNAL_PATTERNS = [
    # Symbol mentions
    r'(?i)(nifty|banknifty|sensex|finnifty|midcpnifty)',
    # MCX symbols
    r'(?i)(gold|silver|crude\s*oil|natural\s*gas|copper)',
    # Stock option keywords
    r'(?i)\b(CE|PE|CALL|PUT)\b',
    # Action keywords
    r'(?i)\b(buy|sell|long|short)\b',
    # Price / entry / SL / target indicators
    r'(?i)(entry|entr|cmp|ltp|sl|stop\s*loss|target|tgt|tp)',
    # Strike price patterns (4-6 digit numbers)
    r'\b\d{4,6}\b',
    # Price with decimal
    r'\b\d{2,5}\.\d{1,2}\b',
    # Above/below/near (price context)
    r'(?i)\b(above|below|near|around|@)\s*\d',
    # Futures keywords
    r'(?i)\b(fut|future|futures|expiry|lot)\b',
]


@dataclass
class ChannelSpecificRules:
    """Per-channel configuration for signal combining and noise filtering.

    Attributes:
        channel_name: Human-readable channel name (for logging).
        combination_window_seconds: Override the default window for this channel.
            Channels that send rapid multi-part signals may need a shorter window;
            slower channels may need a longer one.
        max_messages_to_combine: Override the default max messages for this channel.
        noise_patterns: Additional regex patterns to treat as noise for this channel.
        signal_hint_patterns: Additional patterns that indicate partial signal data.
        always_single_message: If True, skip combining entirely for this channel
            (every message is treated independently, as the current behaviour).
        min_message_length: Messages shorter than this are treated as noise.
        require_symbol_in_first: If True, the first message in a combination must
            contain a recognizable symbol name to start buffering.
    """
    channel_name: str = ""
    combination_window_seconds: Optional[float] = None
    max_messages_to_combine: Optional[int] = None
    noise_patterns: List[str] = field(default_factory=list)
    signal_hint_patterns: List[str] = field(default_factory=list)
    always_single_message: bool = False
    min_message_length: int = 3
    require_symbol_in_first: bool = False


@dataclass
class BufferedMessage:
    """A message waiting in the combination buffer."""
    channel_id: str
    message_id: int
    text: str
    timestamp: float  # time.time() when received
    has_signal_hints: bool = False


@dataclass
class CombineResult:
    """Result of processing a message through the combiner."""
    parsed_data: Optional[Dict[str, Any]]  # The parsed signal (None if no signal)
    combined_text: str  # The text that was parsed (single or combined)
    source_message_ids: List[int]  # Message IDs that contributed
    was_combined: bool  # True if multiple messages were combined
    was_noise: bool  # True if filtered as noise


class MultiMessageSignalCombiner:
    """Buffers and combines multi-part trading signals from Telegram channels.

    Typical flow:
    1. Message arrives → check noise filter → if noise, skip.
    2. Try parsing as single message → if complete signal, return immediately.
    3. If partial/no signal, buffer it.
    4. Try combining buffered messages → if complete signal, return and clear buffer.
    5. If window expires, flush buffer (attempt final combine, then discard).

    Args:
        parser: Signal parser instance with a .parse(text, **kwargs) method.
        combination_window_seconds: Max seconds to wait for follow-up messages.
        max_messages_to_combine: Max messages to buffer per channel.
    """

    def __init__(
        self,
        parser,
        combination_window_seconds: float = 30.0,
        max_messages_to_combine: int = 5,
    ):
        self.parser = parser
        self.combination_window_seconds = combination_window_seconds
        self.max_messages_to_combine = max_messages_to_combine

        # Per-channel message buffers: {channel_id: [BufferedMessage, ...]}
        self._buffers: Dict[str, List[BufferedMessage]] = {}
        # Per-channel rules overrides
        self._channel_rules: Dict[str, ChannelSpecificRules] = {}
        # Per-channel flush timers
        self._flush_tasks: Dict[str, asyncio.Task] = {}

        # Compiled default patterns
        self._noise_compiled = [re.compile(p) for p in DEFAULT_NOISE_PATTERNS]
        self._signal_hint_compiled = [re.compile(p) for p in PARTIAL_SIGNAL_PATTERNS]

        # Per-channel compiled patterns (populated lazily)
        self._channel_noise_compiled: Dict[str, list] = {}
        self._channel_hint_compiled: Dict[str, list] = {}

        # Statistics
        self.stats = {
            'messages_processed': 0,
            'noise_filtered': 0,
            'single_message_signals': 0,
            'combined_signals': 0,
            'messages_combined_total': 0,  # total individual messages that became part of combines
            'buffer_flushes': 0,
            'buffer_expires': 0,
        }

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def add_channel_rules(self, channel_id: str, rules: ChannelSpecificRules):
        """Register per-channel combining/filtering rules."""
        self._channel_rules[str(channel_id)] = rules
        # Pre-compile channel-specific patterns
        if rules.noise_patterns:
            self._channel_noise_compiled[str(channel_id)] = [
                re.compile(p) for p in rules.noise_patterns
            ]
        if rules.signal_hint_patterns:
            self._channel_hint_compiled[str(channel_id)] = [
                re.compile(p) for p in rules.signal_hint_patterns
            ]
        logger.info(
            f"[COMBINER] Rules added for channel {channel_id} "
            f"({rules.channel_name or 'unnamed'}): "
            f"window={rules.combination_window_seconds or self.combination_window_seconds}s, "
            f"max_combine={rules.max_messages_to_combine or self.max_messages_to_combine}, "
            f"single_only={rules.always_single_message}"
        )

    def get_channel_rules(self, channel_id: str) -> ChannelSpecificRules:
        """Get rules for a channel (returns defaults if none configured)."""
        return self._channel_rules.get(str(channel_id), ChannelSpecificRules())

    def _get_window(self, channel_id: str) -> float:
        rules = self.get_channel_rules(channel_id)
        return rules.combination_window_seconds or self.combination_window_seconds

    def _get_max_combine(self, channel_id: str) -> int:
        rules = self.get_channel_rules(channel_id)
        return rules.max_messages_to_combine or self.max_messages_to_combine

    # ------------------------------------------------------------------
    # Noise filtering
    # ------------------------------------------------------------------

    def is_noise(self, text: str, channel_id: str = "") -> bool:
        """Check if a message is noise (non-trading content).

        Returns True if the message matches any noise pattern and does NOT
        contain signal hint patterns.
        """
        text_stripped = text.strip()
        channel_id = str(channel_id)
        rules = self.get_channel_rules(channel_id)

        # Too short
        if len(text_stripped) < rules.min_message_length:
            return True

        # Check default noise patterns
        for pattern in self._noise_compiled:
            if pattern.search(text_stripped):
                # But if it also has signal hints, it's not noise
                if self._has_signal_hints(text_stripped, channel_id):
                    return False
                return True

        # Check channel-specific noise patterns
        channel_noise = self._channel_noise_compiled.get(channel_id, [])
        for pattern in channel_noise:
            if pattern.search(text_stripped):
                if self._has_signal_hints(text_stripped, channel_id):
                    return False
                return True

        return False

    def _has_signal_hints(self, text: str, channel_id: str = "") -> bool:
        """Check if text contains patterns suggesting partial signal data."""
        # Default patterns
        match_count = 0
        for pattern in self._signal_hint_compiled:
            if pattern.search(text):
                match_count += 1
                if match_count >= 2:  # Need at least 2 hints to be a potential signal
                    return True

        # Channel-specific hint patterns
        channel_hints = self._channel_hint_compiled.get(str(channel_id), [])
        for pattern in channel_hints:
            if pattern.search(text):
                match_count += 1
                if match_count >= 2:
                    return True

        return False

    # ------------------------------------------------------------------
    # Signal completeness check
    # ------------------------------------------------------------------

    @staticmethod
    def _is_complete_signal(parsed_data: Optional[Dict]) -> bool:
        """Check if parsed data contains all required fields for a valid signal."""
        if not parsed_data:
            return False

        instrument_type = parsed_data.get('instrument_type', 'OPTIONS')

        if instrument_type == 'FUTURES':
            required = ['symbol', 'action', 'entry_price', 'stop_loss',
                        'expiry_date', 'quantity', 'instrument_type']
        else:
            required = ['symbol', 'strike', 'option_type', 'action',
                        'entry_price', 'stop_loss', 'expiry_date', 'quantity']

        for f in required:
            if f not in parsed_data or parsed_data[f] is None:
                return False
        return True

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    async def process_message(
        self,
        channel_id: str,
        message_text: str,
        message_id: int,
        **parser_kwargs,
    ) -> Optional[CombineResult]:
        """Process a message, potentially combining with buffered messages.

        Returns a CombineResult if a signal was produced (immediately or via
        combination). Returns None if the message was buffered and we're still
        waiting for more. Returns a CombineResult with was_noise=True if filtered.

        Note: When this returns None, a signal may still be produced later via
        the flush callback (register via set_flush_callback).
        """
        channel_id = str(channel_id)
        self.stats['messages_processed'] += 1

        # Store channel_id separately for parser calls (avoid passing it twice
        # to internal methods that also take channel_id as a positional arg)
        self._current_parser_kwargs = dict(parser_kwargs, channel_id=channel_id)

        rules = self.get_channel_rules(channel_id)

        # --- Step 1: Noise filter ---
        if self.is_noise(message_text, channel_id):
            self.stats['noise_filtered'] += 1
            logger.info(f"[COMBINER] Noise filtered: {message_text[:50]}...")
            return CombineResult(
                parsed_data=None,
                combined_text=message_text,
                source_message_ids=[message_id],
                was_combined=False,
                was_noise=True,
            )

        # --- Step 2: Try single-message parse ---
        parsed = self.parser.parse(message_text, **self._current_parser_kwargs)

        if self._is_complete_signal(parsed):
            # Complete signal from a single message — return immediately
            self.stats['single_message_signals'] += 1
            logger.info(f"[COMBINER] Complete single-message signal from msg {message_id}")

            # Clear any stale buffer for this channel since we got a fresh complete signal
            self._clear_buffer(channel_id)

            return CombineResult(
                parsed_data=parsed,
                combined_text=message_text,
                source_message_ids=[message_id],
                was_combined=False,
                was_noise=False,
            )

        # --- Step 3: Channel set to single-message only? ---
        if rules.always_single_message:
            # Don't buffer, return whatever we got (may be None or partial)
            if parsed:
                self.stats['single_message_signals'] += 1
            return CombineResult(
                parsed_data=parsed,
                combined_text=message_text,
                source_message_ids=[message_id],
                was_combined=False,
                was_noise=False,
            )

        # --- Step 4: Check if message has any signal-like content ---
        has_hints = self._has_signal_hints(message_text, channel_id)

        # If require_symbol_in_first and buffer is empty, first message must have a symbol
        if (rules.require_symbol_in_first
                and channel_id not in self._buffers
                and not has_hints):
            # Not starting a new buffer with a non-signal message
            return CombineResult(
                parsed_data=parsed,
                combined_text=message_text,
                source_message_ids=[message_id],
                was_combined=False,
                was_noise=False,
            )

        # --- Step 5: Buffer the message ---
        buffered_msg = BufferedMessage(
            channel_id=channel_id,
            message_id=message_id,
            text=message_text,
            timestamp=time.time(),
            has_signal_hints=has_hints,
        )
        self._add_to_buffer(channel_id, buffered_msg)

        # --- Step 6: Try combining all buffered messages ---
        combine_result = self._try_combine(channel_id)
        if combine_result and self._is_complete_signal(combine_result.parsed_data):
            self.stats['combined_signals'] += 1
            self.stats['messages_combined_total'] += len(combine_result.source_message_ids)
            logger.info(
                f"[COMBINER] Combined {len(combine_result.source_message_ids)} messages "
                f"into complete signal for channel {channel_id}"
            )
            self._clear_buffer(channel_id)
            return combine_result

        # --- Step 7: Schedule flush timer ---
        self._schedule_flush(channel_id)

        # Message buffered, no complete signal yet
        logger.info(
            f"[COMBINER] Message {message_id} buffered for channel {channel_id} "
            f"({len(self._buffers.get(channel_id, []))} in buffer, "
            f"waiting up to {self._get_window(channel_id)}s)"
        )
        return None

    # ------------------------------------------------------------------
    # Buffer management
    # ------------------------------------------------------------------

    def _add_to_buffer(self, channel_id: str, msg: BufferedMessage):
        """Add a message to the channel buffer, respecting max size."""
        if channel_id not in self._buffers:
            self._buffers[channel_id] = []

        buf = self._buffers[channel_id]
        max_combine = self._get_max_combine(channel_id)

        # Expire old messages outside the window
        window = self._get_window(channel_id)
        cutoff = time.time() - window
        self._buffers[channel_id] = [m for m in buf if m.timestamp >= cutoff]
        buf = self._buffers[channel_id]

        # Enforce max size (drop oldest if full)
        while len(buf) >= max_combine:
            dropped = buf.pop(0)
            logger.debug(f"[COMBINER] Dropped oldest buffered msg {dropped.message_id}")

        buf.append(msg)

    def _clear_buffer(self, channel_id: str):
        """Clear the buffer for a channel and cancel any pending flush."""
        self._buffers.pop(channel_id, None)
        self._cancel_flush(channel_id)

    def _try_combine(self, channel_id: str) -> Optional[CombineResult]:
        """Try combining all buffered messages for a channel into a signal.

        Tries combining messages in order (2, 3, ... up to all buffered).
        Returns the first successful complete signal found.
        """
        buf = self._buffers.get(channel_id, [])
        if len(buf) < 2:
            return None

        pk = getattr(self, '_current_parser_kwargs', {'channel_id': channel_id})

        # Try combining from most recent backwards (prioritize newest messages)
        # Also try combining all messages
        combinations_to_try = []

        # Try all messages combined
        combinations_to_try.append(buf[:])

        # Try pairs (most recent first)
        if len(buf) > 2:
            for i in range(len(buf) - 1):
                pair = [buf[i], buf[i + 1]]
                combinations_to_try.append(pair)

        for combo in combinations_to_try:
            combined_text = "\n".join(m.text for m in combo)
            msg_ids = [m.message_id for m in combo]

            parsed = self.parser.parse(combined_text, **pk)

            if self._is_complete_signal(parsed):
                return CombineResult(
                    parsed_data=parsed,
                    combined_text=combined_text,
                    source_message_ids=msg_ids,
                    was_combined=True,
                    was_noise=False,
                )

        return None

    # ------------------------------------------------------------------
    # Flush timer
    # ------------------------------------------------------------------

    # Callback for when a buffer flush produces a signal (or expires)
    _flush_callback: Optional[Callable] = None

    def set_flush_callback(self, callback: Callable):
        """Set a callback for when a buffered combine completes on timer expiry.

        The callback receives (channel_id: str, result: CombineResult).
        This is needed because process_message returns None when buffering,
        and the signal may only be produced when the timer fires.
        """
        self._flush_callback = callback

    def _schedule_flush(self, channel_id: str):
        """Schedule (or reschedule) a flush for the channel buffer."""
        self._cancel_flush(channel_id)
        window = self._get_window(channel_id)

        async def _flush():
            await asyncio.sleep(window)
            self._do_flush(channel_id)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._flush_tasks[channel_id] = asyncio.ensure_future(_flush())
            else:
                # No running loop — can't schedule. Buffer will be handled on next message.
                pass
        except RuntimeError:
            pass

    def _cancel_flush(self, channel_id: str):
        """Cancel a pending flush timer for a channel."""
        task = self._flush_tasks.pop(channel_id, None)
        if task and not task.done():
            task.cancel()

    def _do_flush(self, channel_id: str):
        """Flush handler: try final combine, then clear buffer."""
        buf = self._buffers.get(channel_id, [])
        if not buf:
            return

        self.stats['buffer_expires'] += 1
        pk = getattr(self, '_current_parser_kwargs', {'channel_id': channel_id})

        # Try one last combine
        result = self._try_combine(channel_id)
        if result and self._is_complete_signal(result.parsed_data):
            self.stats['combined_signals'] += 1
            self.stats['messages_combined_total'] += len(result.source_message_ids)
            logger.info(
                f"[COMBINER] Flush-combine produced signal from "
                f"{len(result.source_message_ids)} messages in channel {channel_id}"
            )
            if self._flush_callback:
                self._flush_callback(channel_id, result)
        else:
            # Try parsing each buffered message individually as a last resort
            # (it may have been a partial signal that's good enough)
            for msg in buf:
                parsed = self.parser.parse(msg.text, **pk)
                if parsed:
                    individual_result = CombineResult(
                        parsed_data=parsed,
                        combined_text=msg.text,
                        source_message_ids=[msg.message_id],
                        was_combined=False,
                        was_noise=False,
                    )
                    logger.info(
                        f"[COMBINER] Flush: individual parse produced partial signal "
                        f"from msg {msg.message_id}"
                    )
                    if self._flush_callback:
                        self._flush_callback(channel_id, individual_result)

            self.stats['buffer_flushes'] += 1
            logger.info(
                f"[COMBINER] Buffer expired for channel {channel_id} "
                f"({len(buf)} messages discarded)"
            )

        self._clear_buffer(channel_id)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_buffer_status(self) -> Dict[str, int]:
        """Get current buffer sizes per channel."""
        return {ch: len(msgs) for ch, msgs in self._buffers.items() if msgs}

    def get_stats(self) -> Dict[str, int]:
        """Get combiner statistics."""
        return dict(self.stats)

    def flush_all(self):
        """Flush all channel buffers immediately (e.g., on shutdown)."""
        for channel_id in list(self._buffers.keys()):
            self._do_flush(channel_id)

    def log_stats(self):
        """Log current combiner statistics."""
        s = self.stats
        logger.info("")
        logger.info("=" * 60)
        logger.info("MULTI-MESSAGE COMBINER STATISTICS")
        logger.info("=" * 60)
        logger.info(f"Messages Processed:     {s['messages_processed']}")
        logger.info(f"Noise Filtered:         {s['noise_filtered']}")
        logger.info(f"Single-Msg Signals:     {s['single_message_signals']}")
        logger.info(f"Combined Signals:       {s['combined_signals']}")
        logger.info(f"  Messages Combined:    {s['messages_combined_total']}")
        logger.info(f"Buffer Flushes:         {s['buffer_flushes']}")
        logger.info(f"Buffer Expires:         {s['buffer_expires']}")

        # Current buffer status
        buf_status = self.get_buffer_status()
        if buf_status:
            logger.info(f"Active Buffers:         {len(buf_status)}")
            for ch, count in buf_status.items():
                rules = self.get_channel_rules(ch)
                name = rules.channel_name or ch
                logger.info(f"  {name}: {count} messages waiting")
        else:
            logger.info(f"Active Buffers:         0")
        logger.info("=" * 60)
