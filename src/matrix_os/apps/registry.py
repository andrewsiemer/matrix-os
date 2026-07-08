"""
App registry for MatrixOS.

A single source of truth mapping a stable ``type`` string to its app class and
the parameters that can be edited from the web UI. Both the config store (for
seeding defaults) and the web layer (for rendering the config editor and
resolving a config entry to a runnable app) read from here.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Type

from .base import BaseApp
from .clock import BasicClockApp, BinaryClockApp
from .dvd import DVDApp
from .earth import EarthApp
from .imageviewer import ImageViewerApp
from .slack import SlackStatusApp
from .stocks import StocksApp
from .weather import WeatherApp


@dataclass
class ParamSpec:
    """A single editable app parameter."""

    name: str
    label: str
    kind: str  # "str" | "int" | "float"
    default: Any = None
    optional: bool = False


@dataclass
class AppSpec:
    """Describes an app type: its class and editable parameters."""

    type: str
    label: str
    cls: Type[BaseApp]
    params: List[ParamSpec] = field(default_factory=list)
    default_persist: bool = False

    @property
    def supports_persist(self) -> bool:
        """
        Whether this app can persist/interrupt the carousel.

        Only apps with an active/inactive state expose the persist flag. Such
        apps override ``wants_focus()`` (e.g. Slack returns True while a status
        is set); apps that never request focus can't meaningfully persist.
        """
        return self.cls.wants_focus is not BaseApp.wants_focus


APP_REGISTRY: Dict[str, AppSpec] = {
    "dvd": AppSpec("dvd", "DVD Logo", DVDApp),
    "earth": AppSpec("earth", "Earth", EarthApp),
    "clock": AppSpec("clock", "Basic Clock", BasicClockApp),
    "binary_clock": AppSpec("binary_clock", "Binary Clock", BinaryClockApp),
    "stocks": AppSpec(
        "stocks",
        "Stocks",
        StocksApp,
        params=[ParamSpec("symbol", "Ticker Symbol", "str", default="NVDA")],
    ),
    "weather": AppSpec(
        "weather",
        "Weather",
        WeatherApp,
        params=[
            ParamSpec("lat", "Latitude", "float", default=None, optional=True),
            ParamSpec("lon", "Longitude", "float", default=None, optional=True),
        ],
    ),
    "image": AppSpec(
        "image",
        "Image Viewer",
        ImageViewerApp,
        params=[ParamSpec("image_path", "Image Path", "str", default="")],
    ),
    "slack": AppSpec("slack", "Slack Status", SlackStatusApp, default_persist=True),
}


def get_spec(app_type: str) -> AppSpec:
    """Look up an app spec by type. Raises KeyError if unknown."""
    return APP_REGISTRY[app_type]


def coerce_params(app_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Coerce raw param values (e.g. from JSON/form input) to the types declared
    in the registry, dropping unknown keys and empty optional values.
    """
    spec = get_spec(app_type)
    coerced: Dict[str, Any] = {}
    by_name = {p.name: p for p in spec.params}

    for name, value in params.items():
        pspec = by_name.get(name)
        if pspec is None:
            continue
        if value is None or value == "":
            if pspec.optional:
                continue
            value = pspec.default
        try:
            if value is None:
                coerced[name] = None
            elif pspec.kind == "int":
                coerced[name] = int(value)
            elif pspec.kind == "float":
                coerced[name] = float(value)
            else:
                coerced[name] = str(value)
        except (TypeError, ValueError):
            coerced[name] = pspec.default

    return coerced
