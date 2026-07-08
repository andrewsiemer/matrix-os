"""
System controller facade.

Bridges the web interface and the kernel. Reads live runtime state for the
status page and applies app-configuration changes both to the persisted config
store and, live, to the running kernel (via its render-thread command queue).
"""

import logging
import time
from typing import Any, Dict, List

from ..apps.registry import APP_REGISTRY, coerce_params, get_spec
from .appconfig import AppConfigStore, AppEntry
from .settings import SystemSettingsStore

log = logging.getLogger(__name__)


class SystemController:
    """Facade used by the web layer to inspect and configure the system."""

    def __init__(
        self,
        kernel,
        store: AppConfigStore,
        shared_state,
        settings_store: SystemSettingsStore,
    ):
        self._kernel = kernel
        self._store = store
        self._shared = shared_state
        self._settings = settings_store

    # ---- status -----------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        kernel = self._kernel
        running = set(kernel.sandbox.get_running_apps())
        current = kernel.get_current_app_id()
        focus = kernel.scheduler.get_focus_requests()
        uptime = time.time() - kernel.start_time if kernel.start_time else 0.0

        apps: List[Dict[str, Any]] = []
        for entry in self._store.list():
            spec = APP_REGISTRY.get(entry.type)
            name = spec.label if spec else entry.type
            instance = kernel.app_instances.get(entry.id)
            if instance is not None:
                name = instance.manifest.name
            apps.append(
                {
                    "id": entry.id,
                    "name": name,
                    "type": entry.type,
                    "enabled": entry.enabled,
                    "running": entry.id in running,
                    "current": entry.id == current,
                    "duration": entry.duration,
                    "persist": entry.persist,
                    "wants_focus": entry.id in focus,
                }
            )

        return {
            "mode": "simulation" if kernel.display.is_simulation else "hardware",
            "uptime": uptime,
            "target_fps": self._shared.target_fps,
            "display": {
                "width": kernel.display.width,
                "height": kernel.display.height,
            },
            "current_app": current,
            "app_count": len(kernel.app_instances),
            "running_count": len(running),
            "brightness": kernel.brightness,
            "sleeping": kernel.is_sleeping(),
            "apps": apps,
        }

    # ---- display settings -------------------------------------------

    def get_settings(self) -> Dict[str, Any]:
        return self._settings.get().to_dict()

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        settings = self._settings.update(patch)
        self._kernel.submit_command(lambda k: k.apply_settings(settings))
        return settings.to_dict()

    # ---- config -----------------------------------------------------

    def available_types(self) -> List[Dict[str, Any]]:
        types = []
        for spec in APP_REGISTRY.values():
            types.append(
                {
                    "type": spec.type,
                    "label": spec.label,
                    "default_persist": spec.default_persist,
                    "supports_persist": spec.supports_persist,
                    "params": [
                        {
                            "name": p.name,
                            "label": p.label,
                            "kind": p.kind,
                            "optional": p.optional,
                            "default": p.default,
                        }
                        for p in spec.params
                    ],
                }
            )
        return types

    def list_config(self) -> List[Dict[str, Any]]:
        result = []
        for entry in self._store.list():
            spec = APP_REGISTRY.get(entry.type)
            d = entry.to_dict()
            d["label"] = spec.label if spec else entry.type
            d["supports_persist"] = spec.supports_persist if spec else False
            result.append(d)
        return result

    def _unique_id(self, base: str) -> str:
        existing = {e.id for e in self._store.list()}
        if base not in existing:
            return base
        i = 2
        while f"{base}-{i}" in existing:
            i += 1
        return f"{base}-{i}"

    def add_app(self, data: Dict[str, Any]) -> Dict[str, Any]:
        app_type = data.get("type")
        if not app_type or app_type not in APP_REGISTRY:
            raise ValueError(f"Unknown app type '{app_type}'")

        spec = get_spec(app_type)
        app_id = (data.get("id") or "").strip() or self._unique_id(app_type)
        # Only apps that support it may persist; ignore the flag otherwise.
        persist = bool(data.get("persist", spec.default_persist)) and spec.supports_persist
        entry = AppEntry(
            id=app_id,
            type=app_type,
            enabled=bool(data.get("enabled", True)),
            duration=float(data.get("duration", 15.0)),
            persist=persist,
            params=coerce_params(app_type, data.get("params", {})),
        )
        self._store.add(entry)  # raises ValueError on duplicate id
        self._kernel.submit_command(lambda k: k.apply_app_config(entry))
        return entry.to_dict()

    def update_app(self, app_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        old = self._store.get(app_id)
        if old is None:
            raise KeyError(app_id)

        app_type = patch.get("type", old.type)
        spec = APP_REGISTRY.get(app_type)
        supports_persist = spec.supports_persist if spec else False
        persist = bool(patch.get("persist", old.persist)) and supports_persist
        entry = AppEntry(
            id=old.id,
            type=app_type,
            enabled=bool(patch.get("enabled", old.enabled)),
            duration=float(patch.get("duration", old.duration)),
            persist=persist,
            params=coerce_params(
                app_type,
                patch["params"] if "params" in patch else old.params,
            ),
        )
        self._store.update(entry)

        structural = (
            old.enabled != entry.enabled or old.type != entry.type or old.params != entry.params
        )
        if structural:
            self._kernel.submit_command(lambda k: k.apply_app_config(entry))
        elif entry.enabled:
            # Only rotation settings changed: update in place, no restart.
            self._kernel.submit_command(
                lambda k: k.scheduler.update_app(
                    entry.id, duration=entry.duration, can_persist=entry.persist
                )
            )
        return entry.to_dict()

    def remove_app(self, app_id: str) -> None:
        if self._store.get(app_id) is None:
            raise KeyError(app_id)
        self._store.remove(app_id)
        self._kernel.submit_command(lambda k: k.remove_app_live(app_id))
