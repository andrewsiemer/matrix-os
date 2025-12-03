"""
MatrixOS Core - System kernel, display management, IPC, and sandboxing.
"""

from .config import SystemConfig
from .display import Display, FrameBuffer
from .ipc import Message, MessageBus, MessageType
from .kernel import Kernel
from .sandbox import ExecutionMode, Sandbox
from .scheduler import AppScheduler

__all__ = [
    "Kernel",
    "Display",
    "FrameBuffer",
    "MessageBus",
    "Message",
    "MessageType",
    "Sandbox",
    "ExecutionMode",
    "AppScheduler",
    "SystemConfig",
]
