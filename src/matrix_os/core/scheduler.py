"""
App scheduler for MatrixOS.

Manages which app is currently displayed and handles transitions.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from .display import FrameBuffer

from .config import SchedulerConfig

log = logging.getLogger(__name__)


@dataclass
class ScheduledApp:
    """An app scheduled for display."""

    app_id: str
    priority: int = 0  # Higher = more important
    duration: float = 15.0  # How long to display (seconds)
    is_overlay: bool = False  # Overlay on top of other apps
    is_persistent: bool = False  # Always running in background


class AppScheduler:
    """
    Manages the display schedule for apps.

    Handles:
    - Round-robin rotation of apps
    - Priority-based interruption (e.g., notifications)
    - Overlay apps (e.g., clock)
    - Smooth transitions
    """

    def __init__(self, config: SchedulerConfig):
        self.config = config

        # Scheduled apps
        self._apps: Dict[str, ScheduledApp] = {}
        self._rotation_order: List[str] = []

        # Current state
        self._current_app: Optional[str] = None
        self._current_app_start: float = 0.0
        self._overlay_app: Optional[str] = None

        # Frame buffers from apps
        self._frames: Dict[str, "FrameBuffer"] = {}
        self._frame_lock = threading.Lock()

        # Callbacks
        self._on_app_change: Optional[Callable[[str, str], None]] = None

    def add_app(
        self,
        app_id: str,
        priority: int = 0,
        duration: float = None,
        is_overlay: bool = False,
        is_persistent: bool = False,
    ) -> None:
        """Add an app to the schedule."""
        if duration is None:
            duration = self.config.default_app_duration

        self._apps[app_id] = ScheduledApp(
            app_id=app_id,
            priority=priority,
            duration=duration,
            is_overlay=is_overlay,
            is_persistent=is_persistent,
        )

        if not is_overlay:
            self._rotation_order.append(app_id)
        else:
            self._overlay_app = app_id

        # Start with first app
        if self._current_app is None and not is_overlay:
            self._current_app = app_id
            self._current_app_start = time.time()

        log.info(f"Scheduled app '{app_id}' (priority={priority}, duration={duration}s)")

    def remove_app(self, app_id: str) -> None:
        """Remove an app from the schedule."""
        if app_id in self._apps:
            del self._apps[app_id]
        if app_id in self._rotation_order:
            self._rotation_order.remove(app_id)
        if app_id == self._overlay_app:
            self._overlay_app = None

        with self._frame_lock:
            if app_id in self._frames:
                del self._frames[app_id]

    def submit_frame(self, app_id: str, framebuffer: "FrameBuffer") -> None:
        """Submit a frame from an app."""
        with self._frame_lock:
            self._frames[app_id] = framebuffer

    def get_current_app(self) -> Optional[str]:
        """Get the currently displayed app ID."""
        return self._current_app

    def get_active_apps(self) -> List[str]:
        """Get list of apps that should be running."""
        active = []

        # Current display app
        if self._current_app:
            active.append(self._current_app)

        # Overlay app
        if self._overlay_app:
            active.append(self._overlay_app)

        # Persistent apps
        for app_id, sched in self._apps.items():
            if sched.is_persistent and app_id not in active:
                active.append(app_id)

        return active

    def tick(self) -> Optional["FrameBuffer"]:
        """
        Update scheduler state and return the current frame to display.

        This is called by the render loop and must be fast and non-blocking.
        """
        current_time = time.time()

        # Check if we need to rotate to next app
        if self._current_app and len(self._rotation_order) > 1:
            sched = self._apps.get(self._current_app)
            if sched and (current_time - self._current_app_start) >= sched.duration:
                self._rotate_next()

        # Get current frame
        with self._frame_lock:
            # Start with base app frame
            if self._current_app and self._current_app in self._frames:
                frame = self._frames[self._current_app]
            else:
                return None

            # TODO: Composite overlay if present
            # For now, just return the base frame
            return frame

    def _rotate_next(self) -> None:
        """Rotate to the next app in order."""
        if not self._rotation_order:
            return

        old_app = self._current_app

        try:
            current_idx = self._rotation_order.index(self._current_app)
            next_idx = (current_idx + 1) % len(self._rotation_order)
            self._current_app = self._rotation_order[next_idx]
        except ValueError:
            self._current_app = self._rotation_order[0]

        self._current_app_start = time.time()

        if self._on_app_change and old_app != self._current_app:
            self._on_app_change(old_app, self._current_app)

        log.debug(f"Rotated from '{old_app}' to '{self._current_app}'")

    def force_app(self, app_id: str) -> bool:
        """Force display of a specific app."""
        if app_id not in self._apps:
            return False

        old_app = self._current_app
        self._current_app = app_id
        self._current_app_start = time.time()

        if self._on_app_change and old_app != app_id:
            self._on_app_change(old_app, app_id)

        return True

    def on_app_change(self, callback: Callable[[str, str], None]) -> None:
        """Set callback for app changes. Callback receives (old_app, new_app)."""
        self._on_app_change = callback
