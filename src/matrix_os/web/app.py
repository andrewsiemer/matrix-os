"""
FastAPI application for MatrixOS web interface.

Provides:
- Real-time display simulation via MJPEG streaming
- Real-time log streaming via SSE
- App information display
"""

import asyncio
import base64
import io
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Deque, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

if TYPE_CHECKING:
    from ..core.display import FrameBuffer

log = logging.getLogger(__name__)

# Template and static directories
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@dataclass
class AppInfo:
    """Information about a running app."""

    app_id: str
    name: str
    version: str
    author: str
    description: str
    is_active: bool = False


@dataclass
class SharedState:
    """
    Shared state between kernel and web server.

    Thread-safe container for the current frame and log messages.
    """

    _frame: Optional["FrameBuffer"] = None
    _frame_lock: threading.Lock = field(default_factory=threading.Lock)
    _logs: Deque[Dict] = field(default_factory=lambda: deque(maxlen=1000))
    _log_lock: threading.Lock = field(default_factory=threading.Lock)
    _apps: Dict[str, AppInfo] = field(default_factory=dict)
    _apps_lock: threading.Lock = field(default_factory=threading.Lock)
    _current_app: Optional[str] = None
    display_width: int = 64
    display_height: int = 32
    scale_factor: int = 12

    def set_frame(self, frame: "FrameBuffer") -> None:
        """Update the current frame (thread-safe)."""
        with self._frame_lock:
            self._frame = frame.copy()

    def get_frame(self) -> Optional["FrameBuffer"]:
        """Get a copy of the current frame (thread-safe)."""
        with self._frame_lock:
            return self._frame.copy() if self._frame else None

    def add_log(self, record: Dict) -> None:
        """Add a log record (thread-safe)."""
        with self._log_lock:
            self._logs.append(record)

    def get_logs(self, since_index: int = 0) -> List[Dict]:
        """Get logs since a given index (thread-safe)."""
        with self._log_lock:
            logs = list(self._logs)
        return logs[since_index:]

    def get_log_count(self) -> int:
        """Get the current log count."""
        with self._log_lock:
            return len(self._logs)

    def register_app(self, app_info: AppInfo) -> None:
        """Register an app's info (thread-safe)."""
        with self._apps_lock:
            self._apps[app_info.app_id] = app_info

    def set_current_app(self, app_id: Optional[str]) -> None:
        """Set the currently active app (thread-safe)."""
        with self._apps_lock:
            self._current_app = app_id
            for aid, info in self._apps.items():
                info.is_active = aid == app_id

    def get_apps(self) -> List[AppInfo]:
        """Get all registered apps (thread-safe)."""
        with self._apps_lock:
            return list(self._apps.values())

    def get_current_app_id(self) -> Optional[str]:
        """Get the current app ID."""
        with self._apps_lock:
            return self._current_app


# Global shared state
_shared_state: Optional[SharedState] = None


def get_shared_state() -> SharedState:
    """Get or create the global shared state."""
    global _shared_state
    if _shared_state is None:
        _shared_state = SharedState()
    return _shared_state


class WebLogHandler(logging.Handler):
    """
    Logging handler that forwards logs to the web interface.
    """

    def __init__(self, shared_state: SharedState):
        super().__init__()
        self.shared_state = shared_state
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s : %(levelname)-8s : (%(name)s) %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            log_entry = {
                "timestamp": time.time(),
                "time": self.format(record).split(" : ")[0],
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "formatted": self.format(record),
            }
            self.shared_state.add_log(log_entry)
        except Exception:
            self.handleError(record)


def create_app(shared_state: Optional[SharedState] = None) -> FastAPI:
    """Create the FastAPI application."""
    if shared_state is None:
        shared_state = get_shared_state()

    app = FastAPI(
        title="MatrixOS Web Interface",
        description="Real-time display simulation and log viewer for MatrixOS",
        version="1.0.0",
    )

    # Add middleware to prevent caching of HTML pages
    from starlette.middleware.base import BaseHTTPMiddleware

    class NoCacheMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if "text/html" in response.headers.get("content-type", ""):
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

    app.add_middleware(NoCacheMiddleware)

    # Mount static files
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Set up Jinja2 templates
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    @app.get("/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok"}

    @app.get("/test", response_class=HTMLResponse)
    async def test():
        """Simple test page."""
        return "<html><body style='background:#000;color:#fff'><h1>MatrixOS Test</h1><p>Server is working!</p></body></html>"

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        """Main page with display simulation."""
        # Calculate display dimensions
        display_w = shared_state.display_width * shared_state.scale_factor
        display_h = shared_state.display_height * shared_state.scale_factor

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "active_page": "display",
                "display_width": display_w,
                "display_height": display_h,
            },
        )

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request):
        """Real-time logs page."""
        return templates.TemplateResponse(
            "logs.html",
            {
                "request": request,
                "active_page": "logs",
            },
        )

    @app.get("/stream")
    async def stream_display():
        """MJPEG stream of the current display."""
        from PIL import Image

        async def generate():
            frame_interval = 1.0 / 30  # 30 FPS target

            while True:
                # Get current frame
                frame = shared_state.get_frame()

                if frame:
                    img = frame.to_image()
                else:
                    # Create a dark placeholder
                    img = Image.new(
                        "RGB",
                        (shared_state.display_width, shared_state.display_height),
                        (5, 5, 5),
                    )

                # Scale up for visibility
                scaled = img.resize(
                    (
                        shared_state.display_width * shared_state.scale_factor,
                        shared_state.display_height * shared_state.scale_factor,
                    ),
                    Image.Resampling.NEAREST,
                )

                # Encode as JPEG
                buffer = io.BytesIO()
                scaled.save(buffer, format="JPEG", quality=85)
                jpeg_bytes = buffer.getvalue()

                # Yield MJPEG frame
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg_bytes)).encode() + b"\r\n"
                    b"\r\n" + jpeg_bytes + b"\r\n"
                )

                # Rate limit (non-blocking)
                await asyncio.sleep(frame_interval)

        return StreamingResponse(
            generate(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/api/apps")
    async def get_apps():
        """Get list of registered apps."""
        apps = shared_state.get_apps()
        return [
            {
                "app_id": app.app_id,
                "name": app.name,
                "version": app.version,
                "author": app.author,
                "description": app.description,
                "is_active": app.is_active,
            }
            for app in apps
        ]

    @app.get("/api/logs/stream")
    async def stream_logs(request: Request):
        """SSE endpoint for real-time log streaming."""

        async def event_generator():
            last_index = 0

            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                # Get new logs since last check
                logs = shared_state.get_logs(last_index)
                if logs:
                    for log_entry in logs:
                        yield {
                            "event": "message",
                            "data": json.dumps(log_entry),
                        }
                    last_index = shared_state.get_log_count()

                await asyncio.sleep(0.1)  # Poll every 100ms

        return EventSourceResponse(event_generator())

    @app.get("/api/frame")
    async def get_frame():
        """Get current frame as base64 PNG."""
        from PIL import Image

        frame = shared_state.get_frame()
        if frame:
            img = frame.to_image()
        else:
            img = Image.new(
                "RGB",
                (shared_state.display_width, shared_state.display_height),
                (0, 0, 0),
            )

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return {
            "frame": base64.b64encode(buffer.getvalue()).decode(),
            "width": shared_state.display_width,
            "height": shared_state.display_height,
        }

    return app
