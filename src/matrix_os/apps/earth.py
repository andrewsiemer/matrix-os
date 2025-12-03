"""
Earth Day/Night App

Shows Earth with realistic day/night terminator based on current time.
"""

import os
import time
from math import asin, atan2, cos, pi, sin, sqrt
from typing import Optional

import numpy as np
from PIL import Image

from ..core.display import FrameBuffer
from .base import AppManifest, BaseApp, Capability

# Constants
TPI = 2 * pi
DEGS = 180 / pi
RADS = pi / 180
BLUR = 10.0  # Blur angle for terminator


class EarthApp(BaseApp):
    """Earth visualization with day/night terminator."""

    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="Earth",
            version="1.0.0",
            description="Earth with day/night visualization",
            framerate=1,
            capabilities={Capability.FILESYSTEM, Capability.SYSTEM_INFO},
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._day_image: Optional[Image.Image] = None
        self._night_image: Optional[Image.Image] = None
        self._L = 0  # Global for astronomical calculations

    def on_start(self) -> None:
        """Load day/night images."""
        day_path = self.get_image_path("day.png")
        night_path = self.get_image_path("night.png")

        if os.path.exists(day_path):
            self._day_image = Image.open(day_path).convert("RGB")
            self._day_image = self._day_image.resize((self.width, self.height))

        if os.path.exists(night_path):
            self._night_image = Image.open(night_path).convert("RGB")
            self._night_image = self._night_image.resize((self.width, self.height))

    def update(self) -> None:
        """Nothing to update - state derived from time."""

    def _fn_day(self, y: int, m: int, d: int, h: float) -> float:
        """Calculate days since J2000."""
        days = 367 * y - 7 * (y + (m + 9) // 12) // 4 + 275 * m // 9 + d - 730530 + h / 24.0
        return float(days)

    def _rev(self, x: float) -> float:
        """Normalize angle to 0-360."""
        rv = x - int(x / 360) * 360
        if rv < 0:
            rv += 360
        return rv

    def _calc_ra_dec(self, y: int, m: int, d: int, h: float) -> tuple:
        """Calculate right ascension and declination of the sun."""
        day = self._fn_day(y, m, d, h)

        w = 282.9404 + 4.70935e-5 * day
        e = 0.016709 - 1.151e-9 * day
        M = 356.0470 + 0.9856002585 * day
        M = self._rev(M)

        oblecl = 23.4393 - 3.563e-7 * day
        self._L = self._rev(w + M)

        E = M + DEGS * e * sin(M * RADS) * (1 + e * cos(M * RADS))

        x = cos(E * RADS) - e
        y_coord = sin(E * RADS) * sqrt(1 - e * e)
        r = sqrt(x * x + y_coord * y_coord)
        v = atan2(y_coord, x) * DEGS
        lon = self._rev(v + w)

        xequat = r * cos(lon * RADS)
        yequat = r * sin(lon * RADS) * cos(oblecl * RADS)
        zequat = r * sin(lon * RADS) * sin(oblecl * RADS)

        RA = atan2(yequat, xequat) * DEGS / 15
        Decl = asin(zequat / r) * DEGS

        return RA, Decl

    def _calc_alt(self, RA: float, Decl: float, lat: float, lon: float, h: float) -> float:
        """Calculate altitude of the sun at a given location."""
        GMST0 = (self._L * RADS + 180 * RADS) / 15 * DEGS
        SIDTIME = GMST0 + h + lon / 15
        HA = self._rev(SIDTIME - RA) * 15

        x = cos(HA * RADS) * cos(Decl * RADS)
        y = sin(HA * RADS) * cos(Decl * RADS)
        z = sin(Decl * RADS)

        xhor = x * sin(lat * RADS) - z * cos(lat * RADS)
        zhor = x * cos(lat * RADS) + z * sin(lat * RADS)

        altitude = atan2(zhor, sqrt(xhor * xhor + y * y)) * DEGS
        return altitude

    def _xy_to_latlon(self, x: int, y: int) -> tuple:
        """Convert pixel coordinates to lat/lon."""
        lat = 90.0 - float(y) / self.height * 180.0
        lon = float(x) / self.width * 360.0 - 180.0
        return lat, lon

    def _mix_pixel(self, day_pixel: tuple, night_pixel: tuple, factor: float) -> tuple:
        """Mix day and night pixels based on factor (0 = night, 1 = day)."""
        return tuple(int((1 - factor) * n + factor * d) for d, n in zip(day_pixel, night_pixel))

    def render(self) -> Optional[FrameBuffer]:
        """Render the earth with day/night terminator."""
        if self._day_image is None or self._night_image is None:
            self.fb.clear()
            return self.fb

        # Get current time
        t = time.gmtime(time.time())
        y, m, d = t[0], t[1], t[2]
        h = t[3] + t[4] / 60.0

        # Calculate sun position
        ra, dec = self._calc_ra_dec(y, m, d, h)

        # Get pixel arrays
        day_data = np.array(self._day_image)
        night_data = np.array(self._night_image)
        output = np.zeros((self.height, self.width, 3), dtype=np.uint8)

        for py in range(self.height):
            for px in range(self.width):
                lat, lon = self._xy_to_latlon(px, py)
                alt = self._calc_alt(ra, dec, lat, lon, h)

                day_pixel = tuple(day_data[py, px])
                night_pixel = tuple(night_data[py, px])

                if alt > BLUR:
                    # Full day
                    output[py, px] = day_pixel
                elif alt < -BLUR:
                    # Full night
                    output[py, px] = night_pixel
                else:
                    # Terminator zone - blend
                    factor = (alt + BLUR) / (2 * BLUR)
                    output[py, px] = self._mix_pixel(day_pixel, night_pixel, factor)

        # Blit to framebuffer
        img = Image.fromarray(output)
        self.fb.blit(img)
        return self.fb
