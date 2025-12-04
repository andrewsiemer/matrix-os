"""
App sandboxing for MatrixOS.

All apps run in separate processes for true isolation from the render loop.
"""

import logging
import logging.handlers
import multiprocessing
import threading
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from ..apps.base import BaseApp
    from .ipc import AppChannel

log = logging.getLogger(__name__)

# Shared queue for forwarding logs from child processes to main process
_log_queue: Optional[multiprocessing.Queue] = None


def set_log_queue(queue: multiprocessing.Queue) -> None:
    """Set the log queue for child processes to use."""
    global _log_queue
    _log_queue = queue


def get_log_queue() -> Optional[multiprocessing.Queue]:
    """Get the log queue."""
    return _log_queue


def _setup_child_logging(log_queue: multiprocessing.Queue) -> None:
    """Set up logging in child process to forward to main process."""
    # Remove all existing handlers from root logger
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Add queue handler to forward logs to main process
    queue_handler = logging.handlers.QueueHandler(log_queue)
    queue_handler.setLevel(logging.INFO)
    root.addHandler(queue_handler)
    root.setLevel(logging.INFO)


def _process_run_loop(app: "BaseApp", send_queue, recv_queue, app_id: str, log_queue) -> None:
    """
    Run loop for apps. Runs in a separate process.
    """
    import time
    from queue import Empty

    from .ipc import Message, MessageType

    # Set up logging to forward to main process
    if log_queue:
        _setup_child_logging(log_queue)

    running = True
    paused = False

    def send_msg(msg_type: MessageType, payload=None):
        msg = Message(type=msg_type, source=app_id, payload=payload)
        try:
            send_queue.put_nowait(msg)
        except Exception as e:
            log.error(f"[{app_id}] Failed to send: {e}")

    def recv_msg(timeout=0.001):
        try:
            return recv_queue.get(timeout=timeout)
        except Empty:
            return None

    try:
        log.debug(f"App '{app.manifest.name}' initializing in process...")
        app.on_start()
        send_msg(MessageType.APP_READY)
        log.debug(f"App '{app.manifest.name}' ready")

        frame_interval = 1.0 / max(1, app.manifest.framerate)
        last_frame_time = 0.0

        while running:
            # Check for messages from kernel
            msg = recv_msg(timeout=0.001)
            if msg:
                if msg.type == MessageType.APP_STOP:
                    break
                elif msg.type == MessageType.APP_PAUSE:
                    paused = True
                elif msg.type == MessageType.APP_RESUME:
                    paused = False
                elif msg.type == MessageType.SYSTEM_SHUTDOWN:
                    break

            if paused:
                time.sleep(0.01)
                continue

            # Frame timing
            current_time = time.time()
            if current_time - last_frame_time >= frame_interval:
                try:
                    app.update()
                    framebuffer = app.render()
                    if framebuffer:
                        send_msg(MessageType.FRAME_READY, payload=framebuffer)
                except Exception as e:
                    log.error(f"App '{app.manifest.name}' render error: {e}")

                last_frame_time = current_time
            else:
                sleep_time = frame_interval - (current_time - last_frame_time)
                if sleep_time > 0.001:
                    time.sleep(sleep_time * 0.9)

    except Exception as e:
        log.exception(f"App {app.manifest.name} crashed: {e}")
        send_msg(MessageType.APP_ERROR, payload=str(e))
    finally:
        log.debug(f"App '{app.manifest.name}' stopping...")
        app.on_stop()


class AppWrapper:
    """
    Wraps an app instance for process-based execution.
    """

    def __init__(self, app: "BaseApp", channel: "AppChannel"):
        self.app = app
        self.channel = channel
        self._running = False
        self._paused = False
        self._process: Optional[multiprocessing.Process] = None

    def start(self) -> None:
        """Start the app in its own process."""
        if self._running:
            return

        self._running = True

        self._process = multiprocessing.Process(
            target=_process_run_loop,
            args=(
                self.app,
                self.channel.send_queue,
                self.channel.recv_queue,
                self.channel.app_id,
                get_log_queue(),
            ),
            name=f"matrixos-{self.app.manifest.name}",
            daemon=True,
        )
        self._process.start()
        log.info(f"Started app '{self.app.manifest.name}' in process {self._process.pid}")

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the app gracefully."""
        self._running = False

        if self._process and self._process.is_alive():
            self._process.join(timeout=timeout)
            if self._process.is_alive():
                log.warning(f"Force terminating app '{self.app.manifest.name}'")
                self._process.terminate()
                self._process.join(timeout=1.0)

    def pause(self) -> None:
        """Pause the app."""
        self._paused = True

    def resume(self) -> None:
        """Resume the app."""
        self._paused = False

    @property
    def is_running(self) -> bool:
        return self._running and not self._paused


class Sandbox:
    """
    Manages process-based execution of apps.
    """

    def __init__(self):
        self._apps: Dict[str, AppWrapper] = {}
        self._lock = threading.Lock()

    def register(self, app_id: str, wrapper: AppWrapper) -> None:
        """Register an app wrapper."""
        with self._lock:
            self._apps[app_id] = wrapper

    def unregister(self, app_id: str) -> None:
        """Unregister an app wrapper."""
        with self._lock:
            if app_id in self._apps:
                self._apps[app_id].stop()
                del self._apps[app_id]

    def start(self, app_id: str) -> bool:
        """Start a specific app."""
        with self._lock:
            if app_id in self._apps:
                self._apps[app_id].start()
                return True
            return False

    def stop(self, app_id: str) -> bool:
        """Stop a specific app."""
        with self._lock:
            if app_id in self._apps:
                self._apps[app_id].stop()
                return True
            return False

    def start_all(self) -> None:
        """Start all registered apps."""
        with self._lock:
            for wrapper in self._apps.values():
                wrapper.start()

    def stop_all(self) -> None:
        """Stop all running apps."""
        with self._lock:
            for wrapper in self._apps.values():
                wrapper.stop()

    def get_running_apps(self) -> list:
        """Get list of running app IDs."""
        with self._lock:
            return [app_id for app_id, w in self._apps.items() if w.is_running]
