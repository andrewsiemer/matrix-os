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


class StocksApp(BaseApp):
    """Stock price and chart display."""

    # Shared cache across all StocksApp instances
    _cache: Optional[StockCache] = None
    _cache_lock = threading.Lock()

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
        with cls._cache_lock:
            if cls._cache is None:
                cls._cache = StockCache()
            return cls._cache

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
        self._update_interval = 5 * 60  # 5 minutes to avoid API rate limits
        self._is_fetching = False
        self._data_lock = threading.Lock()
        self._font = None

    def on_start(self) -> None:
        """Initialize and load cached data."""
        # Load font
        font_path = self.get_font_path("5x6.bdf")
        self._font = get_font(font_path)

        # Try to load cached data first
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

            log.info("Loaded cached data for %s: $%.2f", self._symbol, cached.current_price)

            # If cache is stale, schedule a refresh
            if cache.is_stale(self._symbol, self._update_interval):
                self._last_update = time.time() - self._update_interval + 5
        else:
            # No cache - fetch soon
            self._last_update = time.time() - self._update_interval + 5

    def _get_trading_day(self) -> datetime:
        """Get the current trading day's market open time."""
        eastern = zoneinfo.ZoneInfo("America/New_York")
        now = datetime.now(eastern)

        # Market opens at 9:30 AM ET
        market_open_hour = 9
        market_open_min = 30

        # If before market open, use previous day
        if (now.hour * 60 + now.minute) < (market_open_hour * 60 + market_open_min):
            now = now - timedelta(days=1)

        # Set to market open time
        trading_day = now.replace(
            hour=market_open_hour,
            minute=market_open_min,
            second=0,
            microsecond=0,
        )

        # Skip weekends (Saturday=5, Sunday=6)
        while trading_day.weekday() > 4:
            trading_day -= timedelta(days=1)

        return trading_day

    def _fetch_data(self) -> None:
        """Fetch stock data in background."""
        if self._is_fetching:
            return
        self._is_fetching = True

        def fetch():
            try:
                from twelvedata import TDClient

                td = TDClient(apikey=self._api_key)

                # Get trading day (market open time)
                trading_day = self._get_trading_day()
                trading_day_end = trading_day + timedelta(minutes=390)

                # Get intraday data for today only
                ts = td.time_series(
                    symbol=self._symbol,
                    interval="1min",
                    start_date=trading_day,
                    end_date=trading_day_end,
                    outputsize=390,
                    timezone=self._timezone,
                )

                data = ts.as_json()
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
                daily_data = ts_daily.as_json()

                if daily_data and len(daily_data) > 0:
                    close = float(daily_data[0]["close"])
                else:
                    close = current

                diff = current - close
                percent = (diff / close) * 100 if close else 0

                # Build graph data
                graph_result = self._build_graph(data, close, trading_day)
                graph_values = graph_result["values"]
                inflection_pt = graph_result["inflection_pt"]

                now = time.time()
                trading_day_str = trading_day.strftime("%Y-%m-%d")

                # Update local state
                with self._data_lock:
                    self._current_price = current
                    self._close_price = close
                    self._diff = diff
                    self._percent = percent
                    self._graph_data = graph_values
                    self._inflection_pt = inflection_pt
                    self._last_update = now

                # Save to cache
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
                log.warning("Stock fetch failed: %s", e)
            finally:
                self._is_fetching = False

        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()

    def _build_graph(
        self, data: List[Dict], close_price: float, market_open: datetime = None
    ) -> Dict:
        """Build graph data from time series.

        Uses 64 fixed timestamp positions across the trading day (like original).
        Only draws points up to the current time during trading hours.
        """
        if not data:
            return {"values": [], "inflection_pt": 0}

        # Trading day is 390 minutes (6.5 hours: 9:30 AM - 4:00 PM)
        # Create 64 evenly spaced timestamp offsets (0, 6, 12, ... 389)
        open_time = 390
        graph_width = 64
        timestamps = [
            int(round(i * (open_time - 1) / (graph_width - 1))) for i in range(graph_width)
        ]

        # Use provided market_open or derive from data
        if market_open is None:
            try:
                oldest_dt_str = data[-1]["datetime"]
                oldest_dt = datetime.strptime(oldest_dt_str, "%Y-%m-%d %H:%M:%S")
                market_open = oldest_dt.replace(hour=9, minute=30, second=0, microsecond=0)
            except (ValueError, KeyError, IndexError):
                return {"values": [], "inflection_pt": 0}

        # Build a lookup dict for quick access: datetime_str -> data point
        data_lookup = {point["datetime"]: point for point in data}

        # Sample data at each of the 64 timestamp positions
        samples = []
        prev_time = market_open - timedelta(minutes=1)

        for idx, delta in enumerate(timestamps):
            target_time = market_open + timedelta(minutes=delta)
            sample = None
            tries = 5

            # Try to find data at or near this timestamp
            while sample is None and tries > 0 and target_time > prev_time:
                target_str = target_time.strftime("%Y-%m-%d %H:%M:%S")
                if target_str in data_lookup:
                    sample = data_lookup[target_str]
                else:
                    target_time -= timedelta(minutes=1)
                tries -= 1

            if sample:
                prev_time = target_time
                if idx == 0:  # First data point uses open price
                    samples.append(float(sample["open"]))
                else:
                    samples.append(float(sample["close"]))
            else:
                # No data for this timestamp - we've reached the current time
                break

        if not samples:
            return {"values": [], "inflection_pt": 0}

        # Calculate scale for y-axis
        max_val = max(max(samples), close_price)
        min_val = min(min(samples), close_price)

        height = 17
        scale = height / (max_val - min_val) if max_val != min_val else 1

        inflection_pt = int(round((close_price - min_val) * scale))

        # Create values with x positions
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
