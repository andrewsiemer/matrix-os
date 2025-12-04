"""
MatrixOS Web - FastAPI web interface for display simulation and logs.
"""

from .app import AppInfo, SharedState, WebLogHandler, create_app, get_shared_state

__all__ = ["create_app", "get_shared_state", "SharedState", "AppInfo", "WebLogHandler"]
