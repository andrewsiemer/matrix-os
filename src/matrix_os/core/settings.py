"""
System-level display settings for MatrixOS.

Holds the global matrix brightness and an optional nightly sleep window during
which the display is blanked. Persisted to a JSON file so changes made from the
web UI survive restarts.
"""

import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def default_settings_path() -> str:
    """Resolve the settings file path (env override, else repo-root settings.json)."""
    return os.environ.get("MATRIXOS_SETTINGS", os.path.join(_REPO_ROOT, "settings.json"))


def _normalize_time(value: str, fallback: str) -> str:
    """Validate an HH:MM string, returning ``fallback`` if malformed."""
    value = str(value).strip()
    if _TIME_RE.match(value):
        h, m = value.split(":")
        return f"{int(h):02d}:{m}"
    return fallback


def _to_minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


@dataclass
class SystemSettings:
    """Global display settings."""

    brightness: int = 100  # 0-100
    sleep_enabled: bool = False
    sleep_start: str = "22:00"  # HH:MM (local time)
    sleep_end: str = "07:00"  # HH:MM (local time)

    def normalized(self) -> "SystemSettings":
        """Return a validated copy (clamped brightness, well-formed times)."""
        return SystemSettings(
            brightness=max(0, min(100, int(self.brightness))),
            sleep_enabled=bool(self.sleep_enabled),
            sleep_start=_normalize_time(self.sleep_start, "22:00"),
            sleep_end=_normalize_time(self.sleep_end, "07:00"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SystemSettings":
        base = cls()
        return cls(
            brightness=int(data.get("brightness", base.brightness)),
            sleep_enabled=bool(data.get("sleep_enabled", base.sleep_enabled)),
            sleep_start=str(data.get("sleep_start", base.sleep_start)),
            sleep_end=str(data.get("sleep_end", base.sleep_end)),
        ).normalized()

    def is_sleeping(self, now: datetime) -> bool:
        """Whether ``now`` (local time) falls within the sleep window."""
        if not self.sleep_enabled:
            return False
        start = _to_minutes(self.sleep_start)
        end = _to_minutes(self.sleep_end)
        if start == end:
            return False
        cur = now.hour * 60 + now.minute
        if start < end:
            return start <= cur < end
        # Window wraps past midnight (e.g. 22:00 -> 07:00).
        return cur >= start or cur < end


class SystemSettingsStore:
    """Thread-safe loader/saver for system display settings."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_settings_path()
        self._lock = threading.Lock()
        self._settings = SystemSettings()
        self.load()

    def load(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        self._settings = SystemSettings.from_dict(json.load(fh))
                    return
                except Exception as exc:  # noqa: BLE001 - fall back to defaults
                    log.warning("Failed to read %s (%s); using defaults", self.path, exc)
            self._settings = SystemSettings()
            self._save_locked()

    def _save_locked(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._settings.to_dict(), fh, indent=2)
        os.replace(tmp, self.path)

    def get(self) -> SystemSettings:
        with self._lock:
            return SystemSettings.from_dict(self._settings.to_dict())

    def update(self, patch: Dict[str, Any]) -> SystemSettings:
        with self._lock:
            merged = self._settings.to_dict()
            merged.update({k: patch[k] for k in patch if k in merged})
            self._settings = SystemSettings.from_dict(merged)
            self._save_locked()
            return SystemSettings.from_dict(self._settings.to_dict())
