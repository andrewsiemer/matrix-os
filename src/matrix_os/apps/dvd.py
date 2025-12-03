"""
DVD Bouncing Logo App

A nostalgic bouncing DVD logo animation.
"""

import random
from typing import Optional

from ..core.display import FrameBuffer
from .base import AppManifest, BaseApp, Capability


class DVDApp(BaseApp):
    """Bouncing DVD logo animation."""

    BITMAP = [
        [1, 1, 1, 1, 1, 1, 0, 0, 0, 1, 1, 1, 1, 1, 0],
        [0, 0, 1, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 1],
        [1, 0, 0, 1, 0, 1, 1, 0, 1, 1, 0, 1, 0, 0, 1],
        [1, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 1],
        [1, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 1, 1, 1, 0],
        [0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    ]

    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="DVD",
            version="1.0.0",
            description="Bouncing DVD logo animation",
            framerate=10,
            capabilities=set(),
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Logo dimensions
        self.logo_width = len(self.BITMAP[0])
        self.logo_height = len(self.BITMAP)

        # Position
        self.x = 0
        self.y = 0

        # Direction (True = positive, False = negative)
        self.dx = True
        self.dy = True

        # Bounds
        self.x_bound = self.width - self.logo_width
        self.y_bound = self.height - self.logo_height

        # Color
        self._randomize_color()

    def _randomize_color(self):
        """Generate a random bright color."""
        self.r = random.randint(50, 255)
        self.g = random.randint(50, 255)
        self.b = random.randint(50, 255)

    def update(self) -> None:
        """Update logo position and handle bouncing."""
        # Move X
        if self.dx:
            if self.x < self.x_bound:
                self.x += 1
            else:
                self.x -= 1
                self.dx = False
                self._randomize_color()
        else:
            if self.x > 0:
                self.x -= 1
            else:
                self.x += 1
                self.dx = True
                self._randomize_color()

        # Move Y
        if self.dy:
            if self.y < self.y_bound:
                self.y += 1
            else:
                self.y -= 1
                self.dy = False
                self._randomize_color()
        else:
            if self.y > 0:
                self.y -= 1
            else:
                self.y += 1
                self.dy = True
                self._randomize_color()

    def render(self) -> Optional[FrameBuffer]:
        """Render the DVD logo."""
        self.fb.clear()

        for row_idx, row in enumerate(self.BITMAP):
            for col_idx, pixel in enumerate(row):
                if pixel:
                    self.fb.set_pixel(self.x + col_idx, self.y + row_idx, self.r, self.g, self.b)

        return self.fb
