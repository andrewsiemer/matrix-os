"""
Inter-Process Communication (IPC) message bus for MatrixOS.

Provides thread-safe and process-safe communication between apps and the kernel.
"""

import logging
import multiprocessing
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from multiprocessing import Queue as MPQueue
from queue import Empty, Queue
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


class MessageType(Enum):
    """Types of IPC messages."""

    # Frame submission
    FRAME_READY = auto()
    FRAME_REQUEST = auto()

    # App lifecycle
    APP_START = auto()
    APP_STOP = auto()
    APP_PAUSE = auto()
    APP_RESUME = auto()
    APP_ERROR = auto()
    APP_READY = auto()

    # System events
    SYSTEM_SHUTDOWN = auto()
    SYSTEM_CONFIG = auto()

    # App requests
    REQUEST_NETWORK = auto()
    REQUEST_FILESYSTEM = auto()
    REQUEST_CANVAS = auto()

    # Responses
    RESPONSE_OK = auto()
    RESPONSE_ERROR = auto()
    RESPONSE_DENIED = auto()


@dataclass
class Message:
    """IPC message container."""

    type: MessageType
    source: str  # App ID or "kernel"
    target: str = "kernel"  # App ID or "kernel"
    payload: Any = None
    timestamp: float = field(default_factory=lambda: __import__("time").time())

    def __repr__(self) -> str:
        return f"Message({self.type.name}, {self.source} -> {self.target})"


class MessageBus:
    """
    Thread-safe message bus for IPC communication.

    Supports both threading and multiprocessing modes for app sandboxing.
    """

    def __init__(self, use_multiprocessing: bool = False):
        self._use_mp = use_multiprocessing
        self._lock = threading.Lock() if not use_multiprocessing else multiprocessing.Lock()

        # Kernel inbox (apps -> kernel)
        self._kernel_queue: Queue = MPQueue() if use_multiprocessing else Queue()

        # App inboxes (kernel -> apps)
        self._app_queues: Dict[str, Queue] = {}

        # Subscribers for broadcast messages
        self._subscribers: Dict[MessageType, List[Callable[[Message], None]]] = {}

        self._running = True

    def create_app_channel(self, app_id: str) -> "AppChannel":
        """Create a dedicated channel for an app."""
        with self._lock:
            if self._use_mp:
                self._app_queues[app_id] = MPQueue()
            else:
                self._app_queues[app_id] = Queue()

            return AppChannel(
                app_id=app_id,
                send_queue=self._kernel_queue,
                recv_queue=self._app_queues[app_id],
            )

    def remove_app_channel(self, app_id: str) -> None:
        """Remove an app's communication channel."""
        with self._lock:
            if app_id in self._app_queues:
                del self._app_queues[app_id]

    def send_to_app(self, app_id: str, message: Message) -> bool:
        """Send a message to a specific app."""
        with self._lock:
            if app_id not in self._app_queues:
                log.warning(f"App {app_id} not found in message bus")
                return False

            try:
                self._app_queues[app_id].put_nowait(message)
                return True
            except Exception as e:
                log.error(f"Failed to send message to {app_id}: {e}")
                return False

    def broadcast(self, message: Message) -> None:
        """Broadcast a message to all apps."""
        with self._lock:
            for app_id, queue in self._app_queues.items():
                try:
                    queue.put_nowait(message)
                except Exception as e:
                    log.error(f"Failed to broadcast to {app_id}: {e}")

    def receive_from_apps(self, timeout: float = 0.001) -> Optional[Message]:
        """Receive a message from any app (non-blocking)."""
        try:
            return self._kernel_queue.get(timeout=timeout)
        except Empty:
            return None

    def subscribe(self, msg_type: MessageType, callback: Callable[[Message], None]) -> None:
        """Subscribe to a specific message type."""
        with self._lock:
            if msg_type not in self._subscribers:
                self._subscribers[msg_type] = []
            self._subscribers[msg_type].append(callback)

    def shutdown(self) -> None:
        """Shutdown the message bus."""
        self._running = False
        # Send shutdown to all apps
        shutdown_msg = Message(
            type=MessageType.SYSTEM_SHUTDOWN,
            source="kernel",
            target="*",
        )
        self.broadcast(shutdown_msg)


@dataclass
class AppChannel:
    """
    Communication channel for an app to talk to the kernel.

    This is the only interface apps have to the outside world.
    """

    app_id: str
    send_queue: Queue
    recv_queue: Queue

    def send(self, msg_type: MessageType, payload: Any = None, target: str = "kernel") -> None:
        """Send a message to the kernel or another app."""
        msg = Message(
            type=msg_type,
            source=self.app_id,
            target=target,
            payload=payload,
        )
        try:
            self.send_queue.put_nowait(msg)
        except Exception as e:
            log.error(f"[{self.app_id}] Failed to send message: {e}")

    def receive(self, timeout: float = 0.001) -> Optional[Message]:
        """Receive a message (non-blocking by default)."""
        try:
            return self.recv_queue.get(timeout=timeout)
        except Empty:
            return None

    def submit_frame(self, framebuffer: Any) -> None:
        """Submit a rendered frame to the kernel."""
        self.send(MessageType.FRAME_READY, payload=framebuffer)

    def report_ready(self) -> None:
        """Report that the app is ready to run."""
        self.send(MessageType.APP_READY)

    def report_error(self, error: Exception) -> None:
        """Report an error to the kernel."""
        self.send(MessageType.APP_ERROR, payload=str(error))
