"""
Stocks App

Displays stock price and chart from TwelveData API.
Uses SQLite for caching data between app restarts.
"""

import logging
import threading
import time
import zoneinfo
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from PIL import Image, ImageDraw

from ...core.display import FrameBuffer
from ..base import AppManifest, BaseApp
from ..fonts import get_font
from .db import StockCache, StockData

log = logging.getLogger(__name__)

# Module-level storage for shared state (avoids pickle issues with class-level locks)
_stocks_cache: Optional[StockCache] = None
_stocks_cache_lock = threading.Lock()
_stocks_rate_limited_until: float = 0
_stocks_rate_limit_lock = threading.Lock()


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

    @classmethod
    def _is_rate_limited(cls) -> bool:
        """Check if we're currently rate limited."""
        with _stocks_rate_limit_lock:
            return time.time() < _stocks_rate_limited_until

    @classmethod
    def _set_rate_limited(cls, seconds: int = 60):
        """Set rate limit for specified seconds."""
        global _stocks_rate_limited_until
        with _stocks_rate_limit_lock:
            _stocks_rate_limited_until = time.time() + seconds
            log.warning("Rate limited - pausing API calls for %d seconds", seconds)

    def __init__(self, *args, symbol: str = "NVDA", **kwargs):
        super().__init__(*args, **kwargs)

        self._symbol = symbol
        self._api_key = self.get_env("stocks_api_key", "")
        self._timezone = "America/New_York"

        # Data
        self._current_price: Optional[float] = None
        self._close_price: Optional[float] = None
        self._diff: Optional[float] = None
        self._percent: Optional[float] = None
        self._graph_data: List[tuple] = []
        self._inflection_pt: int = 0

        # State
        self._last_update = 0
        self._update_interval = 5 * 60  # 5 minutes
        self._is_fetching = False
        self._data_lock = threading.Lock()
        self._font = None
        self._has_todays_data = False

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

        # Get today's trading day
        trading_day = self._get_trading_day()
        trading_day_str = trading_day.strftime("%Y-%m-%d")

        # Try to load cached data
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
                self._last_update = cached.updated

            log.info(
                "Loaded cached data for %s: $%.2f (from %s)",
                self._symbol,
                cached.current_price,
                cached.trading_day,
            )

            # Check if this is today's data
            if cached.trading_day == trading_day_str:
                self._has_todays_data = True
                # Only refresh if data is stale (older than update interval)
                if cache.is_stale(self._symbol, self._update_interval):
                    log.info("Cache is stale, will refresh %s", self._symbol)
                    self._last_update = time.time() - self._update_interval + 5
                else:
                    log.info("Using fresh cached data for %s", self._symbol)
            else:
                # Different trading day - need fresh data
                log.info(
                    "Cache is from %s, need fresh data for %s", cached.trading_day, trading_day_str
                )
                self._last_update = time.time() - self._update_interval + 5
        else:
            # No cache - fetch soon
            self._last_update = time.time() - self._update_interval + 5

    def _get_trading_day(self) -> datetime:
        """Get the current trading day's market open time."""
        eastern = zoneinfo.ZoneInfo("America/New_York")
        now = datetime.now(eastern)

        market_open_hour = 9
        market_open_min = 30

        # If before market open, use previous day
        if (now.hour * 60 + now.minute) < (market_open_hour * 60 + market_open_min):
            now = now - timedelta(days=1)

        trading_day = now.replace(
            hour=market_open_hour,
            minute=market_open_min,
            second=0,
            microsecond=0,
        )

        # Skip weekends
        while trading_day.weekday() > 4:
            trading_day -= timedelta(days=1)

        return trading_day

    def _fetch_data(self) -> None:
        """Fetch stock data in background."""
        if self._is_fetching:
            return

        # Check rate limit - skip silently and update last_update to prevent spam
        if self._is_rate_limited():
            self._last_update = time.time()
            return

        self._is_fetching = True

        def fetch():
            try:
                from twelvedata import TDClient, exceptions

                td = TDClient(apikey=self._api_key)

                trading_day = self._get_trading_day()
                trading_day_end = trading_day + timedelta(minutes=390)

                # Get intraday data
                ts = td.time_series(
                    symbol=self._symbol,
                    interval="1min",
                    start_date=trading_day,
                    end_date=trading_day_end,
                    outputsize=390,
                    timezone=self._timezone,
                )

                try:
                    data = ts.as_json()
                except exceptions.TwelveDataError as e:
                    error_msg = str(e).lower()
                    if "run out of api credits" in error_msg:
                        # Daily limit hit - wait 1 hour before retrying
                        self._set_rate_limited(3600)
                    elif "api credits" in error_msg or "rate" in error_msg:
                        # Rate limit hit - wait until next minute
                        seconds_to_wait = 61 - datetime.now().second
                        self._set_rate_limited(seconds_to_wait)
                    raise

                if not data:
                    return

                current = float(data[0]["close"])

                # Get previous trading day for close price
                prev_day = trading_day - timedelta(days=1)
                while prev_day.weekday() > 4:
                    prev_day -= timedelta(days=1)

                ts_daily = td.time_series(
                    symbol=self._symbol,
                    interval="1day",
                    start_date=prev_day,
                    end_date=prev_day + timedelta(minutes=390),
                    outputsize=1,
                    timezone=self._timezone,
                )

                try:
                    daily_data = ts_daily.as_json()
                except exceptions.TwelveDataError as e:
                    error_msg = str(e).lower()
                    if "run out of api credits" in error_msg:
                        # Daily limit hit - wait 1 hour before retrying
                        self._set_rate_limited(3600)
                    elif "api credits" in error_msg or "rate" in error_msg:
                        # Rate limit hit - wait until next minute
                        seconds_to_wait = 61 - datetime.now().second
                        self._set_rate_limited(seconds_to_wait)
                    raise

                if daily_data and len(daily_data) > 0:
                    close = float(daily_data[0]["close"])
                else:
                    close = current

                diff = current - close
                percent = (diff / close) * 100 if close else 0

                graph_result = self._build_graph(data, close, trading_day)
                graph_values = graph_result["values"]
                inflection_pt = graph_result["inflection_pt"]

                now = time.time()
                trading_day_str = trading_day.strftime("%Y-%m-%d")

                with self._data_lock:
                    self._current_price = current
                    self._close_price = close
                    self._diff = diff
                    self._percent = percent
                    self._graph_data = graph_values
                    self._inflection_pt = inflection_pt
                    self._last_update = now
                    self._has_todays_data = True

                cache = self._get_cache()
                cache.set(
                    StockData(
                        symbol=self._symbol,
                        current_price=current,
                        close_price=close,
                        difference=diff,
                        percent=percent,
                        inflection_pt=inflection_pt,
                        graph_values=graph_values,
                        trading_day=trading_day_str,
                        updated=now,
                    )
                )

                log.info("Stock data updated: %s = $%.2f", self._symbol, current)

            except Exception as e:
                error_msg = str(e).lower()
                if "run out of api credits" in error_msg:
                    log.warning("Stock API daily limit reached - pausing for 1 hour")
                elif "api credits" in error_msg or "rate" in error_msg:
                    log.warning("Stock fetch rate limited: %s", e)
                else:
                    log.warning("Stock fetch failed: %s", e)
            finally:
                self._is_fetching = False

        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()

    def _build_graph(
        self, data: List[Dict], close_price: float, market_open: datetime = None
    ) -> Dict:
        """Build graph data from time series."""
        if not data:
            return {"values": [], "inflection_pt": 0}

        open_time = 390
        graph_width = 64
        timestamps = [
            int(round(i * (open_time - 1) / (graph_width - 1))) for i in range(graph_width)
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

        max_val = max(max(samples), close_price)
        min_val = min(min(samples), close_price)

        height = 17
        scale = height / (max_val - min_val) if max_val != min_val else 1

        inflection_pt = int(round((close_price - min_val) * scale))
        values = [(x, int((s - min_val) * scale)) for x, s in enumerate(samples)]

        return {"values": values, "inflection_pt": inflection_pt}

    def update(self) -> None:
        """Check if refresh needed."""
        if time.time() - self._last_update > self._update_interval:
            self._fetch_data()

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
