"""
App sandboxing for MatrixOS.

Provides isolation for apps via threads or processes based on their capabilities.
"""

import logging
import multiprocessing
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Dict, Optional, Set

if TYPE_CHECKING:
    from ..apps.base import BaseApp
    from .ipc import AppChannel

log = logging.getLogger(__name__)


class ExecutionMode(Enum):
    """How an app should be executed."""

    THREAD = auto()  # Fast, shared memory, for trusted apps
    PROCESS = auto()  # Isolated, for network/filesystem apps or untrusted code


class Capability(Enum):
    """Capabilities an app can request."""

    NETWORK = auto()  # Can make network requests
    FILESYSTEM = auto()  # Can access filesystem
    SYSTEM_INFO = auto()  # Can read system info (time, IP, etc.)
    HIGH_FRAMERATE = auto()  # Needs >30 FPS
    PERSISTENT_STATE = auto()  # Can save/load state


@dataclass
class AppManifest:
    """
    Manifest declaring an app's requirements and metadata.

    Apps declare what capabilities they need, and the sandbox
    enforces these restrictions.
    """

    name: str
    version: str = "1.0.0"
    author: str = "unknown"
    description: str = ""

    # Execution configuration
    execution_mode: ExecutionMode = ExecutionMode.THREAD
    framerate: int = 30

    # Required capabilities
    capabilities: Set[Capability] = field(default_factory=set)

    # Resource limits
    max_memory_mb: int = 50
    max_cpu_percent: float = 25.0

    def requires_process(self) -> bool:
        """Check if this app should run in a separate process."""
        # Apps with network or filesystem access run in processes for isolation
        dangerous_caps = {Capability.NETWORK, Capability.FILESYSTEM}
        return (
            bool(self.capabilities & dangerous_caps) or self.execution_mode == ExecutionMode.PROCESS
        )


def _process_run_loop(app: "BaseApp", send_queue, recv_queue, app_id: str) -> None:
    """
    Run loop for process-based apps.

    This function runs in a separate process and handles the app lifecycle.
    """
    import time
    from queue import Empty

    from .ipc import Message, MessageType

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
                        # Send just the numpy array data for efficiency
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
    Wraps an app instance for sandboxed execution.

    Handles the execution loop, IPC communication, and lifecycle management.
    """

    def __init__(self, app: "BaseApp", channel: "AppChannel"):
        self.app = app
        self.channel = channel
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[multiprocessing.Process] = None

    def _run_loop_thread(self) -> None:
        """Main execution loop for thread-based apps."""
        import time

        from .ipc import MessageType

        try:
            log.debug(f"App '{self.app.manifest.name}' initializing...")
            self.app.on_start()
            self.channel.report_ready()
            log.debug(f"App '{self.app.manifest.name}' ready")

            frame_interval = 1.0 / max(1, self.app.manifest.framerate)
            last_frame_time = 0.0

            while self._running:
                # Check for messages from kernel
                msg = self.channel.receive(timeout=0.001)
                if msg:
                    if msg.type == MessageType.APP_STOP:
                        break
                    elif msg.type == MessageType.APP_PAUSE:
                        self._paused = True
                    elif msg.type == MessageType.APP_RESUME:
                        self._paused = False
                    elif msg.type == MessageType.SYSTEM_SHUTDOWN:
                        break

                if self._paused:
                    time.sleep(0.01)
                    continue

                # Frame timing
                current_time = time.time()
                if current_time - last_frame_time >= frame_interval:
                    try:
                        self.app.update()
                        framebuffer = self.app.render()
                        if framebuffer:
                            self.channel.submit_frame(framebuffer)
                    except Exception as e:
                        log.error(f"App '{self.app.manifest.name}' render error: {e}")

                    last_frame_time = current_time
                else:
                    sleep_time = frame_interval - (current_time - last_frame_time)
                    if sleep_time > 0.001:
                        time.sleep(sleep_time * 0.9)

        except Exception as e:
            log.exception(f"App {self.app.manifest.name} crashed: {e}")
            self.channel.report_error(e)
        finally:
            log.debug(f"App '{self.app.manifest.name}' stopping...")
            self.app.on_stop()

    def start(self) -> None:
        """Start the app in its sandbox."""
        if self._running:
            return

        self._running = True

        if self.app.manifest.requires_process():
            # For process-based apps, pass the queues directly
            # The app will be pickled and sent to the child process
            self._process = multiprocessing.Process(
                target=_process_run_loop,
                args=(
                    self.app,
                    self.channel.send_queue,
                    self.channel.recv_queue,
                    self.channel.app_id,
                ),
                name=f"matrixos-{self.app.manifest.name}",
                daemon=True,
            )
            self._process.start()
            log.info(f"Started app '{self.app.manifest.name}' in process {self._process.pid}")
        else:
            self._thread = threading.Thread(
                target=self._run_loop_thread,
                name=f"matrixos-{self.app.manifest.name}",
                daemon=True,
            )
            self._thread.start()
            log.info(f"Started app '{self.app.manifest.name}' in thread")

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the app gracefully."""
        self._running = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning(f"App '{self.app.manifest.name}' did not stop gracefully")

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
    Manages sandboxed execution of multiple apps.
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
