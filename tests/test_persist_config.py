"""Tests that persist is a per-app capability, enforced server-side."""

from matrix_os.apps.registry import APP_REGISTRY
from matrix_os.core.appconfig import AppConfigStore
from matrix_os.core.controller import SystemController
from matrix_os.core.settings import SystemSettingsStore


class _FakeKernel:
    """Kernel stub: config mutations don't need a live render loop here."""

    def submit_command(self, fn, timeout=5.0):
        return None


class _FakeState:
    target_fps = 60


def _controller(tmp_path):
    store = AppConfigStore(path=str(tmp_path / "apps.json"))
    settings = SystemSettingsStore(path=str(tmp_path / "settings.json"))
    return SystemController(_FakeKernel(), store, _FakeState(), settings)


def test_registry_supports_persist_only_for_active_state_apps():
    assert APP_REGISTRY["slack"].supports_persist is True
    assert APP_REGISTRY["dvd"].supports_persist is False
    assert APP_REGISTRY["clock"].supports_persist is False


def test_add_ignores_persist_for_unsupported_app(tmp_path):
    entry = _controller(tmp_path).add_app({"type": "dvd", "persist": True})
    assert entry["persist"] is False


def test_add_allows_persist_for_slack(tmp_path):
    entry = _controller(tmp_path).add_app({"id": "slack2", "type": "slack", "persist": True})
    assert entry["persist"] is True


def test_update_cannot_enable_persist_on_unsupported_app(tmp_path):
    controller = _controller(tmp_path)
    controller.add_app({"id": "dvd2", "type": "dvd"})
    entry = controller.update_app("dvd2", {"persist": True})
    assert entry["persist"] is False


def test_list_config_reports_capability(tmp_path):
    apps = _controller(tmp_path).list_config()
    by_type = {a["type"]: a for a in apps}
    assert by_type["slack"]["supports_persist"] is True
    assert by_type["dvd"]["supports_persist"] is False
