"""
MatrixOS Kernel - The heart of the system.

Orchestrates the display, apps, IPC, and scheduling.
The main render loop runs here and must never block.
"""

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Callable, Dict, Optional, Type

from .config import SystemConfig
from .display import Display, FrameBuffer
from .ipc import MessageBus, MessageType
from .sandbox import AppWrapper, Sandbox
from .scheduler import AppScheduler

if TYPE_CHECKING:
    from ..apps.base import BaseApp

log = logging.getLogger(__name__)

# Optional callback for frame updates (used by web interface)
_frame_callback: Optional[Callable[[FrameBuffer], None]] = None
_app_change_callback: Optional[Callable[[str], None]] = None


def set_frame_callback(callback: Optional[Callable[[FrameBuffer], None]]) -> None:
    """Set a callback to receive frame updates."""
    global _frame_callback
    _frame_callback = callback


def set_app_change_callback(callback: Optional[Callable[[str], None]]) -> None:
    """Set a callback to receive app change notifications."""
    global _app_change_callback
    _app_change_callback = callback


class Kernel:
    """
    The MatrixOS kernel.

    Responsibilities:
    - Initialize hardware (display)
    - Manage app lifecycle
    - Run the main render loop (non-blocking)
    - Handle IPC messages
    - Coordinate scheduling
    """

    def __init__(self, config: Optional[SystemConfig] = None):
        self.config = config or SystemConfig()

        # Core components
        self.display = Display(self.config.display)
        # Use multiprocessing queues for true process isolation
        self.message_bus = MessageBus(use_multiprocessing=True)
        self.sandbox = Sandbox()
        self.scheduler = AppScheduler(self.config.scheduler)

        # App registry
        self._app_instances: Dict[str, "BaseApp"] = {}
        self._app_counter = 0

        # Runtime state
        self._running = False
        self._render_thread: Optional[threading.Thread] = None

        # Paths
        self._base_path = os.path.dirname(os.path.dirname(__file__))
        self._fonts_path = os.path.join(self._base_path, "..", "..", "fonts")
        self._images_path = os.path.join(self._base_path, "..", "..", "images")

    @property
    def fonts_path(self) -> str:
        return os.path.abspath(self._fonts_path)

    @property
    def images_path(self) -> str:
        return os.path.abspath(self._images_path)

    @property
    def app_instances(self) -> Dict[str, "BaseApp"]:
        """Get all registered app instances."""
        return self._app_instances

    def get_current_app_id(self) -> Optional[str]:
        """Get the currently active app ID."""
        return self.scheduler.get_current_app()

    def create_framebuffer(self) -> FrameBuffer:
        """Create a framebuffer for apps to render to."""
        return self.display.create_framebuffer()

    def register_app(
        self,
        app_class: Type["BaseApp"],
        *args,
        priority: int = 0,
        duration: float = None,
        **kwargs,
    ) -> str:
        """
        Register and instantiate an app.

        Returns the app ID for later reference.
        """
        self._app_counter += 1
        app_id = f"{app_class.__name__.lower()}_{self._app_counter}"

        # Create IPC channel for the app
        channel = self.message_bus.create_app_channel(app_id)

        # Create framebuffer for the app
        framebuffer = self.create_framebuffer()

        # Instantiate the app
        app = app_class(
            app_id=app_id,
            framebuffer=framebuffer,
            channel=channel,
            kernel=self,
            *args,
            **kwargs,
        )

        # Store and wrap for sandboxing
        self._app_instances[app_id] = app
        wrapper = AppWrapper(app, channel)
        self.sandbox.register(app_id, wrapper)

        # Add to scheduler
        self.scheduler.add_app(
            app_id,
            priority=priority,
            duration=duration,
            is_overlay=app.manifest.framerate > 30,  # High FPS apps as overlays
        )

        log.info(f"Registered app '{app_id}' ({app.manifest.name} v{app.manifest.version})")

        # Notify web interface about the new app
        if _app_change_callback:
            try:
                _app_change_callback(app_id)
            except Exception:
                pass

        return app_id

    def unregister_app(self, app_id: str) -> bool:
        """Unregister and stop an app."""
        if app_id not in self._app_instances:
            return False

        self.sandbox.unregister(app_id)
        self.scheduler.remove_app(app_id)
        self.message_bus.remove_app_channel(app_id)
        del self._app_instances[app_id]

        log.info(f"Unregistered app '{app_id}'")
        return True

    def _process_messages(self) -> None:
        """Process pending IPC messages (non-blocking)."""
        # Process up to 10 messages per tick to avoid starvation
        for _ in range(10):
            msg = self.message_bus.receive_from_apps(timeout=0.0001)
            if msg is None:
                break

            if msg.type == MessageType.FRAME_READY:
                # App submitted a frame
                self.scheduler.submit_frame(msg.source, msg.payload)

            elif msg.type == MessageType.APP_READY:
                log.debug(f"App '{msg.source}' reported ready")

            elif msg.type == MessageType.APP_ERROR:
                log.error(f"App '{msg.source}' error: {msg.payload}")

            # Add more message handlers as needed

    def _render_loop(self) -> None:
        """
        Main render loop. Runs in a dedicated thread.

        This loop must NEVER block. All operations must be non-blocking
        or have strict timeouts.
        """
        target_fps = 60
        frame_time = 1.0 / target_fps

        log.info("Render loop started")

        while self._running:
            loop_start = time.time()

            # Process IPC messages (non-blocking)
            self._process_messages()

            # Get current frame from scheduler
            frame = self.scheduler.tick()

            # Render to display
            if frame:
                self.display.render(frame)

                # Notify web interface if callback is set
                if _frame_callback:
                    try:
                        _frame_callback(frame)
                    except Exception:
                        pass  # Don't let web callback errors affect rendering

            # Frame timing - sleep for remainder of frame time
            elapsed = time.time() - loop_start
            sleep_time = frame_time - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        log.info("Render loop stopped")

    def start(self) -> None:
        """Start the kernel and all registered apps."""
        log.info("Starting MatrixOS kernel...")

        # Initialize display
        if not self.display.initialize():
            log.error("Failed to initialize display")
            return

        self._running = True

        # Start all apps in their sandboxes
        self.sandbox.start_all()

        # Start render loop in separate thread
        self._render_thread = threading.Thread(
            target=self._render_loop,
            name="matrixos-render",
            daemon=True,
        )
        self._render_thread.start()

        log.info("MatrixOS kernel started")

    def run(self) -> None:
        """Start and run until interrupted. Let KeyboardInterrupt propagate to caller."""
        self.start()

        try:
            while self._running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            log.info("Keyboard interrupt received")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the kernel and all apps gracefully."""
        log.info("Stopping MatrixOS kernel...")

        self._running = False

        # Stop all apps
        self.sandbox.stop_all()

        # Shutdown message bus
        self.message_bus.shutdown()

        # Wait for render thread
        if self._render_thread and self._render_thread.is_alive():
            self._render_thread.join(timeout=2.0)

        # Shutdown display
        self.display.shutdown()

        log.info("MatrixOS kernel stopped")
