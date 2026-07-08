"""
Declarative app configuration for MatrixOS.

Defines which apps are registered, their rotation duration, whether they may
persist/interrupt the carousel, and their app-specific parameters. Persisted to
a JSON file so edits made from the web UI survive restarts.
"""

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Repo root: src/matrix_os/core/appconfig.py -> up four levels
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_IMAGES_DIR = os.path.join(_REPO_ROOT, "images")


@dataclass
class AppEntry:
    """A single configured app instance."""

    id: str
    type: str
    enabled: bool = True
    duration: float = 15.0
    persist: bool = False
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppEntry":
        return cls(
            id=data["id"],
            type=data["type"],
            enabled=bool(data.get("enabled", True)),
            duration=float(data.get("duration", 15.0)),
            persist=bool(data.get("persist", False)),
            params=dict(data.get("params", {})),
        )


def _default_entries() -> List[AppEntry]:
    """Seed roster mirroring the historical hardcoded main.py registration."""
    entries = [
        AppEntry("dvd", "dvd", duration=15),
        AppEntry("earth", "earth", duration=15),
        AppEntry("stocks-nvda", "stocks", duration=15, params={"symbol": "NVDA"}),
        AppEntry("stocks-vti", "stocks", duration=15, params={"symbol": "VTI"}),
        AppEntry("weather", "weather", duration=15),
        AppEntry("slack", "slack", duration=15, persist=True),
        AppEntry("clock", "clock", duration=15),
    ]
    nvidia_path = os.path.join(_IMAGES_DIR, "nvidia.png")
    if os.path.exists(nvidia_path):
        entries.append(
            AppEntry(
                "image-nvidia",
                "image",
                duration=10,
                params={"image_path": nvidia_path},
            )
        )
    return entries


def default_config_path() -> str:
    """Resolve the config file path (env override, else repo-root apps.json)."""
    return os.environ.get("MATRIXOS_CONFIG", os.path.join(_REPO_ROOT, "apps.json"))


class AppConfigStore:
    """Thread-safe loader/saver for the app configuration file."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_config_path()
        self._lock = threading.Lock()
        self._entries: List[AppEntry] = []
        self.load()

    def load(self) -> None:
        """Load entries from disk, seeding defaults if the file is absent."""
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                    self._entries = [AppEntry.from_dict(e) for e in raw.get("apps", [])]
                    return
                except Exception as exc:  # noqa: BLE001 - fall back to defaults
                    log.warning("Failed to read %s (%s); using defaults", self.path, exc)
            self._entries = _default_entries()
            self._save_locked()

    def _save_locked(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"apps": [e.to_dict() for e in self._entries]}, fh, indent=2)
        os.replace(tmp, self.path)

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def list(self) -> List[AppEntry]:
        with self._lock:
            return [AppEntry.from_dict(e.to_dict()) for e in self._entries]

    def get(self, app_id: str) -> Optional[AppEntry]:
        with self._lock:
            for e in self._entries:
                if e.id == app_id:
                    return AppEntry.from_dict(e.to_dict())
        return None

    def add(self, entry: AppEntry) -> None:
        with self._lock:
            if any(e.id == entry.id for e in self._entries):
                raise ValueError(f"App id '{entry.id}' already exists")
            self._entries.append(entry)
            self._save_locked()

    def update(self, entry: AppEntry) -> None:
        with self._lock:
            for i, e in enumerate(self._entries):
                if e.id == entry.id:
                    self._entries[i] = entry
                    self._save_locked()
                    return
            raise KeyError(entry.id)

    def remove(self, app_id: str) -> None:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.id != app_id]
            if len(self._entries) != before:
                self._save_locked()
