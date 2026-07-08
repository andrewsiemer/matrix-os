"""Tests for system display settings: sleep window logic and persistence."""

from datetime import datetime

from matrix_os.core.settings import SystemSettings, SystemSettingsStore


def _at(hour, minute=0):
    return datetime(2026, 1, 1, hour, minute)


def test_brightness_is_clamped():
    assert SystemSettings(brightness=150).normalized().brightness == 100
    assert SystemSettings(brightness=-5).normalized().brightness == 0


def test_malformed_time_falls_back():
    s = SystemSettings(sleep_start="99:99", sleep_end="7:05").normalized()
    assert s.sleep_start == "22:00"  # malformed -> default
    assert s.sleep_end == "07:05"  # single-digit hour normalized


def test_sleep_disabled_never_sleeps():
    s = SystemSettings(sleep_enabled=False, sleep_start="22:00", sleep_end="07:00")
    assert s.is_sleeping(_at(23)) is False


def test_daytime_window():
    s = SystemSettings(sleep_enabled=True, sleep_start="09:00", sleep_end="17:00")
    assert s.is_sleeping(_at(8, 59)) is False
    assert s.is_sleeping(_at(9, 0)) is True
    assert s.is_sleeping(_at(12)) is True
    assert s.is_sleeping(_at(17, 0)) is False  # end is exclusive


def test_window_wrapping_midnight():
    s = SystemSettings(sleep_enabled=True, sleep_start="22:00", sleep_end="07:00")
    assert s.is_sleeping(_at(23)) is True
    assert s.is_sleeping(_at(2)) is True
    assert s.is_sleeping(_at(6, 59)) is True
    assert s.is_sleeping(_at(7, 0)) is False
    assert s.is_sleeping(_at(12)) is False


def test_equal_start_end_never_sleeps():
    s = SystemSettings(sleep_enabled=True, sleep_start="08:00", sleep_end="08:00")
    assert s.is_sleeping(_at(8)) is False


def test_store_roundtrip_and_clamp(tmp_path):
    path = str(tmp_path / "settings.json")
    store = SystemSettingsStore(path=path)
    out = store.update({"brightness": 250, "sleep_enabled": True, "sleep_start": "23:30"})
    assert out.brightness == 100
    assert out.sleep_enabled is True
    assert out.sleep_start == "23:30"

    # Reload from disk preserves values.
    assert SystemSettingsStore(path=path).get().sleep_start == "23:30"
