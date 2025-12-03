"""
MatrixOS - A modular LED matrix display system with sandboxed apps.

Architecture:
    - Core: Kernel, Display, IPC, Sandbox, Scheduler
    - Apps: Sandboxed applications that render to framebuffers

Each app runs in its own process, isolated from the main render loop.

Example:
    from matrix_os.core import Kernel, SystemConfig
    from matrix_os.apps.dvd import DVDApp

    kernel = Kernel(SystemConfig())
    kernel.register_app(DVDApp, duration=15)
    kernel.run()
"""

__version__ = "2.0.0"
__author__ = "MatrixOS Team"

from .apps import AppManifest, BaseApp
from .core import AppScheduler, Display, FrameBuffer, Kernel, MessageBus, Sandbox, SystemConfig

__all__ = [
    # Core
    "Kernel",
    "Display",
    "FrameBuffer",
    "MessageBus",
    "Sandbox",
    "AppScheduler",
    "SystemConfig",
    # Apps
    "BaseApp",
    "AppManifest",
]
