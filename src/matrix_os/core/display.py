"""
Display abstraction layer for MatrixOS.

Provides a hardware-agnostic interface for rendering to the LED matrix.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

import numpy as np
from PIL import Image

if TYPE_CHECKING:
    from rgbmatrix import RGBMatrix

from .config import DisplayConfig

log = logging.getLogger(__name__)


@dataclass
class FrameBuffer:
    """
    Hardware-independent framebuffer that apps render to.

    Apps never directly access the matrix - they render to this buffer,
    which is then composited by the kernel.
    """

    width: int
    height: int
    _data: Optional[np.ndarray] = None

    def __post_init__(self):
        if self._data is None:
            self._data = np.zeros((self.height, self.width, 3), dtype=np.uint8)

    @property
    def data(self) -> np.ndarray:
        """Get the raw pixel data as numpy array (height, width, 3)."""
        return self._data

    def clear(self, color: Tuple[int, int, int] = (0, 0, 0)) -> None:
        """Clear the framebuffer to a solid color."""
        self._data[:, :] = color

    def set_pixel(self, x: int, y: int, r: int, g: int, b: int) -> None:
        """Set a single pixel."""
        if 0 <= x < self.width and 0 <= y < self.height:
            self._data[y, x] = (r, g, b)

    def get_pixel(self, x: int, y: int) -> Tuple[int, int, int]:
        """Get a single pixel."""
        if 0 <= x < self.width and 0 <= y < self.height:
            return tuple(self._data[y, x])
        return (0, 0, 0)

    def draw_line(self, x0: int, y0: int, x1: int, y1: int, r: int, g: int, b: int) -> None:
        """Draw a line using Bresenham's algorithm."""
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy

        while True:
            self.set_pixel(x0, y0, r, g, b)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def blit(self, image: Image.Image, x: int = 0, y: int = 0) -> None:
        """Blit a PIL Image onto the framebuffer."""
        if image.mode != "RGB":
            image = image.convert("RGB")

        img_array = np.array(image)
        h, w = img_array.shape[:2]

        # Calculate clipping bounds
        src_x, src_y = 0, 0
        dst_x, dst_y = x, y

        if dst_x < 0:
            src_x = -dst_x
            w += dst_x
            dst_x = 0
        if dst_y < 0:
            src_y = -dst_y
            h += dst_y
            dst_y = 0

        w = min(w, self.width - dst_x)
        h = min(h, self.height - dst_y)

        if w > 0 and h > 0:
            self._data[dst_y : dst_y + h, dst_x : dst_x + w] = img_array[
                src_y : src_y + h, src_x : src_x + w
            ]

    def to_image(self) -> Image.Image:
        """Convert framebuffer to PIL Image."""
        return Image.fromarray(self._data, mode="RGB")

    def copy(self) -> "FrameBuffer":
        """Create a copy of this framebuffer."""
        fb = FrameBuffer(self.width, self.height)
        fb._data = self._data.copy()
        return fb


class Display:
    """
    Hardware display abstraction.

    Manages the physical LED matrix and provides vsync-aware rendering.
    """

    def __init__(self, config: DisplayConfig):
        self.config = config
        self._matrix: Optional["RGBMatrix"] = None
        self._canvas = None
        self._initialized = False

    @property
    def width(self) -> int:
        return self.config.cols * self.config.chain_length

    @property
    def height(self) -> int:
        return self.config.rows * self.config.parallel

    def initialize(self) -> bool:
        """Initialize the hardware display."""
        try:
            from rgbmatrix import RGBMatrix, RGBMatrixOptions

            options = RGBMatrixOptions()
            options.hardware_mapping = self.config.hardware_mapping
            options.rows = self.config.rows
            options.cols = self.config.cols
            options.chain_length = self.config.chain_length
            options.parallel = self.config.parallel
            options.row_address_type = self.config.row_address_type
            options.multiplexing = self.config.multiplexing
            options.pwm_bits = self.config.pwm_bits
            options.brightness = self.config.brightness
            options.pwm_lsb_nanoseconds = self.config.pwm_lsb_nanoseconds
            options.led_rgb_sequence = self.config.led_rgb_sequence
            options.pixel_mapper_config = self.config.pixel_mapper_config
            options.panel_type = self.config.panel_type
            options.show_refresh_rate = self.config.show_refresh_rate
            options.gpio_slowdown = self.config.gpio_slowdown
            options.disable_hardware_pulsing = self.config.disable_hardware_pulsing
            options.drop_privileges = self.config.drop_privileges

            self._matrix = RGBMatrix(options=options)
            self._canvas = self._matrix.CreateFrameCanvas()
            self._initialized = True
            log.info(f"Display initialized: {self.width}x{self.height}")
            return True

        except ImportError:
            log.warning("rgbmatrix not available - running in simulation mode")
            self._initialized = True
            return True
        except Exception as e:
            log.error(f"Failed to initialize display: {e}")
            return False

    def render(self, framebuffer: FrameBuffer) -> None:
        """Render a framebuffer to the display with vsync."""
        if not self._initialized:
            return

        if self._matrix is None:
            # Simulation mode - just log
            return

        # Convert framebuffer to PIL Image and set on canvas
        image = framebuffer.to_image()
        self._canvas.SetImage(image)
        self._canvas = self._matrix.SwapOnVSync(self._canvas)

    def set_brightness(self, brightness: int) -> None:
        """Set display brightness (0-100)."""
        brightness = max(0, min(100, brightness))
        if self._matrix:
            self._matrix.brightness = brightness
        self.config.brightness = brightness

    def clear(self) -> None:
        """Clear the display."""
        if self._matrix:
            self._matrix.Clear()

    def create_framebuffer(self) -> FrameBuffer:
        """Create a new framebuffer sized for this display."""
        return FrameBuffer(self.width, self.height)

    def shutdown(self) -> None:
        """Shutdown the display."""
        self.clear()
        self._initialized = False
        log.info("Display shutdown complete")
