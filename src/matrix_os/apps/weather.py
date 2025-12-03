"""
Weather App

Displays current weather from OpenWeatherMap API.
"""

import io
import logging
import threading
from typing import Optional

from PIL import Image, ImageDraw

from .base import BaseApp, AppManifest, Capability
from .fonts import get_font
from ..core.display import FrameBuffer

log = logging.getLogger(__name__)


class WeatherApp(BaseApp):
    """Current weather display."""

    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="Weather",
            version="1.0.0",
            description="Current weather display",
            framerate=1,
            capabilities={Capability.NETWORK, Capability.SYSTEM_INFO},
        )

    def __init__(self, *args, lat: float = None, lon: float = None, **kwargs):
        super().__init__(*args, **kwargs)

        self._lat = lat or self.get_env("lat", 0.0)
        self._lon = lon or self.get_env("lon", 0.0)
        self._api_key = self.get_env("weather_api_key", "")

        self._temp = "--°F"
        self._icon: Optional[Image.Image] = None
        self._last_update = 0
        self._update_interval = 5 * 60  # 5 minutes

        self._font = None
        self._update_lock = threading.Lock()

    def on_start(self) -> None:
        """Initialize and fetch initial weather."""
        font_path = self.get_font_path("5x6.bdf")
        self._font = get_font(font_path)

        # Start background update
        self._fetch_weather()

    def _fetch_weather(self) -> None:
        """Fetch weather data from API in background thread."""
        def fetch():
            try:
                import requests
                import time

                url = (
                    f"https://api.openweathermap.org/data/3.0/onecall"
                    f"?lat={self._lat}&lon={self._lon}"
                    f"&exclude=hourly,daily&units=imperial"
                    f"&appid={self._api_key}"
                )

                response = requests.get(url, timeout=7)
                data = response.json()

                if "current" in data:
                    current = data["current"]

                    with self._update_lock:
                        self._temp = f"{round(current['temp'])}°F"

                        # Fetch weather icon
                        icon_code = current["weather"][0]["icon"]
                        icon_url = f"http://openweathermap.org/img/wn/{icon_code}@2x.png"
                        icon_response = requests.get(icon_url, timeout=7)

                        icon_img = Image.open(io.BytesIO(icon_response.content))
                        icon_img.thumbnail((18, 18))
                        self._icon = icon_img.convert("RGB")

                        self._last_update = time.time()

                    log.info(f"Weather updated: {self._temp}")

            except Exception as e:
                log.warning(f"Weather fetch failed: {e}")

        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()

    def update(self) -> None:
        """Check if we need to refresh weather data."""
        import time

        if time.time() - self._last_update > self._update_interval:
            self._fetch_weather()

    def render(self) -> Optional[FrameBuffer]:
        """Render the weather display."""
        self.fb.clear()

        # Create image for rendering
        img = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        with self._update_lock:
            # Draw icon
            if self._icon:
                icon_x = (self.width - 18) // 2
                img.paste(self._icon, (icon_x, 2))

            # Draw temperature
            if self._font:
                bbox = draw.textbbox((0, 0), self._temp, font=self._font)
                text_width = bbox[2] - bbox[0]
                x = (self.width - text_width) // 2
                draw.text((x, 22), self._temp, fill=(255, 255, 255), font=self._font)

        self.fb.blit(img)
        return self.fb
