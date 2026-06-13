"""
nse_live_feed.py
----------------
Live NIFTY/BANKNIFTY/FINNIFTY price feed from NSE public API.
No API key, no brokerage account needed.

How it works
------------
1. On startup: fetches today's full intraday chart from NSE to pre-load candles
2. Every 60s: polls live price from NSE allIndices endpoint
3. Builds rolling 1m / 5m / 15m OHLCV DataFrames in memory
4. OmniEngine calls get_candles(symbol, interval) instead of Dhan API

Supported symbols: NIFTY, BANKNIFTY, FINNIFTY
(SENSEX is BSE — not on NSE endpoint, skipped)

Also provides NSEFuturesFeed:
  Polls NSE quote-derivative API for near-month index futures LTP + volume + OI.
  Builds candles with real volume (cumulative-delta) + OI and persists to DB.
  Supported: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY (SENSEX is BSE — skipped)

Usage (standalone test)
-----------------------
    python nse_live_feed.py
"""

import time
import json
import threading
import logging
import http.cookiejar
import urllib.request
import urllib.parse
from datetime import datetime, date
from collections import defaultdict

import pandas as pd
import pytz

logger = logging.getLogger(__name__)

IST          = pytz.timezone("Asia/Kolkata")
MARKET_OPEN  = (9, 15)
MARKET_CLOSE = (15, 30)

# NSE API name → our symbol name
_NSE_INDEX_MAP = {
    "NIFTY 50":                  "NIFTY",
    "NIFTY BANK":                "BANKNIFTY",
    "NIFTY FIN SERVICE":         "FINNIFTY",   # older name
    "NIFTY FINANCIAL SERVICES":  "FINNIFTY",   # current name
}
_SYMBOL_TO_NSE = {v: k for k, v in _NSE_INDEX_MAP.items()}

# NSE chart API index codes (spot)
_NSE_CHART_CODE = {
    "NIFTY":     "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY":  "NIFTY FIN SERVICE",
}

# ── Futures feed config ────────────────────────────────────────────────────────

# NSE chart API codes for futures intraday history (no &indices=true)
_NSE_FUT_CHART_CODE = {
    "NIFTY":      "NIFTY FUT",
    "BANKNIFTY":  "BANKNIFTY FUT",
    "FINNIFTY":   "FINNIFTY FUT",
    "MIDCPNIFTY": "MIDCPNIFTY FUT",
}

# NSE derivative quote API symbol names
_NSE_DERIV_SYM = {
    "NIFTY":      "NIFTY",
    "BANKNIFTY":  "BANKNIFTY",
    "FINNIFTY":   "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
}

POLL_INTERVAL_S = 60        # poll every 60 seconds


