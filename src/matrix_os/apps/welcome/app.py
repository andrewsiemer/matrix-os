"""
Welcome App

Displays a welcome animation on startup.
"""

import socket
from typing import Optional

from PIL import Image, ImageDraw

from ...core.display import FrameBuffer
from ..base import AppManifest, BaseApp
from ..fonts import get_font


class WelcomeApp(BaseApp):
    """Welcome/boot animation."""

    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="Welcome",
            version="1.0.0",
            description="Boot welcome animation",
            framerate=60,  # High FPS for smooth animation
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Animation state
        self._phase = 0  # 0: hello, 1: i am, 2: IP
        self._brightness = 0
        self._direction = 1  # 1 = fading in, -1 = fading out
        self._hold_frames = 0
        self._completed = False

        # Get IP address
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            self._ip = s.getsockname()[0]
            s.close()
        except Exception:
            self._ip = "no network"

        self._messages = ["hello world", "i am", self._ip]
        self._font = None

    def on_start(self) -> None:
        """Initialize font."""
        font_path = self.get_font_path("5x6.bdf")
        self._font = get_font(font_path)

    def is_completed(self) -> bool:
        """Check if animation is complete."""
        return self._completed

    def update(self) -> None:
        """Update animation state."""
        if self._completed:
            return

        if self._hold_frames > 0:
            self._hold_frames -= 1
            return

        # Update brightness
        self._brightness += self._direction * 2

        if self._brightness >= 100:
            self._brightness = 100
            self._direction = -1
            self._hold_frames = 60  # Hold for 1 second at 60fps
        elif self._brightness <= 0:
            self._brightness = 0
            self._direction = 1
            self._phase += 1

            if self._phase >= len(self._messages):
                self._completed = True

    def render(self) -> Optional[FrameBuffer]:
        """Render current animation frame."""
        self.fb.clear()

        if self._completed:
            return self.fb

        # Get current message
        message = self._messages[self._phase]

        # Create image
        img = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Calculate text position
        if self._font:
            bbox = draw.textbbox((0, 0), message, font=self._font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            x = (self.width - text_width) // 2
            y = (self.height - text_height) // 2

            # Apply brightness
            intensity = int(255 * self._brightness / 100)
            color = (intensity, intensity, intensity)

            draw.text((x, y), message, fill=color, font=self._font)

        self.fb.blit(img)
        return self.fb
