"""Tests for the app scheduler, focused on rotation and persist/focus holds."""

import time

from matrix_os.core.config import SchedulerConfig
from matrix_os.core.scheduler import AppScheduler


def _make_scheduler():
    sched = AppScheduler(SchedulerConfig())
    sched.add_app("a", duration=0.05)
    sched.add_app("b", duration=0.05)
    sched.add_app("slack", duration=0.05, can_persist=True)
    return sched


def test_round_robin_rotation():
    sched = _make_scheduler()
    assert sched.get_current_app() == "a"

    # Force the rotation timer to expire and tick.
    sched._current_app_start = time.time() - 1.0
    sched.tick()
    assert sched.get_current_app() == "b"


def test_focus_pins_and_suppresses_rotation():
    sched = _make_scheduler()

    # Slack requests focus and is persist-enabled -> it should grab the display.
    sched.set_focus("slack", True)
    sched.tick()
    assert sched.get_current_app() == "slack"

    # Even after its duration elapses, rotation is suppressed while focused.
    sched._current_app_start = time.time() - 1.0
    sched.tick()
    assert sched.get_current_app() == "slack"

    # Releasing focus resumes normal rotation.
    sched.set_focus("slack", False)
    sched._current_app_start = time.time() - 1.0
    sched.tick()
    assert sched.get_current_app() != "slack"


def test_focus_ignored_without_persist():
    sched = _make_scheduler()

    # 'a' has can_persist=False, so its focus request must be ignored.
    sched.set_focus("a", True)
    sched.set_focus("b", True)  # also non-persist
    sched._current_app_start = time.time() - 1.0
    sched.tick()
    # Rotation proceeds normally (b), no pin.
    assert sched.get_current_app() == "b"


def test_update_app_changes_persist():
    sched = _make_scheduler()

    # Give 'a' persist ability at runtime, then it can hold focus.
    assert sched.update_app("a", can_persist=True)
    sched.set_focus("a", True)
    sched.tick()
    assert sched.get_current_app() == "a"


def test_remove_app_clears_focus():
    sched = _make_scheduler()
    sched.set_focus("slack", True)
    sched.tick()
    assert sched.get_current_app() == "slack"

    sched.remove_app("slack")
    assert "slack" not in sched.get_focus_requests()
    assert sched.get_current_app() != "slack"
