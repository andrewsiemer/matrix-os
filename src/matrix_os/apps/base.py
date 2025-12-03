"""
Base app class for MatrixOS applications.

All apps must inherit from BaseApp and implement the required methods.
"""

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

from PIL import Image

if TYPE_CHECKING:
    from ..core.display import FrameBuffer
    from ..core.ipc import AppChannel
    from ..core.kernel import Kernel

log = logging.getLogger(__name__)


@dataclass
class AppManifest:
    """
    Manifest declaring an app's requirements and metadata.
    """

    name: str
    version: str = "1.0.0"
    author: str = "unknown"
    description: str = ""
    framerate: int = 30


class BaseApp(ABC):
    """
    Base class for all MatrixOS applications.

    Apps render to a framebuffer and communicate via IPC.
    They never directly access the display hardware.

    Lifecycle:
        1. __init__() - Called when app is registered
        2. on_start() - Called when app begins execution
        3. update() / render() - Called each frame
        4. on_stop() - Called when app is stopped

    Example:
        class MyApp(BaseApp):
            @classmethod
            def get_manifest(cls) -> AppManifest:
                return AppManifest(
                    name="My App",
                    framerate=30,
                )

            def update(self):
                self.counter += 1

            def render(self) -> FrameBuffer:
                self.fb.clear()
                self.fb.set_pixel(self.counter % 64, 16, 255, 255, 255)
                return self.fb
    """

    def __init__(
        self,
        app_id: str,
        framebuffer: "FrameBuffer",
        channel: "AppChannel",
        kernel: "Kernel",
        **kwargs,
    ):
        self.app_id = app_id
        self.fb = framebuffer
        self.channel = channel

        # Store paths and env upfront so we don't need kernel reference at runtime
        # This makes the app picklable for multiprocessing
        self._fonts_path = kernel.fonts_path
        self._images_path = kernel.images_path
        self._env_settings: Dict[str, Any] = {}
        if kernel.config.env:
            # Copy all env settings
            for key in dir(kernel.config.env):
                if not key.startswith("_"):
                    try:
                        self._env_settings[key] = getattr(kernel.config.env, key)
                    except Exception:
                        pass

        # Get manifest from class method
        self._manifest = self.get_manifest()

        # Store any extra kwargs as instance attributes
        for key, value in kwargs.items():
            setattr(self, key, value)

    @property
    def manifest(self) -> AppManifest:
        return self._manifest

    @property
    def width(self) -> int:
        """Display width in pixels."""
        return self.fb.width

    @property
    def height(self) -> int:
        """Display height in pixels."""
        return self.fb.height

    @classmethod
    @abstractmethod
    def get_manifest(cls) -> AppManifest:
        """
        Return the app's manifest.

        Must be implemented by subclasses.
        """
        pass

    def on_start(self) -> None:
        """
        Called when the app starts.

        Override to perform initialization that requires the app to be running.
        """

    def on_stop(self) -> None:
        """
        Called when the app stops.

        Override to perform cleanup.
        """

    @abstractmethod
    def update(self) -> None:
        """
        Update app state.

        Called once per frame before render().
        Must be implemented by subclasses.
        """

    @abstractmethod
    def render(self) -> Optional["FrameBuffer"]:
        """
        Render the current frame.

        Must return the framebuffer to be displayed.
        Must be implemented by subclasses.
        """

    # Utility methods for apps

    def get_font_path(self, font_name: str) -> str:
        """Get the path to a font file."""
        return os.path.join(self._fonts_path, font_name)

    def get_image_path(self, image_name: str) -> str:
        """Get the path to an image file."""
        return os.path.join(self._images_path, image_name)

    def load_image(self, path: str, size: tuple = None) -> Image.Image:
        """Load and optionally resize an image."""
        image = Image.open(path)
        if size:
            image.thumbnail(size, Image.Resampling.LANCZOS)
        return image.convert("RGB")

    def get_env(self, key: str, default: Any = None) -> Any:
        """Get an environment setting."""
        return self._env_settings.get(key, default)

    def __getstate__(self):
        """Custom pickle support - exclude unpicklable objects."""
        state = self.__dict__.copy()
        if "channel" in state:
            del state["channel"]
        return state

    def __setstate__(self, state):
        """Custom unpickle support."""
        self.__dict__.update(state)
        self.channel = None
