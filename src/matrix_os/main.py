#!/usr/bin/env python3
"""
MatrixOS - Main Entry Point

A modular LED matrix display system with sandboxed apps.

Usage:
    python -m matrix_os
    python src/matrix_os/main.py
    python -m matrix_os --web-only  # Run only the web server (for testing without hardware)

The system runs a non-blocking render loop that composites frames
from multiple sandboxed apps.
"""

import argparse
import logging
import os
import threading

from matrix_os.apps import BasicClockApp, DVDApp, EarthApp, ImageViewerApp, SlackStatusApp
from matrix_os.apps.stocks import StocksApp
from matrix_os.apps.weather import WeatherApp
from matrix_os.core import Kernel, SystemConfig

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s : %(levelname)-8s : (%(name)s) %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("matrix_os")

# Suppress noisy third-party loggers
logging.getLogger("sse_starlette").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)


def setup_web_integration(kernel):
    """Set up web interface integration with the kernel."""
    from matrix_os.core import set_app_change_callback, set_frame_callback
    from matrix_os.web import AppInfo, get_shared_state

    shared_state = get_shared_state()

    # Set display dimensions from kernel config
    shared_state.display_width = kernel.display.width
    shared_state.display_height = kernel.display.height

    # Set up frame callback
    def on_frame(frame):
        shared_state.set_frame(frame)

    set_frame_callback(on_frame)

    # Register apps with web interface
    def register_app_info(app_id):
        if app_id in kernel.app_instances:
            app = kernel.app_instances[app_id]
            manifest = app.manifest
            current_app_id = kernel.get_current_app_id()
            shared_state.register_app(
                AppInfo(
                    app_id=app_id,
                    name=manifest.name,
                    version=manifest.version,
                    author=manifest.author,
                    description=manifest.description,
                    is_active=app_id == current_app_id,
                )
            )

    set_app_change_callback(register_app_info)

    # Set up scheduler callback for app changes
    def on_app_change(old_app, new_app):
        shared_state.set_current_app(new_app)

    kernel.scheduler.on_app_change(on_app_change)

    return shared_state


def setup_web_logging():
    """Set up web log handler to capture all logs for the web interface."""
    import multiprocessing
    from logging.handlers import QueueListener

    from matrix_os.core.sandbox import set_log_queue
    from matrix_os.web import WebLogHandler, get_shared_state

    shared_state = get_shared_state()

    root_logger = logging.getLogger()

    # Add web log handler to root logger (doesn't print to console, just stores for web)
    web_handler = WebLogHandler(shared_state)
    root_logger.addHandler(web_handler)

    # Set up multiprocessing queue for child process logs
    log_queue = multiprocessing.Queue()
    set_log_queue(log_queue)

    # Get the existing console handler to also receive child process logs
    console_handler = None
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, WebLogHandler):
            console_handler = handler
            break

    # Create a listener that forwards logs from child processes to handlers
    handlers = [web_handler]
    if console_handler:
        handlers.append(console_handler)

    queue_listener = QueueListener(
        log_queue,
        *handlers,
        respect_handler_level=True,
    )
    queue_listener.start()

    # Store listener to prevent garbage collection
    shared_state._queue_listener = queue_listener

    return shared_state


def run_web_server_thread(host: str = "0.0.0.0", port: int = 8000):
    """Run the web server in a background thread."""
    import asyncio
    import time

    import uvicorn

    from matrix_os.web import create_app, get_shared_state

    shared_state = get_shared_state()
    app = create_app(shared_state)

    log.info("Starting web server at http://%s:%d", host, port)

    # Configure uvicorn
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def run_server():
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    # Run in a daemon thread
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()

    # Give the server a moment to start
    time.sleep(0.5)

    return thread


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="MatrixOS - LED Matrix Display System")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Web server host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Web server port (default: 8000)",
    )
    args = parser.parse_args()

    # Set up web logging early to capture all logs
    setup_web_logging()

    log.info("=" * 50)
    log.info("MatrixOS Starting...")
    log.info("=" * 50)

    # Create kernel with default config
    config = SystemConfig()
    kernel = Kernel(config)

    # Set up web integration
    setup_web_integration(kernel)

    # Register apps
    # Each app runs in its own process and communicates with the kernel via IPC

    # Fun/visual apps
    kernel.register_app(DVDApp, duration=15)
    kernel.register_app(EarthApp, duration=15)
    kernel.register_app(StocksApp, symbol="NVDA", duration=15)
    kernel.register_app(StocksApp, symbol="VTI", duration=15)
    kernel.register_app(WeatherApp, duration=15)
    kernel.register_app(SlackStatusApp, duration=15)
    kernel.register_app(BasicClockApp, duration=15)
    # kernel.register_app(BinaryClockApp, duration=15)

    # Static image
    if os.path.exists(nvidia_path := os.path.join(kernel.images_path, "nvidia.png")):
        kernel.register_app(ImageViewerApp, image_path=nvidia_path, duration=10)

    log.info("All apps registered. Starting kernel...")

    # Start web server in background
    run_web_server_thread(args.host, args.port)

    # Run kernel (auto-detects if hardware is available or runs in simulation mode)
    try:
        kernel.run()
    except KeyboardInterrupt:
        log.info("Shutdown requested...")
    finally:
        kernel.stop()

    log.info("MatrixOS shutdown complete.")


if __name__ == "__main__":
    main()
