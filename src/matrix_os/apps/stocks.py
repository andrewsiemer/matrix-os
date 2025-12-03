"""
Stocks App

Displays stock price and chart from TwelveData API.
"""

import logging
import threading
import time
from typing import Dict, List, Optional

from PIL import Image, ImageDraw

from ..core.display import FrameBuffer
from .base import AppManifest, BaseApp, Capability
from .fonts import get_font

log = logging.getLogger(__name__)


class StocksApp(BaseApp):
    """Stock price and chart display."""

    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="Stocks",
            version="1.0.0",
            description="Stock price and chart display",
            framerate=1,
            capabilities={Capability.NETWORK, Capability.FILESYSTEM},
        )

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
        """Initialize and fetch data."""
        # Load small bitmap font for LED matrix (5x6 was used in original)
        font_path = self.get_font_path("5x6.bdf")
        self._font = get_font(font_path)

        # Don't fetch immediately - wait for first update cycle
        # This helps stagger API calls when multiple stock apps are running
        self._last_update = time.time() - self._update_interval + 5  # Fetch in 5 seconds

    def _fetch_data(self) -> None:
        """Fetch stock data in background."""
        # Prevent concurrent fetches
        if self._is_fetching:
            return
        self._is_fetching = True

        def fetch():
            try:
                from twelvedata import TDClient

                td = TDClient(apikey=self._api_key)

                # Get current quote
                ts = td.time_series(
                    symbol=self._symbol,
                    interval="1min",
                    outputsize=390,  # Full trading day
                    timezone=self._timezone,
                )

                data = ts.as_json()
                if not data:
                    return

                # Parse data
                current = float(data[0]["close"])

                # Get previous close (simplified)
                ts_daily = td.time_series(
                    symbol=self._symbol,
                    interval="1day",
                    outputsize=2,
                    timezone=self._timezone,
                )
                daily_data = ts_daily.as_json()

                if daily_data and len(daily_data) > 1:
                    close = float(daily_data[1]["close"])
                else:
                    close = current

                diff = current - close
                percent = (diff / close) * 100 if close else 0

                # Build graph data
                graph_data = self._build_graph(data, close)

                with self._data_lock:
                    self._current_price = current
                    self._close_price = close
                    self._diff = diff
                    self._percent = percent
                    self._graph_data = graph_data["values"]
                    self._inflection_pt = graph_data["inflection_pt"]
                    self._last_update = time.time()

                log.info(f"Stock data updated: {self._symbol} = ${current:.2f}")

            except Exception as e:
                log.warning(f"Stock fetch failed: {e}")
            finally:
                self._is_fetching = False

        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()

    def _build_graph(self, data: List[Dict], close_price: float) -> Dict:
        """Build graph data from time series."""
        # Sample 64 points across the trading day
        samples = []
        step = max(1, len(data) // 64)

        for i in range(0, len(data), step):
            if i == 0:
                samples.append(float(data[i]["open"]))
            else:
                samples.append(float(data[i]["close"]))
            if len(samples) >= 64:
                break

        # Reverse so oldest is first
        samples = samples[::-1]

        if not samples:
            return {"values": [], "inflection_pt": 0}

        # Calculate scale
        max_val = max(max(samples), close_price)
        min_val = min(min(samples), close_price)

        height = 17  # Graph height
        scale = height / (max_val - min_val) if max_val != min_val else 1

        inflection_pt = int((close_price - min_val) * scale)

        values = [(x, int((s - min_val) * scale)) for x, s in enumerate(samples)]

        return {
            "values": values,
            "inflection_pt": inflection_pt,
        }

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
            # Colors (same as original)
            white = (255, 255, 255)
            grey = (155, 155, 155)
            green = (0, 255, 0)
            red = (255, 0, 0)
            green_dim = (0, 25, 0)
            red_dim = (25, 0, 0)

            # Original used baseline y=6 for line 1, y=13 for line 2
            # With 5x6 font (6px tall), convert to top-left coordinates
            line1_y = -1
            line2_y = 6

            # Symbol (top left) - original: graphics.DrawText(..., 1, 6, white, self.symbol)
            draw.text((1, line1_y), self._symbol, fill=white, font=self._font)

            if self._current_price is not None:
                # Format numbers like original: str("%0.2f" % value)
                price_str = "%.2f" % self._current_price
                diff_str = "%.2f" % self._diff
                pct_str = "%.2f%%" % self._percent

                # Current price (second line, left)
                draw.text((1, line2_y), price_str, fill=grey, font=self._font)

                # Color based on positive/negative
                color = green if self._diff >= 0 else red

                # Right-align difference (line 1, right)
                diff_width = self._get_text_width(draw, diff_str)
                draw.text((self.width - diff_width, line1_y), diff_str, fill=color, font=self._font)

                # Right-align percent (line 2, right)
                pct_width = self._get_text_width(draw, pct_str)
                draw.text((self.width - pct_width, line2_y), pct_str, fill=color, font=self._font)

                # Draw graph (bottom half) - original used y_offset=31
                y_offset = 31
                for x, y in self._graph_data:
                    # Draw area fill
                    if y >= self._inflection_pt:
                        for fill_y in range(self._inflection_pt, y + 1):
                            if 0 <= y_offset - fill_y < self.height:
                                img.putpixel((x, y_offset - fill_y), green_dim)
                    else:
                        for fill_y in range(y, self._inflection_pt + 1):
                            if 0 <= y_offset - fill_y < self.height:
                                img.putpixel((x, y_offset - fill_y), red_dim)

                    # Draw line
                    line_color = green if y >= self._inflection_pt else red
                    if 0 <= y_offset - y < self.height:
                        img.putpixel((x, y_offset - y), line_color)
            else:
                # No data - match original layout
                draw.text((1, line2_y), "-.--", fill=grey, font=self._font)
                draw.text((50, line1_y), "-.--", fill=grey, font=self._font)
                draw.text((45, line2_y), "-.--%", fill=grey, font=self._font)
                draw.text((13, 19), "No data", fill=grey, font=self._font)

        self.fb.blit(img)
        return self.fb
