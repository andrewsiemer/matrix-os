"""
App scheduler for MatrixOS.

Manages which app is currently displayed and handles transitions.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set

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
    can_persist: bool = False  # May grab & hold the display while it wants focus


class AppScheduler:
    """
    Manages the display schedule for apps.

    Handles:
    - Round-robin rotation of apps
    - Focus holds: an app may interrupt rotation and stay displayed while active
    - Overlay apps (e.g. clock)
    """

    def __init__(self, config: SchedulerConfig):
        self.config = config

        # Scheduled apps (guarded by _sched_lock)
        self._apps: Dict[str, ScheduledApp] = {}
        self._rotation_order: List[str] = []
        self._sched_lock = threading.RLock()

        # Current state
        self._current_app: Optional[str] = None
        self._current_app_start: float = 0.0
        self._overlay_app: Optional[str] = None

        # Apps currently requesting display focus
        self._focus_requests: Set[str] = set()

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
        can_persist: bool = False,
    ) -> None:
        """Add an app to the schedule."""
        if duration is None:
            duration = self.config.default_app_duration

        with self._sched_lock:
            self._apps[app_id] = ScheduledApp(
                app_id=app_id,
                priority=priority,
                duration=duration,
                is_overlay=is_overlay,
                is_persistent=is_persistent,
                can_persist=can_persist,
            )

            if not is_overlay:
                if app_id not in self._rotation_order:
                    self._rotation_order.append(app_id)
            else:
                self._overlay_app = app_id

            # Start with first app
            if self._current_app is None and not is_overlay:
                self._current_app = app_id
                self._current_app_start = time.time()

        log.info(
            f"Scheduled app '{app_id}' (priority={priority}, duration={duration}s, "
            f"persist={can_persist})"
        )

    def update_app(
        self,
        app_id: str,
        duration: Optional[float] = None,
        priority: Optional[int] = None,
        can_persist: Optional[bool] = None,
    ) -> bool:
        """Update a scheduled app's rotation settings in place."""
        with self._sched_lock:
            sched = self._apps.get(app_id)
            if sched is None:
                return False
            if duration is not None:
                sched.duration = duration
            if priority is not None:
                sched.priority = priority
            if can_persist is not None:
                sched.can_persist = can_persist
            return True

    def remove_app(self, app_id: str) -> None:
        """Remove an app from the schedule."""
        with self._sched_lock:
            if app_id in self._apps:
                del self._apps[app_id]
            if app_id in self._rotation_order:
                self._rotation_order.remove(app_id)
            if app_id == self._overlay_app:
                self._overlay_app = None
            self._focus_requests.discard(app_id)

            # If we removed the current app, move to another one
            if self._current_app == app_id:
                self._current_app = self._rotation_order[0] if self._rotation_order else None
                self._current_app_start = time.time()

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

    def get_focus_requests(self) -> Set[str]:
        """Get the set of app IDs currently requesting a display hold."""
        with self._sched_lock:
            return set(self._focus_requests)

    def get_scheduled(self, app_id: str) -> Optional[ScheduledApp]:
        """Get the scheduling record for an app, if present."""
        with self._sched_lock:
            return self._apps.get(app_id)

    def set_focus(self, app_id: str, wanted: bool) -> None:
        """Record or clear an app's request to hold the display."""
        with self._sched_lock:
            if wanted:
                self._focus_requests.add(app_id)
            else:
                self._focus_requests.discard(app_id)

    def _focused_app_locked(self) -> Optional[str]:
        """The app that should currently hold the display, if any.

        Must be called while holding ``_sched_lock``.
        """
        candidates = [
            self._apps[aid]
            for aid in self._focus_requests
            if aid in self._apps and self._apps[aid].can_persist
        ]
        if not candidates:
            return None
        # Highest priority wins; ties broken by rotation order for stability.
        candidates.sort(
            key=lambda s: (
                -s.priority,
                (
                    self._rotation_order.index(s.app_id)
                    if s.app_id in self._rotation_order
                    else 1_000_000
                ),
            )
        )
        return candidates[0].app_id

    def get_active_apps(self) -> List[str]:
        """Get list of apps that should be running."""
        active = []

        if self._current_app:
            active.append(self._current_app)

        if self._overlay_app:
            active.append(self._overlay_app)

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

        with self._sched_lock:
            focused = self._focused_app_locked()

            if focused:
                # An app is holding the display: pin to it, suppress rotation.
                if self._current_app != focused:
                    old_app = self._current_app
                    self._current_app = focused
                    self._current_app_start = current_time
                    if self._on_app_change and old_app != focused:
                        self._on_app_change(old_app, focused)
            elif self._current_app and len(self._rotation_order) > 1:
                # Normal round-robin rotation.
                sched = self._apps.get(self._current_app)
                if sched and (current_time - self._current_app_start) >= sched.duration:
                    self._rotate_next()

            current_app = self._current_app

        # Get current frame
        with self._frame_lock:
            if current_app and current_app in self._frames:
                return self._frames[current_app]
            return None

    def _rotate_next(self) -> None:
        """Rotate to the next app in order. Caller must hold ``_sched_lock``."""
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
        with self._sched_lock:
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
