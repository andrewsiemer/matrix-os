"""
Slack Status App

Displays current Slack status with icon.
"""

import io
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from PIL import Image, ImageDraw

from .base import BaseApp, AppManifest, Capability
from .fonts import get_font
from ..core.display import FrameBuffer

log = logging.getLogger(__name__)


class SlackStatusApp(BaseApp):
    """Slack status display."""

    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="Slack Status",
            version="1.0.0",
            description="Display Slack status",
            framerate=10,  # Higher for scrolling text
            capabilities={Capability.NETWORK},
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._user_id = self.get_env("slack_user_id", "")
        self._token = self.get_env("slack_token", "")

        # Status data
        self._active = False
        self._status = "Available"
        self._expiration = 0
        self._icon: Optional[Image.Image] = None

        # Scrolling state
        self._scroll_pos = 0
        self._text_width = 0

        # Update settings
        self._last_update = 0
        self._update_interval = 10  # seconds
        self._data_lock = threading.Lock()

        # Excluded status suffixes
        self._exclude = [" • Outlook Calendar"]

        self._font = None

    def on_start(self) -> None:
        """Initialize."""
        font_path = self.get_font_path("5x6.bdf")
        self._font = get_font(font_path)

        self._fetch_status()

    def _fetch_status(self) -> None:
        """Fetch Slack status in background."""
        def fetch():
            try:
                import requests

                url = f"https://slack.com/api/users.profile.get?user={self._user_id}&pretty=1"
                headers = {"Authorization": f"Bearer {self._token}"}

                response = requests.get(url, headers=headers, timeout=7)
                data = response.json()

                if "profile" not in data:
                    return

                profile = data["profile"]

                with self._data_lock:
                    if profile.get("status_text"):
                        self._status = profile["status_text"]
                        self._active = True

                        # Remove excluded suffixes
                        for suffix in self._exclude:
                            self._status = self._status.replace(suffix, "")

                        self._expiration = profile.get("status_expiration", 0)

                        # Get status emoji icon
                        if profile.get("status_emoji_display_info"):
                            icon_url = profile["status_emoji_display_info"][0]["display_url"]
                            icon_response = requests.get(icon_url, timeout=7)
                            icon_img = Image.open(io.BytesIO(icon_response.content))
                            icon_img.thumbnail((12, 12))
                            self._icon = icon_img.convert("RGB")
                    else:
                        self._active = False
                        self._status = "Available"
                        self._expiration = 0

                        # Default checkmark icon
                        default_url = "https://a.slack-edge.com/production-standard-emoji-assets/14.0/apple-large/2714-fe0f.png"
                        icon_response = requests.get(default_url, timeout=7)
                        icon_img = Image.open(io.BytesIO(icon_response.content))
                        icon_img.thumbnail((12, 12))
                        self._icon = icon_img.convert("RGB")

                    self._last_update = time.time()

            except Exception as e:
                log.warning(f"Slack status fetch failed: {e}")

        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()

    def is_active(self) -> bool:
        """Check if there's an active status."""
        with self._data_lock:
            return self._active

    def update(self) -> None:
        """Update scroll position and check for refresh."""
        if time.time() - self._last_update > self._update_interval:
            self._fetch_status()

        # Update scroll if text is too wide
        if self._text_width > self.width:
            self._scroll_pos -= 1
            if self._scroll_pos < -self._text_width:
                self._scroll_pos = self.width

    def render(self) -> Optional[FrameBuffer]:
        """Render Slack status."""
        self.fb.clear()

        img = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        white = (255, 255, 255)
        grey = (155, 155, 155)

        with self._data_lock:
            y_offset = 0

            # Expiration time
            now_ts = datetime.now(timezone.utc).timestamp()
            remaining = int((self._expiration - now_ts) / 60)

            if remaining > 0:
                exp_str = f"for {remaining} mins"
                if self._font:
                    bbox = draw.textbbox((0, 0), exp_str, font=self._font)
                    text_width = bbox[2] - bbox[0]
                    x = (self.width - text_width) // 2
                    draw.text((x, 24), exp_str, fill=grey, font=self._font)
            else:
                y_offset = 4

            # Status icon
            if self._icon:
                icon_x = (self.width - 12) // 2
                img.paste(self._icon, (icon_x, 2 + y_offset))

            # Status text
            if self._font:
                bbox = draw.textbbox((0, 0), self._status, font=self._font)
                self._text_width = bbox[2] - bbox[0]

                if self._text_width > self.width:
                    # Scrolling text
                    draw.text(
                        (self._scroll_pos, 16 + y_offset),
                        self._status,
                        fill=white,
                        font=self._font,
                    )
                else:
                    # Centered text
                    x = (self.width - self._text_width) // 2
                    draw.text((x, 16 + y_offset), self._status, fill=white, font=self._font)

        self.fb.blit(img)
        return self.fb
