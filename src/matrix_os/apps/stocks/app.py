"""
Stocks App

Displays stock price and chart from TwelveData API.
Uses SQLite for caching data between app restarts.

API Strategy (matches original implementation):
- Uses market_state API to get exact time_to_open/time_to_close
- When market is OPEN: fetch data every 3 minutes
- When market is CLOSED: wait until market opens (no polling)
- Validates trading days before fetching
"""

import logging
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
from .db import StockCache, StockData

log = logging.getLogger(__name__)

# Module-level storage for shared state (avoids pickle issues with class-level locks)
_stocks_cache: Optional[StockCache] = None
_stocks_cache_lock = threading.Lock()

# Scheduled update times (shared across instances)
_next_data_update: float = 0
_next_market_check: float = 0
_market_is_open: bool = False
_current_trading_day: Optional[str] = None
_previous_trading_day: Optional[str] = None
_schedule_lock = threading.Lock()

# Update interval when market is open (3 minutes, matching original)
_MARKET_OPEN_UPDATE_INTERVAL = 3 * 60


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

    def __init__(self, *args, symbol: str = "NVDA", **kwargs):
        super().__init__(*args, **kwargs)

        self._symbol = symbol
        self._api_key = self.get_env("stocks_api_key", "")
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

    def _get_market_state(self) -> Optional[Dict]:
        """Get market state from TwelveData API (free endpoint)."""
        try:
            url = f"https://api.twelvedata.com/market_state?exchange={self._exchange}&apikey={self._api_key}"
            response = requests.get(url, timeout=10)
            data = response.json()

            if isinstance(data, list) and len(data) > 0:
                log.info("Market state: %s", data[0])
                return data[0]
            elif isinstance(data, dict) and data.get("status") == "error":
                log.warning("Market state API error: %s", data.get("message"))
                return None
            return None
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("Failed to get market state: %s", e)
            return None

    def _is_trading_day(self, day: datetime) -> bool:
        """Check if a given day is a trading day by querying API."""
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
                except exceptions.TwelveDataError:
                    timeout = 61 - datetime.now().second
                    log.warning("is_trading_day rate limited, waiting %d seconds", timeout)
                    tries -= 1
                    time.sleep(timeout)
            return False
        except (ImportError, AttributeError) as e:
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
        while trading_day.weekday() > 4 or not self._is_trading_day(trading_day):
            trading_day -= timedelta(days=1)

        # Get previous trading day
        previous_day = trading_day - timedelta(days=1)
        while previous_day.weekday() > 4 or not self._is_trading_day(previous_day):
            previous_day -= timedelta(days=1)

        return trading_day, previous_day

    def _update_market_state(self) -> None:
        """Update market state and schedule next updates."""
        global _next_data_update, _next_market_check, _market_is_open  # noqa: PLW0603
        global _current_trading_day, _previous_trading_day  # noqa: PLW0603

        log.info("Checking market state for %s", self._symbol)

        # Get trading days
        trading_day, previous_day = self._get_trading_days()

        with _schedule_lock:
            _current_trading_day = trading_day.strftime("%Y-%m-%d")
            _previous_trading_day = previous_day.strftime("%Y-%m-%d")

        log.info(
            "Current trading day: %s, Previous: %s", _current_trading_day, _previous_trading_day
        )

        # Fetch data for current trading day
        self._fetch_data(trading_day, previous_day)

        # Get market state to schedule next updates
        market_state = self._get_market_state()

        if market_state is None:
            # API failed, retry in 5 minutes
            with _schedule_lock:
                _next_market_check = time.time() + 300
            log.warning("Market state API failed, retrying in 5 minutes")
            return

        with _schedule_lock:
            if market_state.get("is_market_open"):
                _market_is_open = True

                # Schedule data updates every 3 minutes while market is open
                _next_data_update = time.time() + _MARKET_OPEN_UPDATE_INTERVAL

                # Schedule market check for after close
                time_to_close = market_state.get("time_to_close", "0:05:00")
                parts = time_to_close.split(":")
                minutes_to_close = int(parts[0]) * 60 + int(parts[1]) + 5
                _next_market_check = time.time() + (minutes_to_close * 60)

                log.info(
                    "Market OPEN - next data update in 3 min, market check in %d min",
                    minutes_to_close,
                )
            else:
                _market_is_open = False
                _next_data_update = float("inf")  # Don't update data when market closed

                # Schedule market check for after open
                time_to_open = market_state.get("time_to_open", "0:05:00")
                parts = time_to_open.split(":")
                minutes_to_open = int(parts[0]) * 60 + int(parts[1]) + 5
                _next_market_check = time.time() + (minutes_to_open * 60)

                log.info("Market CLOSED - next market check in %d min", minutes_to_open)

    def _fetch_data(self, trading_day: datetime, previous_day: datetime) -> None:
        """Fetch stock data from API."""
        if self._is_fetching:
            return

        self._is_fetching = True
        log.info("Fetching data for %s", self._symbol)

        def fetch():
            try:
                from twelvedata import TDClient

                td = TDClient(apikey=self._api_key)

                # Get previous day close price
                ts_daily = td.time_series(
                    symbol=self._symbol,
                    interval="1day",
                    outputsize=1,
                    start_date=previous_day,
                    end_date=previous_day + timedelta(minutes=self._open_time),
                    timezone=self._timezone,
                )

                daily_data = self._try_api(ts_daily)
                if daily_data and len(daily_data) > 0:
                    close_price = float(daily_data[0]["close"])
                else:
                    log.warning("No daily data for %s", self._symbol)
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

            except (ImportError, AttributeError, KeyError, ValueError) as e:
                log.warning("Stock fetch failed: %s", e)
            finally:
                self._is_fetching = False

        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()

    def _try_api(self, ts) -> Optional[List[Dict]]:
        """Try API call with retry logic (matches original implementation)."""
        from twelvedata import exceptions

        tries = 5
        while tries > 0:
            try:
                return ts.as_json()
            except exceptions.BadRequestError:
                log.warning("API bad request: %s", ts.as_url())
                return None
            except exceptions.TwelveDataError:
                timeout = 61 - datetime.now().second
                log.warning(
                    "API out of credits, retrying in %d seconds (%s)",
                    timeout,
                    ts.as_url(),
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

        now = time.time()
        should_check_market = False
        should_fetch_data = False
        trading_day = None
        previous_day = None

        # Check if market state update is due
        with _schedule_lock:
            if now >= _next_market_check:
                _next_market_check = float("inf")  # Prevent re-entry
                should_check_market = True

            # Check if data update is due (only when market is open)
            elif _market_is_open and now >= _next_data_update:
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
        if should_check_market and not self._is_fetching:
            thread = threading.Thread(target=self._update_market_state, daemon=True)
            thread.start()
        elif should_fetch_data and trading_day and previous_day:
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
