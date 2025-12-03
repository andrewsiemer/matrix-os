"""
MatrixOS Apps

Each app lives in its own folder with an app.py file containing the implementation.
"""

from .base import AppManifest, BaseApp

# Import apps from their folders
from .clock import BasicClockApp, BinaryClockApp
from .dvd import DVDApp
from .earth import EarthApp
from .imageviewer import ImageViewerApp
from .slack import SlackStatusApp
from .stocks import StocksApp
from .weather import WeatherApp
from .welcome import WelcomeApp

__all__ = [
    # Base
    "BaseApp",
    "AppManifest",
    # Apps
    "BasicClockApp",
    "BinaryClockApp",
    "DVDApp",
    "EarthApp",
    "ImageViewerApp",
    "SlackStatusApp",
    "StocksApp",
    "WeatherApp",
    "WelcomeApp",
]
