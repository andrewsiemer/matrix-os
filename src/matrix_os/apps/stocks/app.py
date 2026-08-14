"""
Stocks App

Displays stock price and chart from TwelveData API.
Uses SQLite for caching data between app restarts.

API Strategy:
- market_state drives scheduling via time_to_open/time_to_close. It is billed
  like any other call, so it is shared across symbols and the overnight wait is
  capped on the local clock rather than by polling
- When market is OPEN: refresh at an adaptive rate that spends the day's
  remaining API credits evenly over the rest of the session
- When market is CLOSED: one closing snapshot, then no billed calls until
  the next session
- Credits are metered per API key, not per process, so the ledger is shared
  through SQLite (see ApiBudget). Running out means waiting for the next
  trading day rather than degrading into failed requests.
"""

import logging
import re
import threading
import time
import zoneinfo
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests
from PIL import Image, ImageDraw

from ...core.display import FrameBuffer
from ..base import AppManifest, BaseApp
from ..fonts import get_font
from .db import ApiBudget, StockCache, StockData, get_trading_date

log = logging.getLogger(__name__)

# Module-level storage for shared state (avoids pickle issues with class-level locks)
_stocks_cache: Optional[StockCache] = None
_stocks_cache_lock = threading.Lock()
_api_budget: Optional[ApiBudget] = None
_api_budget_lock = threading.Lock()

# Previous-day close, keyed by trading day. Constant for a whole session, so it
# is worth one credit per day rather than one per refresh.
_close_prices: Dict[str, float] = {}

# Scheduled update times (shared across instances)
_next_data_update: float = 0
_next_market_check: float = 0
_market_is_open: bool = False
_current_trading_day: Optional[str] = None
_previous_trading_day: Optional[str] = None
_market_check_in_flight: bool = False
_final_fetch_done: bool = False
_market_close_ts: float = 0.0
_schedule_lock = threading.Lock()

# Fallback update interval when the budget cannot be consulted
_MARKET_OPEN_UPDATE_INTERVAL = 3 * 60

# Credit cost per request. market_state is free; every time_series call bills one.
_CREDITS_PER_REQUEST = 1

# Held back from the daily allowance so the closing snapshot and the next
# morning's trading-day lookups are always affordable, however busy the session.
_CREDIT_RESERVE = 25

# Bounds on the adaptive refresh rate. Faster than a minute wastes credits on a
# 64px chart; slower than 15 minutes stops looking live.
_MIN_UPDATE_INTERVAL = 60
_MAX_UPDATE_INTERVAL = 15 * 60

# Retry interval after any market-state failure
_MARKET_CHECK_RETRY_INTERVAL = 5 * 60

# Absolute ceiling on the gap between market checks, so a bad schedule can never
# strand the app on a stale trading day. market_state is billed, so this is a
# backstop only -- _seconds_until_expected_open does the real overnight capping.
_MAX_MARKET_CHECK_INTERVAL = 4 * 60 * 60

# How far back to walk when hunting for a trading day before giving up
_MAX_TRADING_DAY_LOOKBACK = 10


class _BudgetExhausted(Exception):
    """Today's API allowance cannot cover a call. Wait for the next session."""


def _redact(url: str) -> str:
    """Strip the API key from a URL before it reaches the logs.

    The web UI exposes a log viewer, and journald keeps these for weeks -- a key
    logged once is a key leaked to everyone who can see the display's logs.
    """
    return re.sub(r"(apikey=)[^&\s]+", r"\1***", str(url))


