#!/usr/bin/env python3
"""
MatrixOS - Main Entry Point

A modular LED matrix display system with sandboxed apps.

Usage:
    python -m matrix_os
    python src/matrix_os/main.py

The system runs a non-blocking render loop that composites frames
from multiple sandboxed apps.
"""

import logging
import os
import sys

# Ensure the package is importable when run directly
if __name__ == "__main__":
    # Add the src directory to the path so we can import matrix_os as a package
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s (%(name)s) %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("matrixos")


def main():
    """Main entry point."""
    # Import using absolute package paths
    from matrix_os.apps.dvd import DVDApp
    from matrix_os.apps.earth import EarthApp
    from matrix_os.apps.imageviewer import ImageViewerApp
    from matrix_os.apps.stocks import StocksApp
    from matrix_os.core import Kernel, SystemConfig

    # These are available but not enabled by default:
    # from matrix_os.apps.weather import WeatherApp
    # from matrix_os.apps.slack import SlackStatusApp
    # from matrix_os.apps.welcome import WelcomeApp
    # from matrix_os.apps.clock import BasicClockApp, BinaryClockApp

    log.info("=" * 50)
    log.info("MatrixOS Starting...")
    log.info("=" * 50)

    # Create kernel with default config
    config = SystemConfig()
    kernel = Kernel(config)

    # Register apps
    # Each app runs in its own sandbox (thread or process)
    # and communicates with the kernel via IPC

    # Fun/visual apps
    kernel.register_app(DVDApp, duration=15)
    kernel.register_app(EarthApp, duration=15)

    # Stock ticker (using single symbol to avoid API rate limits)
    kernel.register_app(StocksApp, symbol="NVDA", duration=15)

    # Static image
    nvidia_path = os.path.join(kernel.images_path, "nvidia.png")
    if os.path.exists(nvidia_path):
        kernel.register_app(ImageViewerApp, image_path=nvidia_path, duration=10)

    log.info("All apps registered. Starting kernel...")

    # Run the kernel (blocks until interrupted)
    try:
        kernel.run()
    except KeyboardInterrupt:
        log.info("Shutdown requested...")
    finally:
        kernel.stop()

    log.info("MatrixOS shutdown complete.")


if __name__ == "__main__":
    main()
