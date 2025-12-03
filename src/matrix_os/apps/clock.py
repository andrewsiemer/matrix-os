"""
Clock Apps

Various clock display implementations.
"""

import random
import zoneinfo
from datetime import datetime
from typing import Optional

from PIL import Image, ImageDraw

from .base import BaseApp, AppManifest, Capability
from .fonts import get_font
from ..core.display import FrameBuffer


class BasicClockApp(BaseApp):
    """Simple digital clock display."""

    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="Basic Clock",
            version="1.0.0",
            description="Simple digital clock",
            framerate=1,
            capabilities={Capability.SYSTEM_INFO},
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timezone = self.get_env("local_tz", "America/Los_Angeles")
        self._font = None
        self._blink = True

    def on_start(self) -> None:
        """Load font on startup."""
        font_path = self.get_font_path("5x6.bdf")
        self._font = get_font(font_path)

    def update(self) -> None:
        """Toggle blink state."""
        self._blink = not self._blink

    def render(self) -> Optional[FrameBuffer]:
        """Render the clock."""
        self.fb.clear()

        # Get current time
        now = datetime.now(zoneinfo.ZoneInfo(self.timezone))
        hour = now.strftime("%-I")
        minute = now.strftime("%M")
        ampm = now.strftime("%p")

        # Create image for text rendering
        img = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Format time string
        if self._blink:
            time_str = f"{hour}:{minute} {ampm}"
        else:
            time_str = f"{hour} {minute} {ampm}"

        # Center text
        bbox = draw.textbbox((0, 0), time_str, font=self._font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (self.width - text_width) // 2
        y = (self.height - text_height) // 2

        draw.text((x, y), time_str, fill=(255, 255, 255), font=self._font)

        self.fb.blit(img)
        return self.fb


class BinaryClockApp(BaseApp):
    """Binary clock display with colorful squares."""

    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="Binary Clock",
            version="1.0.0",
            description="Binary representation of time",
            framerate=1,
            capabilities={Capability.SYSTEM_INFO},
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timezone = self.get_env("local_tz", "America/Los_Angeles")

    def update(self) -> None:
        """Nothing to update - state is derived from time."""

    def _draw_square(self, x: int, y: int, size: int, color: tuple) -> None:
        """Draw a filled square."""
        for dx in range(size):
            for dy in range(size):
                self.fb.set_pixel(x + dx, y + dy, *color)

    def render(self) -> Optional[FrameBuffer]:
        """Render the binary clock."""
        self.fb.clear()

        now = datetime.now(zoneinfo.ZoneInfo(self.timezone))

        # Get seconds since midnight (or noon if PM)
        if now.hour < 12:
            base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            base = now.replace(hour=12, minute=0, second=0, microsecond=0)

        delta = now - base
        seconds = int(delta.total_seconds())

        # Convert to 16-bit binary
        binary = format(seconds, '016b')

        # Layout settings
        start_x = 17
        start_y = 1
        interval = 8
        size = 6

        # Draw each bit
        for bit_idx, bit in enumerate(binary):
            if bit == '1':
                x = start_x + (bit_idx % 4) * interval
                y = start_y + (bit_idx // 4) * interval
                color = (
                    random.randint(50, 255),
                    random.randint(50, 255),
                    random.randint(50, 255),
                )
                self._draw_square(x, y, size, color)

        return self.fb