class NSELiveFeed:
    """
    Thread-safe live price feed using NSE public HTTP API.
    Builds OHLCV candles from per-minute price snapshots.
    """

    def __init__(self, symbols=None, verbose=True):
        self.symbols  = symbols or ["NIFTY", "BANKNIFTY", "FINNIFTY"]
        self.verbose  = verbose

        # _ticks[symbol] = list of (datetime, price) tuples — raw 1-min snapshots
        self._ticks: dict[str, list] = defaultdict(list)
        self._lock   = threading.Lock()
        self._thread = None
        self._stop   = threading.Event()

        # Cached DataFrames per (symbol, interval)
        self._cache: dict[tuple, pd.DataFrame] = {}

        # HTTP opener with cookie support (NSE requires session cookie)
        self._opener = self._make_opener()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _make_opener(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        opener.addheaders = [
            ("User-Agent",
             "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/120.0.0.0 Safari/537.36"),
            ("Accept",          "application/json, text/plain, */*"),
            ("Accept-Language", "en-US,en;q=0.9"),
            ("Referer",         "https://www.nseindia.com"),
        ]
        return opener

    def _get(self, url: str, timeout: int = 10) -> dict:
        try:
            resp = self._opener.open(url, timeout=timeout)
            return json.loads(resp.read())
        except Exception as e:
            logger.debug(f"NSE HTTP error {url}: {e}")
            return {}

    # ------------------------------------------------------------------
    # Fetch live spot prices for all symbols in one call
    # ------------------------------------------------------------------

    def _fetch_live_prices(self) -> dict[str, float]:
        """
        Returns {symbol: price} for all tracked symbols.
        Uses NSE allIndices endpoint.
        """
        data = self._get("https://www.nseindia.com/api/allIndices")
        prices = {}
        for row in data.get("data", []):
            name  = row.get("index", "")
            sym   = _NSE_INDEX_MAP.get(name)
            if sym and sym in self.symbols:
                try:
                    prices[sym] = float(str(row["last"]).replace(",", ""))
                except (ValueError, KeyError):
                    pass
        return prices

    # ------------------------------------------------------------------
    # Fetch today's full intraday data on startup
    # ------------------------------------------------------------------

    def _load_todays_history(self):
        """
        Loads today's intraday ticks from NSE chart endpoint.
        NSE returns [[epoch_ms, price], ...] for the current trading day.
        Only works during/after market hours — safe to call at startup.
        """
        for sym in self.symbols:
            nse_name = _NSE_CHART_CODE.get(sym)
            if not nse_name:
                continue

            encoded = urllib.parse.quote(nse_name)
            url = (f"https://www.nseindia.com/api/chart-databyindex"
                   f"?index={encoded}&indices=true")

            raw = self._get(url, timeout=15)
            pts = raw.get("grapthData", [])
            if not pts:
                logger.debug(f"No chart history for {sym} from NSE")
                continue

            ticks = []
            for pt in pts:
                try:
                    ts_ms, price = pt[0], pt[1]
                    dt = datetime.fromtimestamp(ts_ms / 1000, tz=IST).replace(tzinfo=None)
                    ticks.append((dt, float(price)))
                except Exception:
                    continue

            if ticks:
                with self._lock:
                    self._ticks[sym] = ticks
                self._rebuild_cache(sym)
                logger.info(f"[NSEFeed] Loaded {len(ticks)} ticks for {sym} from NSE chart")
            else:
                logger.info(f"[NSEFeed] No chart history for {sym} (market may be closed)")

    # ------------------------------------------------------------------
    # Candle builder
    # ------------------------------------------------------------------

    def _ticks_to_candles(self, ticks: list, interval_minutes: int) -> pd.DataFrame:
        """
        Converts (datetime, price) tick list → OHLCV DataFrame with
        interval_minutes candle size.
        """
        if not ticks:
            return pd.DataFrame()

        df = pd.DataFrame(ticks, columns=["timestamp", "close"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)

        # Round down to candle boundary
        freq = f"{interval_minutes}min"
        df["candle_ts"] = df["timestamp"].dt.floor(freq)

        # Aggregate
        ohlcv = df.groupby("candle_ts").agg(
            open  = ("close", "first"),
            high  = ("close", "max"),
            low   = ("close", "min"),
            close = ("close", "last"),
        ).reset_index().rename(columns={"candle_ts": "timestamp"})

        # Add dummy volume (NSE index has no volume)
        ohlcv["volume"] = 0

        # Drop the still-open (current) candle — last candle is incomplete
        now_floor = pd.Timestamp.now().floor(freq)
        ohlcv = ohlcv[ohlcv["timestamp"] < now_floor].reset_index(drop=True)

        return ohlcv[["timestamp", "open", "high", "low", "close", "volume"]]

    def _rebuild_cache(self, sym: str):
        """Rebuild 1m/5m/15m candle DataFrames from current tick list."""
        with self._lock:
            ticks = list(self._ticks[sym])
        for interval in (1, 5, 15):
            df = self._ticks_to_candles(ticks, interval)
            with self._lock:
                self._cache[(sym, interval)] = df

    # ------------------------------------------------------------------
    # Background polling thread
    # ------------------------------------------------------------------

    def _poll_loop(self):
        logger.info("[NSEFeed] Polling thread started")
        while not self._stop.is_set():
            now_ist = datetime.now(IST)

            # Only poll during market hours on weekdays
            if (now_ist.weekday() < 5 and
                    MARKET_OPEN <= (now_ist.hour, now_ist.minute) <= MARKET_CLOSE):
                prices = self._fetch_live_prices()
                now_dt = now_ist.replace(tzinfo=None)

                updated = []
                for sym, price in prices.items():
                    with self._lock:
                        self._ticks[sym].append((now_dt, price))
                        # Keep only today's ticks
                        today_start = datetime.combine(date.today(),
                                                       datetime.min.time())
                        self._ticks[sym] = [
                            t for t in self._ticks[sym]
                            if t[0] >= today_start
                        ]
                    self._rebuild_cache(sym)
                    updated.append(f"{sym}={price:.1f}")

                if updated:
                    logger.info(f"[NSEFeed] Tick: {' | '.join(updated)}")
                else:
                    logger.warning("[NSEFeed] No prices received from NSE")
            else:
                logger.debug("[NSEFeed] Outside market hours — skipping poll")

            self._stop.wait(POLL_INTERVAL_S)

        logger.info("[NSEFeed] Polling thread stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start background polling. Call once at app startup."""
        logger.info("[NSEFeed] Starting — loading today's history first...")
        self._load_todays_history()

        # Seed with current live prices immediately
        prices = self._fetch_live_prices()
        now_dt = datetime.now(IST).replace(tzinfo=None)
        for sym, price in prices.items():
            with self._lock:
                # Only add if no ticks yet for this symbol
                if not self._ticks[sym]:
                    self._ticks[sym].append((now_dt, price))
            self._rebuild_cache(sym)

        # Start background thread
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="NSELiveFeed"
        )
        self._thread.start()
        logger.info(f"[NSEFeed] Live for: {self.symbols}")

    def stop(self):
        """Stop background polling."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_candles(self, symbol: str, interval: int) -> pd.DataFrame:
        """
        Returns OHLCV DataFrame for the given symbol and interval (minutes).
        Format matches what OmniEngine strategies expect:
            columns: timestamp, open, high, low, close, volume

        Returns empty DataFrame if no data yet (feed just started).
        """
        with self._lock:
            df = self._cache.get((symbol, interval), pd.DataFrame())
        return df.copy()

    def get_ltp(self, symbol: str) -> float:
        """Returns latest price for symbol, or 0 if unavailable."""
        with self._lock:
            ticks = self._ticks.get(symbol, [])
        return ticks[-1][1] if ticks else 0.0

    def status(self) -> dict:
        """Returns dict with tick count and last price per symbol."""
        out = {}
        for sym in self.symbols:
            with self._lock:
                ticks = self._ticks.get(sym, [])
            out[sym] = {
                "ticks":      len(ticks),
                "last_price": ticks[-1][1] if ticks else None,
                "last_time":  str(ticks[-1][0]) if ticks else None,
                "candles_5m": len(self._cache.get((sym, 5), pd.DataFrame())),
            }
        return out


# ---------------------------------------------------------------------------
# Singleton — used by OmniEngine
# ---------------------------------------------------------------------------
_feed_instance: NSELiveFeed | None = None

def get_feed() -> NSELiveFeed:
    """Return the global NSELiveFeed singleton (starts it if needed)."""
    global _feed_instance
    if _feed_instance is None:
        _feed_instance = NSELiveFeed()
        _feed_instance.start()
    return _feed_instance


# ---------------------------------------------------------------------------
# NSEFuturesFeed — near-month futures with real volume + OI
# ---------------------------------------------------------------------------

class NSEFuturesFeed:
    """
    Live feed for near-month index futures from NSE public API.

    Polls NSE quote-derivative every 60 s for LTP, cumulative volume, OI.
    Converts cumulative volume → per-candle delta volume.
    Builds 1m/5m/15m candles and persists them to candles_futures_*min tables.

    Supported: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY
    Not supported: SENSEX (BSE-listed, separate exchange API).

    Volume note:
      NSE reports totalTradedVolume as a running cumulative from market open.
      Each poll computes delta vs previous poll — so volume in early candles
      (before feed starts) will be 0; it accumulates correctly from start time.
      Today's chart history pre-loads price ticks but has volume=0.
    """

    def __init__(self, symbols: list[str] | None = None, persist_to_db: bool = True):
        self.symbols       = symbols or list(_NSE_DERIV_SYM.keys())
        self.persist_to_db = persist_to_db

        # Ticks: {sym: [(datetime, ltp, per_tick_vol, oi), ...]}
        self._ticks: dict[str, list] = defaultdict(list)
        self._lock   = threading.Lock()
        self._thread = None
        self._stop   = threading.Event()

        # Cached candle DataFrames: {(sym, interval): DataFrame}
        self._cache: dict[tuple, pd.DataFrame] = {}

        # Last cumulative volume seen — for delta calculation
        self._last_cum_vol: dict[str, int] = {}

        self._opener = self._make_opener()

    # ── HTTP ────────────────────────────────────────────────────────────────

    def _make_opener(self):
        jar    = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        opener.addheaders = [
            ("User-Agent",
             "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/120.0.0.0 Safari/537.36"),
            ("Accept",          "application/json, text/plain, */*"),
            ("Accept-Language", "en-US,en;q=0.9"),
            ("Referer",         "https://www.nseindia.com"),
        ]
        return opener

    def _get(self, url: str, timeout: int = 10) -> dict:
        try:
            resp = self._opener.open(url, timeout=timeout)
            return json.loads(resp.read())
        except Exception as e:
            logger.debug(f"[FutFeed] HTTP {url}: {e}")
            return {}

    # ── NSE parsers ─────────────────────────────────────────────────────────

    def _fetch_futures_tick(self, sym: str) -> tuple[float, int, int] | None:
        """
        Returns (ltp, cum_volume, oi) for near-month futures of sym.
        None if the API call fails or no futures row found.
        """
        nse_sym = _NSE_DERIV_SYM.get(sym)
        if not nse_sym:
            return None
        url = (f"https://www.nseindia.com/api/quote-derivative"
               f"?symbol={urllib.parse.quote(nse_sym)}")
        raw = self._get(url)

        records  = raw.get("records", {}).get("data", [])
        fut_rows = [r for r in records
                    if r.get("instrumentType") == "Index Futures"]
        if not fut_rows:
            return None

        def _exp(r):
            try:
                return datetime.strptime(r.get("expiryDate", "01-Jan-2099"),
                                         "%d-%b-%Y")
            except Exception:
                return datetime(2099, 1, 1)

        near = min(fut_rows, key=_exp)
        try:
            ltp = float(near.get("lastPrice",         0) or 0)
            vol = int(  near.get("totalTradedVolume",  0) or 0)
            oi  = int(  near.get("openInterest",       0) or 0)
            return ltp, vol, oi
        except (TypeError, ValueError):
            return None

    def _load_todays_history(self):
        """
        Pre-loads today's intraday price history from NSE chart API.
        Volume/OI are unknown from this endpoint — filled with 0.
        """
        for sym in self.symbols:
            chart_code = _NSE_FUT_CHART_CODE.get(sym)
            if not chart_code:
                continue
            url = (f"https://www.nseindia.com/api/chart-databyindex"
                   f"?index={urllib.parse.quote(chart_code)}")
            raw = self._get(url, timeout=15)
            pts = raw.get("grapthData", [])
            if not pts:
                logger.debug(f"[FutFeed] No chart history for {sym} FUT")
                continue

            ticks = []
            for pt in pts:
                try:
                    ts_ms, price = pt[0], pt[1]
                    dt = datetime.fromtimestamp(ts_ms / 1000, tz=IST).replace(tzinfo=None)
                    ticks.append((dt, float(price), 0, 0))  # vol/OI unknown
                except Exception:
                    continue

            if ticks:
                with self._lock:
                    self._ticks[sym] = ticks
                self._rebuild_cache(sym, persist=False)
                logger.info(
                    f"[FutFeed] Loaded {len(ticks)} history ticks "
                    f"for {sym} FUT (vol=0 until live feed catches up)"
                )

    # ── Candle builder ──────────────────────────────────────────────────────

    def _ticks_to_candles(self, ticks: list, interval_minutes: int) -> pd.DataFrame:
        """
        Converts [(dt, ltp, per_tick_vol, oi), ...] → OHLCV+OI DataFrame.
        """
        if not ticks:
            return pd.DataFrame()
        df = pd.DataFrame(ticks, columns=["timestamp", "close", "tick_vol", "oi"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)

        freq = f"{interval_minutes}min"
        df["candle_ts"] = df["timestamp"].dt.floor(freq)

        agg = (df.groupby("candle_ts")
                 .agg(open   = ("close",    "first"),
                      high   = ("close",    "max"),
                      low    = ("close",    "min"),
                      close  = ("close",    "last"),
                      volume = ("tick_vol", "sum"),
                      oi     = ("oi",       "last"))
                 .reset_index()
                 .rename(columns={"candle_ts": "timestamp"}))

        # Drop the currently-open (incomplete) candle
        now_floor = pd.Timestamp.now().floor(freq)
        agg = agg[agg["timestamp"] < now_floor].reset_index(drop=True)
        return agg[["timestamp", "open", "high", "low", "close", "volume", "oi"]]

    def _rebuild_cache(self, sym: str, persist: bool = True):
        with self._lock:
            ticks      = list(self._ticks[sym])
            prev_count = len(self._cache.get((sym, 1), pd.DataFrame()))

        dfs = {iv: self._ticks_to_candles(ticks, iv) for iv in (1, 5, 15)}

        with self._lock:
            for iv, df in dfs.items():
                self._cache[(sym, iv)] = df
            new_count = len(dfs[1])

        # Persist only when a completed 1m candle is new
        if persist and self.persist_to_db and new_count > prev_count:
            self._persist(sym)

    def _persist(self, sym: str):
        try:
            from futures_candle_store import _save_df, FUTURES_CONFIG
            sym_key = FUTURES_CONFIG.get(sym, {}).get("sym")
            if not sym_key:
                return
            for iv in (1, 5, 15):
                df = self.get_candles(sym, iv)
                if not df.empty:
                    _save_df(sym_key, df, iv)
            logger.debug(f"[FutFeed] DB flush: {sym}")
        except Exception as e:
            logger.warning(f"[FutFeed] persist error {sym}: {e}")

    # ── Background polling ──────────────────────────────────────────────────

    def _poll_loop(self):
        logger.info("[FutFeed] Futures polling thread started")
        while not self._stop.is_set():
            now_ist = datetime.now(IST)
            if (now_ist.weekday() < 5 and
                    MARKET_OPEN <= (now_ist.hour, now_ist.minute) <= MARKET_CLOSE):
                now_dt = now_ist.replace(tzinfo=None)
                for sym in self.symbols:
                    result = self._fetch_futures_tick(sym)
                    if result:
                        ltp, cum_vol, oi = result
                        prev_cv  = self._last_cum_vol.get(sym, cum_vol)
                        tick_vol = max(0, cum_vol - prev_cv)
                        self._last_cum_vol[sym] = cum_vol

                        with self._lock:
                            self._ticks[sym].append((now_dt, ltp, tick_vol, oi))
                            today_start = datetime.combine(
                                date.today(), datetime.min.time()
                            )
                            self._ticks[sym] = [
                                t for t in self._ticks[sym]
                                if t[0] >= today_start
                            ]
                        self._rebuild_cache(sym)
                        logger.info(
                            f"[FutFeed] {sym} FUT: {ltp:.1f}  "
                            f"vol+={tick_vol:,}  oi={oi:,}"
                        )
                    else:
                        logger.debug(f"[FutFeed] No data for {sym} FUT")
            else:
                logger.debug("[FutFeed] Outside market hours — skipping poll")

            self._stop.wait(POLL_INTERVAL_S)
        logger.info("[FutFeed] Futures polling thread stopped")

    # ── Public API ──────────────────────────────────────────────────────────

    def start(self):
        """Start background polling. Loads today's price history first."""
        logger.info("[FutFeed] Starting — loading today's history...")
        self._load_todays_history()

        # Seed live tick immediately so LTP is available right away
        now_dt = datetime.now(IST).replace(tzinfo=None)
        for sym in self.symbols:
            result = self._fetch_futures_tick(sym)
            if result:
                ltp, cum_vol, oi = result
                self._last_cum_vol[sym] = cum_vol  # baseline — first delta = 0
                with self._lock:
                    if not self._ticks[sym]:
                        self._ticks[sym].append((now_dt, ltp, 0, oi))
                self._rebuild_cache(sym, persist=False)

        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="NSEFuturesFeed"
        )
        self._thread.start()
        logger.info(f"[FutFeed] Live for: {self.symbols}")

    def stop(self):
        """Stop polling and flush all candles to DB."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.persist_to_db:
            for sym in self.symbols:
                self._persist(sym)

    def get_candles(self, symbol: str, interval: int) -> pd.DataFrame:
        """
        Returns OHLCV+OI DataFrame for symbol (e.g. "NIFTY") and interval (minutes).
        Columns: timestamp, open, high, low, close, volume, oi
        """
        with self._lock:
            df = self._cache.get((symbol, interval), pd.DataFrame())
        return df.copy()

    def get_ltp(self, symbol: str) -> float:
        """Latest futures LTP, or 0 if not yet available."""
        with self._lock:
            ticks = self._ticks.get(symbol, [])
        return ticks[-1][1] if ticks else 0.0

    def status(self) -> dict:
        out = {}
        for sym in self.symbols:
            with self._lock:
                ticks = self._ticks.get(sym, [])
            out[sym] = {
                "ticks":      len(ticks),
                "last_price": ticks[-1][1] if ticks else None,
                "last_oi":    ticks[-1][3] if ticks else None,
                "last_time":  str(ticks[-1][0]) if ticks else None,
                "candles_1m": len(self._cache.get((sym, 1), pd.DataFrame())),
            }
        return out


# ---------------------------------------------------------------------------
# Futures feed singleton — used by OmniEngine
# ---------------------------------------------------------------------------
_futures_feed_instance: NSEFuturesFeed | None = None

def get_futures_feed() -> NSEFuturesFeed:
    """Return the global NSEFuturesFeed singleton (starts it if needed)."""
    global _futures_feed_instance
    if _futures_feed_instance is None:
        _futures_feed_instance = NSEFuturesFeed()
        _futures_feed_instance.start()
    return _futures_feed_instance


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--futures", action="store_true",
                    help="Test futures feed instead of spot feed")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )

    if args.futures:
        print("\n  NSE Futures Feed — standalone test")
        print("  Ctrl+C to stop\n")

        fut_feed = NSEFuturesFeed(persist_to_db=False)
        fut_feed.start()

        try:
            while True:
                time.sleep(30)
                print("\n  --- Futures Status ---")
                for sym, info in fut_feed.status().items():
                    print(f"  {sym:<12} ltp={info['last_price']}  "
                          f"oi={info['last_oi']}  ticks={info['ticks']}  "
                          f"1m_candles={info['candles_1m']}")

                df1 = fut_feed.get_candles("NIFTY", 1)
                if not df1.empty:
                    print(f"\n  NIFTY FUT 1m candles (last 3):")
                    print(df1.tail(3).to_string(index=False))
        except KeyboardInterrupt:
            print("\n  Stopping...")
            fut_feed.stop()
    else:
        print("\n  NSE Spot Live Feed — standalone test")
        print("  Ctrl+C to stop\n")

        feed = NSELiveFeed(verbose=True)
        feed.start()

        try:
            while True:
                time.sleep(30)
                print("\n  --- Spot Status ---")
                for sym, info in feed.status().items():
                    print(f"  {sym:<12} price={info['last_price']}  "
                          f"ticks={info['ticks']}  5m_candles={info['candles_5m']}")

                df5 = feed.get_candles("NIFTY", 5)
                if not df5.empty:
                    print(f"\n  NIFTY 5m candles (last 3):")
                    print(df5.tail(3).to_string(index=False))
        except KeyboardInterrupt:
            print("\n  Stopping...")
            feed.stop()