class StocksApp(BaseApp):
    """Stock price and chart display."""

    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="Stocks",
            version="1.0.0",
            description="Stock price and chart display",
            framerate=1,
        )

    @classmethod
    def _get_cache(cls) -> StockCache:
        """Get or create the shared cache."""
        global _stocks_cache
        with _stocks_cache_lock:
            if _stocks_cache is None:
                _stocks_cache = StockCache()
            return _stocks_cache

    def _get_budget(self) -> ApiBudget:
        """Get or create the shared daily credit ledger."""
        global _api_budget  # noqa: PLW0603
        with _api_budget_lock:
            if _api_budget is None:
                _api_budget = ApiBudget(daily_limit=self._daily_credits)
            return _api_budget

    def _spend(self, credits: int = _CREDITS_PER_REQUEST, reserve: int = _CREDIT_RESERVE) -> bool:
        """Claim credits before an API call. False means: do not make the call."""
        if not self._get_budget().try_spend(credits, reserve=reserve):
            log.info(
                "%s: daily API credit budget exhausted (%d used), holding until tomorrow",
                self._symbol,
                self._get_budget().used(),
            )
            return False
        return True

    def __init__(self, *args, symbol: str = "NVDA", **kwargs):
        super().__init__(*args, **kwargs)

        self._symbol = symbol
        self._api_key = self.get_env("stocks_api_key", "")
        self._daily_credits = int(self.get_env("stocks_daily_api_credits", 800) or 800)
        self._timezone = "America/New_York"
        self._exchange = "NYSE"
        self._open_time = 390  # minutes in stock day

        # Data
        self._current_price: Optional[float] = None
        self._close_price: Optional[float] = None
        self._diff: Optional[float] = None
        self._percent: Optional[float] = None
        self._graph_data: List[tuple] = []
        self._inflection_pt: int = 0

        # State
        self._is_fetching = False
        self._data_lock = threading.Lock()
        self._font = None
        self._initialized = False

    def __getstate__(self):
        """Custom pickle support - exclude unpicklable objects."""
        state = super().__getstate__()
        if "_data_lock" in state:
            del state["_data_lock"]
        return state

    def __setstate__(self, state):
        """Custom unpickle support - restore locks."""
        super().__setstate__(state)
        self._data_lock = threading.Lock()

    def on_start(self) -> None:
        """Initialize and load cached data."""
        font_path = self.get_font_path("5x6.bdf")
        self._font = get_font(font_path)

        # Load cached data if available
        cache = self._get_cache()
        cached = cache.get(self._symbol)

        if cached:
            with self._data_lock:
                self._current_price = cached.current_price
                self._close_price = cached.close_price
                self._diff = cached.difference
                self._percent = cached.percent
                self._graph_data = [tuple(v) for v in cached.graph_values]
                self._inflection_pt = cached.inflection_pt

            log.info(
                "Loaded cached data for %s: $%.2f (from %s)",
                self._symbol,
                cached.current_price,
                cached.trading_day,
            )

        # Trigger initial market state check
        self._schedule_market_check_now()

    def _schedule_market_check_now(self) -> None:
        """Schedule an immediate market state check."""
        global _next_market_check  # noqa: PLW0603
        with _schedule_lock:
            _next_market_check = 0

    def _schedule_market_check(self, delay: float) -> None:
        """Schedule the next market state check, clamped so it can never be lost."""
        global _next_market_check  # noqa: PLW0603
        # Waking before the next plausible open is wasted spend, but sleeping
        # past it costs a whole session -- so cap on the local clock, for free.
        delay = min(delay, self._seconds_until_expected_open() + 60)
        delay = max(60.0, min(delay, _MAX_MARKET_CHECK_INTERVAL))
        with _schedule_lock:
            _next_market_check = time.time() + delay
        log.info("Next market check for %s in %.1f min", self._symbol, delay / 60)

    @staticmethod
    def _seconds_until_expected_open() -> float:
        """Seconds to the next weekday 9:30 ET, computed locally and for free.

        A holiday still reports an open here; the market state lookup that
        follows will say otherwise and the app simply sleeps again. Costing one
        credit on a holiday morning beats paying to poll all night.
        """
        eastern = zoneinfo.ZoneInfo("America/New_York")
        now = datetime.now(eastern)

        candidate = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        while candidate.weekday() > 4:
            candidate += timedelta(days=1)

        return max(0.0, (candidate - now).total_seconds())

    def _compute_update_interval(self) -> Optional[float]:
        """Spread this symbol's remaining credits evenly over the rest of the session.

        Returns the seconds to wait before the next refresh, or None when the
        budget is spent -- in which case the caller stops polling until the next
        trading day resets the ledger.
        """
        budget = self._get_budget()
        budget.heartbeat(self._symbol)

        with _schedule_lock:
            seconds_left = _market_close_ts - time.time()

        if seconds_left <= 0:
            return None

        # Every symbol sharing the key gets an equal cut of what is left
        share = max(0, budget.remaining() - _CREDIT_RESERVE) / budget.active_symbols()
        affordable_updates = share / _CREDITS_PER_REQUEST

        if affordable_updates < 1:
            return None

        interval = seconds_left / affordable_updates
        interval = max(_MIN_UPDATE_INTERVAL, min(interval, _MAX_UPDATE_INTERVAL))

        log.info(
            "%s: %d credits left, %d symbols sharing, %.0f min to close -> refresh every %.1f min",
            self._symbol,
            budget.remaining(),
            budget.active_symbols(),
            seconds_left / 60,
            interval / 60,
        )
        return interval

    @staticmethod
    def _parse_duration(value: Optional[str], default_minutes: int = 5) -> float:
        """Parse a TwelveData 'H:MM:SS' duration into seconds."""
        try:
            parts = str(value).split(":")
            return int(parts[0]) * 3600 + int(parts[1]) * 60
        except (AttributeError, IndexError, TypeError, ValueError):
            log.warning("Could not parse duration %r, using %d min", value, default_minutes)
            return default_minutes * 60

    def _get_market_state(self) -> Optional[Dict]:
        """Get market state for the exchange. Billed, so shared and cached.

        Raises `_BudgetExhausted` when the day's allowance cannot cover it.
        """
        budget = self._get_budget()

        shared = budget.get_market_state(self._exchange)
        if shared is not None:
            log.debug("Market state for %s served from shared cache", self._exchange)
            return shared

        if not self._spend():
            raise _BudgetExhausted(f"no credits for {self._exchange} market state")

        try:
            url = f"https://api.twelvedata.com/market_state?exchange={self._exchange}&apikey={self._api_key}"
            response = requests.get(url, timeout=10)
            data = response.json()

            if isinstance(data, list) and len(data) > 0:
                log.info("Market state: %s", data[0])
                budget.set_market_state(self._exchange, data[0])
                return data[0]
            elif isinstance(data, dict) and data.get("status") == "error":
                log.warning("Market state API error: %s", data.get("message"))
                return None
            return None
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("Failed to get market state: %s", e)
            return None

    def _is_trading_day(self, day: datetime) -> bool:
        """Check if a given day is a trading day.

        Whether a past date traded never changes, so verdicts are cached
        permanently -- weekends and holidays cost one credit once, ever.
        """
        day_str = day.strftime("%Y-%m-%d")
        budget = self._get_budget()

        cached = budget.get_trading_day(day_str)
        if cached is not None:
            log.debug("is_trading_day %s: %s (cached)", day_str, cached)
            return cached

        # Weekends are free to rule out
        if day.weekday() > 4:
            budget.set_trading_day(day_str, False)
            return False

        # Draws on the reserve: resolving the calendar is what the reserve is for,
        # it is cached forever after, and nothing else works without it.
        if not self._spend(reserve=0):
            raise _BudgetExhausted(f"no credits to resolve trading day {day_str}")

        result = self._query_is_trading_day(day)

        # "No data yet" for today means the session has not opened, not that the
        # market is closed -- caching that would strand us for the rest of the day.
        if result or day_str < get_trading_date():
            budget.set_trading_day(day_str, result)

        return result

    def _query_is_trading_day(self, day: datetime) -> bool:
        """Ask the API whether `day` has intraday data. Costs one credit."""
        try:
            from twelvedata import TDClient, exceptions

            td = TDClient(apikey=self._api_key)
            ts = td.time_series(
                symbol=self._symbol,
                interval="1min",
                outputsize=1,
                start_date=day,
                end_date=day + timedelta(minutes=self._open_time),
                timezone=self._timezone,
            )

            tries = 5
            while tries > 0:
                try:
                    res = ts.as_json()
                    log.info("is_trading_day %s: %s", day.strftime("%Y-%m-%d"), bool(res))
                    return bool(res)
                except exceptions.BadRequestError:
                    log.warning("is_trading_day bad request")
                    return False
                except (exceptions.TwelveDataError, requests.RequestException):
                    timeout = 61 - datetime.now().second
                    log.warning("is_trading_day rate limited, waiting %d seconds", timeout)
                    tries -= 1
                    time.sleep(timeout)
            return False
        except Exception as e:
            log.warning("is_trading_day failed: %s", e)
            return False

    def _get_trading_days(self) -> tuple:
        """Get current and previous trading days."""
        eastern = zoneinfo.ZoneInfo("America/New_York")
        now = datetime.now(eastern)

        open_hour = 9
        open_min = 30

        # If before market open, use previous day
        if (now.hour * 60 + now.minute) < (open_hour * 60 + open_min):
            trading_day = (now - timedelta(days=1)).replace(
                hour=open_hour, minute=open_min, second=0, microsecond=0
            )
        else:
            trading_day = now.replace(hour=open_hour, minute=open_min, second=0, microsecond=0)

        # Skip weekends and non-trading days
        trading_day = self._walk_back_to_trading_day(trading_day)

        # Get previous trading day
        previous_day = self._walk_back_to_trading_day(trading_day - timedelta(days=1))

        return trading_day, previous_day

    def _walk_back_to_trading_day(self, day: datetime) -> datetime:
        """Walk backwards from `day` to the nearest trading day.

        Bounded: when the API is down or out of credits `_is_trading_day` reports
        False for every day, which would otherwise walk backwards forever, burning
        a request per iteration and never returning.
        """
        for _ in range(_MAX_TRADING_DAY_LOOKBACK):
            if self._is_trading_day(day):
                return day
            day -= timedelta(days=1)

        raise RuntimeError(
            f"No trading day found within {_MAX_TRADING_DAY_LOOKBACK} days of {day:%Y-%m-%d}"
        )

    def _update_market_state(self) -> None:
        """Update market state and schedule next updates.

        Runs on a background thread. Every exit path must leave a finite
        `_next_market_check` behind, otherwise the app silently stops updating
        and displays the last cached trading day until the process restarts.
        """
        global _next_data_update, _market_is_open, _market_close_ts  # noqa: PLW0603
        global _current_trading_day, _previous_trading_day  # noqa: PLW0603
        global _market_check_in_flight, _final_fetch_done  # noqa: PLW0603

        try:
            log.info("Checking market state for %s", self._symbol)

            market_state = self._get_market_state()

            if market_state is None:
                self._schedule_market_check(_MARKET_CHECK_RETRY_INTERVAL)
                log.warning("Market state API failed, retrying shortly")
                return

            is_open = bool(market_state.get("is_market_open"))

            with _schedule_lock:
                _market_is_open = is_open
                needs_final_fetch = not _final_fetch_done
                if is_open:
                    _market_close_ts = time.time() + self._parse_duration(
                        market_state.get("time_to_close")
                    )
                else:
                    _market_close_ts = 0.0
                    _next_data_update = float("inf")

            if is_open:
                delay = self._parse_duration(market_state.get("time_to_close")) + 300
                interval = self._compute_update_interval()
                with _schedule_lock:
                    _next_data_update = (
                        time.time() + interval if interval is not None else float("inf")
                    )
                if interval is None:
                    log.info("Market OPEN but credits exhausted - waiting for next trading day")
            else:
                delay = self._parse_duration(market_state.get("time_to_open")) + 300
                log.info("Market CLOSED")
            self._schedule_market_check(delay)

            # Resolving trading days and fetching costs API credits, so only do it
            # while the market is open or once to capture the closing values.
            if not is_open and not needs_final_fetch:
                log.info("Market closed and closing data already captured, skipping fetch")
                return

            trading_day, previous_day = self._get_trading_days()

            with _schedule_lock:
                _current_trading_day = trading_day.strftime("%Y-%m-%d")
                _previous_trading_day = previous_day.strftime("%Y-%m-%d")
                # Market open invalidates the previous close's final snapshot
                _final_fetch_done = not is_open

            log.info(
                "Current trading day: %s, Previous: %s", _current_trading_day, _previous_trading_day
            )

            self._fetch_data(trading_day, previous_day, final=not is_open)

        except _BudgetExhausted as e:
            # Retrying costs nothing but achieves nothing. Sleep until the
            # ledger has rolled over and there is something to spend again.
            log.info("%s: %s - waiting for the next trading day", self._symbol, e)
            self._schedule_market_check(self._seconds_until_expected_open() + 60)
        except Exception as e:
            log.warning("Market state update failed for %s: %s", self._symbol, e)
            self._schedule_market_check(_MARKET_CHECK_RETRY_INTERVAL)
        finally:
            with _schedule_lock:
                _market_check_in_flight = False

    def _fetch_data(
        self, trading_day: datetime, previous_day: datetime, final: bool = False
    ) -> None:
        """Fetch stock data from API.

        `final` marks the one post-close snapshot that leaves the display showing
        correct closing values all evening. It may draw on the credit reserve,
        because arriving at the close with nothing left to spend is exactly the
        outcome the reserve exists to prevent.
        """
        if self._is_fetching:
            return

        self._is_fetching = True
        reserve = 0 if final else _CREDIT_RESERVE
        log.info("Fetching data for %s%s", self._symbol, " (closing snapshot)" if final else "")

        def fetch():
            try:
                from twelvedata import TDClient

                td = TDClient(apikey=self._api_key)

                close_price = self._get_close_price(td, trading_day, previous_day, reserve)
                if close_price is None:
                    return

                if not self._spend(reserve=reserve):
                    return

                # Get trading day intraday data
                ts = td.time_series(
                    symbol=self._symbol,
                    interval="1min",
                    start_date=trading_day,
                    end_date=trading_day + timedelta(minutes=self._open_time),
                    outputsize=self._open_time,
                    timezone=self._timezone,
                )

                data = self._try_api(ts)
                if not data:
                    log.warning("No intraday data for %s", self._symbol)
                    return

                current_price = float(data[0]["close"])
                diff = current_price - close_price
                percent = (diff / close_price) * 100 if close_price else 0

                graph_result = self._build_graph(data, close_price, trading_day)
                graph_values = graph_result["values"]
                inflection_pt = graph_result["inflection_pt"]

                trading_day_str = trading_day.strftime("%Y-%m-%d")

                with self._data_lock:
                    self._current_price = current_price
                    self._close_price = close_price
                    self._diff = diff
                    self._percent = percent
                    self._graph_data = graph_values
                    self._inflection_pt = inflection_pt

                # Save to cache
                cache = self._get_cache()
                cache.set(
                    StockData(
                        symbol=self._symbol,
                        current_price=current_price,
                        close_price=close_price,
                        difference=diff,
                        percent=percent,
                        inflection_pt=inflection_pt,
                        graph_values=graph_values,
                        trading_day=trading_day_str,
                        updated=time.time(),
                    )
                )

                log.info("Stock data updated: %s = $%.2f", self._symbol, current_price)

            except Exception as e:
                log.warning("Stock fetch failed: %s", e)
            finally:
                self._is_fetching = False

        try:
            thread = threading.Thread(target=fetch, daemon=True)
            thread.start()
        except Exception as e:
            self._is_fetching = False
            log.warning("Could not start stock fetch thread: %s", e)

    def _get_close_price(
        self, td, trading_day: datetime, previous_day: datetime, reserve: int = _CREDIT_RESERVE
    ) -> Optional[float]:
        """Previous session's close, fetched at most once per trading day.

        This anchors the chart's zero line and the daily change, and it does not
        move once the session opens -- so re-fetching it on every refresh would
        double the cost of the whole app for no new information.
        """
        trading_day_str = trading_day.strftime("%Y-%m-%d")
        key = f"{self._symbol}:{trading_day_str}"

        if key in _close_prices:
            return _close_prices[key]

        # Survives a process restart: a cached row for this day already has it
        cached = self._get_cache().get(self._symbol)
        if cached and cached.trading_day == trading_day_str and cached.close_price:
            _close_prices[key] = cached.close_price
            log.debug("Reusing stored close price for %s: %.2f", key, cached.close_price)
            return cached.close_price

        if not self._spend(reserve=reserve):
            return None

        ts_daily = td.time_series(
            symbol=self._symbol,
            interval="1day",
            outputsize=1,
            start_date=previous_day,
            end_date=previous_day + timedelta(minutes=self._open_time),
            timezone=self._timezone,
        )

        daily_data = self._try_api(ts_daily)
        if not daily_data:
            log.warning("No daily data for %s", self._symbol)
            return None

        close_price = float(daily_data[0]["close"])
        _close_prices[key] = close_price
        log.info("Fetched close price for %s: %.2f", key, close_price)
        return close_price

    def _try_api(self, ts) -> Optional[List[Dict]]:
        """Try API call with retry logic (matches original implementation)."""
        from twelvedata import exceptions

        tries = 5
        while tries > 0:
            try:
                return ts.as_json()
            except exceptions.BadRequestError:
                log.warning("API bad request: %s", _redact(ts.as_url()))
                return None
            except (exceptions.TwelveDataError, requests.RequestException):
                timeout = 61 - datetime.now().second
                log.warning(
                    "API out of credits, retrying in %d seconds (%s)",
                    timeout,
                    _redact(ts.as_url()),
                )
                tries -= 1
                time.sleep(timeout)

        log.error("API errors continue after several attempts")
        return None

    def _build_graph(
        self, data: List[Dict], close_price: float, market_open: datetime = None
    ) -> Dict:
        """Build graph data from time series."""
        if not data:
            return {"values": [], "inflection_pt": 0}

        graph_width = 64
        timestamps = [
            int(round(i * (self._open_time - 1) / (graph_width - 1))) for i in range(graph_width)
        ]

        if market_open is None:
            try:
                oldest_dt_str = data[-1]["datetime"]
                oldest_dt = datetime.strptime(oldest_dt_str, "%Y-%m-%d %H:%M:%S")
                market_open = oldest_dt.replace(hour=9, minute=30, second=0, microsecond=0)
            except (ValueError, KeyError, IndexError):
                return {"values": [], "inflection_pt": 0}

        data_lookup = {point["datetime"]: point for point in data}

        samples = []
        prev_time = market_open - timedelta(minutes=1)

        for idx, delta in enumerate(timestamps):
            target_time = market_open + timedelta(minutes=delta)
            sample = None
            tries = 5

            while sample is None and tries > 0 and target_time > prev_time:
                target_str = target_time.strftime("%Y-%m-%d %H:%M:%S")
                if target_str in data_lookup:
                    sample = data_lookup[target_str]
                else:
                    target_time -= timedelta(minutes=1)
                tries -= 1

            if sample:
                prev_time = target_time
                if idx == 0:
                    samples.append(float(sample["open"]))
                else:
                    samples.append(float(sample["close"]))
            else:
                break

        if not samples:
            return {"values": [], "inflection_pt": 0}

        max_val = max(*samples, close_price)
        min_val = min(*samples, close_price)

        height = 17
        scale = height / (max_val - min_val) if max_val != min_val else 1

        inflection_pt = int(round((close_price - min_val) * scale))
        values = [(x, int((s - min_val) * scale)) for x, s in enumerate(samples)]

        return {"values": values, "inflection_pt": inflection_pt}

    def update(self) -> None:
        """Check if any scheduled updates are due."""
        global _next_data_update, _next_market_check  # noqa: PLW0603
        global _market_check_in_flight  # noqa: PLW0603

        now = time.time()
        should_check_market = False
        should_fetch_data = False
        trading_day = None
        previous_day = None

        # Check if market state update is due
        with _schedule_lock:
            if now >= _next_market_check and not _market_check_in_flight:
                # Back off rather than disabling: if the check thread dies or hangs
                # this still fires again instead of freezing the app forever.
                _next_market_check = now + _MARKET_CHECK_RETRY_INTERVAL
                _market_check_in_flight = True
                should_check_market = True

            # Check if data update is due (only when market is open)
            elif _market_is_open and now >= _next_data_update:
                # Provisional: replaced below once the budget has been consulted
                _next_data_update = now + _MARKET_OPEN_UPDATE_INTERVAL
                should_fetch_data = True

                # Parse trading days
                if _current_trading_day and _previous_trading_day:
                    eastern = zoneinfo.ZoneInfo("America/New_York")
                    trading_day = datetime.strptime(
                        _current_trading_day + " 09:30:00", "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=eastern)
                    previous_day = datetime.strptime(
                        _previous_trading_day + " 09:30:00", "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=eastern)

        # Run market state update in background (outside lock)
        if should_check_market:
            try:
                thread = threading.Thread(target=self._update_market_state, daemon=True)
                thread.start()
            except Exception as e:
                log.warning("Could not start market check thread: %s", e)
                with _schedule_lock:
                    _market_check_in_flight = False
        elif should_fetch_data and trading_day and previous_day:
            # Re-pace against the live budget: as credits deplete the refresh
            # rate stretches out, and once they run dry polling stops entirely
            # until the ledger rolls over to the next trading day.
            interval = self._compute_update_interval()
            with _schedule_lock:
                _next_data_update = time.time() + interval if interval is not None else float("inf")
            if interval is not None:
                self._fetch_data(trading_day, previous_day)

    def _get_text_width(self, draw: ImageDraw, text: str) -> int:
        """Get text width for right-alignment."""
        bbox = draw.textbbox((0, 0), text, font=self._font)
        return bbox[2] - bbox[0]

    def render(self) -> Optional[FrameBuffer]:
        """Render stock display."""
        self.fb.clear()

        img = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        with self._data_lock:
            white = (255, 255, 255)
            grey = (155, 155, 155)
            green = (0, 255, 0)
            red = (255, 0, 0)
            green_dim = (0, 25, 0)
            red_dim = (25, 0, 0)

            line1_y = -1
            line2_y = 6

            draw.text((1, line1_y), self._symbol, fill=white, font=self._font)

            if self._current_price is not None:
                price_str = "%.2f" % self._current_price
                diff_str = "%.2f" % self._diff
                pct_str = "%.2f%%" % self._percent

                draw.text((1, line2_y), price_str, fill=grey, font=self._font)

                color = green if self._diff >= 0 else red

                diff_width = self._get_text_width(draw, diff_str)
                draw.text((self.width - diff_width, line1_y), diff_str, fill=color, font=self._font)

                pct_width = self._get_text_width(draw, pct_str)
                draw.text((self.width - pct_width, line2_y), pct_str, fill=color, font=self._font)

                y_offset = 31

                # Draw area fills
                for x, y in self._graph_data:
                    if y >= self._inflection_pt:
                        for fill_y in range(self._inflection_pt, y + 1):
                            if 0 <= y_offset - fill_y < self.height:
                                img.putpixel((x, y_offset - fill_y), green_dim)
                    else:
                        for fill_y in range(y, self._inflection_pt + 1):
                            if 0 <= y_offset - fill_y < self.height:
                                img.putpixel((x, y_offset - fill_y), red_dim)

                # Draw connected lines
                num_points = len(self._graph_data)
                for idx, (x, y) in enumerate(self._graph_data):
                    curr_y = y_offset - y

                    if idx < num_points - 1:
                        next_x, next_y = self._graph_data[idx + 1]
                        next_screen_y = y_offset - next_y

                        if y >= self._inflection_pt:
                            line_color = green
                        else:
                            line_color = red

                        inflection_screen_y = y_offset - self._inflection_pt
                        if y >= self._inflection_pt and next_y < self._inflection_pt:
                            draw.line([(x, curr_y), (x, inflection_screen_y)], fill=green)
                            draw.line([(x, inflection_screen_y), (next_x, next_screen_y)], fill=red)
                        elif y < self._inflection_pt and next_y >= self._inflection_pt:
                            draw.line([(x, curr_y), (x, inflection_screen_y)], fill=red)
                            draw.line(
                                [(x, inflection_screen_y), (next_x, next_screen_y)], fill=green
                            )
                        else:
                            draw.line([(x, curr_y), (next_x, next_screen_y)], fill=line_color)
                    else:
                        line_color = green if y >= self._inflection_pt else red
                        if 0 <= curr_y < self.height:
                            img.putpixel((x, curr_y), line_color)
            else:
                draw.text((1, line2_y), "-.--", fill=grey, font=self._font)
                draw.text((50, line1_y), "-.--", fill=grey, font=self._font)
                draw.text((45, line2_y), "-.--%", fill=grey, font=self._font)
                draw.text((13, 19), "No data", fill=grey, font=self._font)

        self.fb.blit(img)
        return self.fb
