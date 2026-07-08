"""Tests for the generic ScrollingText marquee utility."""

from matrix_os.apps.scroll import ScrollingText


def test_text_that_fits_is_centered_and_static():
    s = ScrollingText(view_width=64, speed=2)
    x = s.x(20)  # narrower than the view
    assert s.scrolling is False
    assert x == (64 - 20) // 2
    s.tick()  # should not move
    assert s.x(20) == (64 - 20) // 2


def test_wide_text_scrolls_left_by_speed():
    s = ScrollingText(view_width=64, speed=2)
    s.x(100)  # wider than the view -> scrolling
    assert s.scrolling is True
    start = s.offset
    s.tick()
    assert s.offset == start - 2


def test_scroll_wraps_around():
    s = ScrollingText(view_width=64, speed=5)
    s.x(100)
    # Push the offset past the left edge; it should wrap back to the right.
    for _ in range(200):
        s.tick()
    assert -100 <= s.offset <= 64


def test_reset_returns_to_right_edge():
    s = ScrollingText(view_width=64, speed=2)
    s.x(100)
    s.tick()
    s.reset()
    assert s.offset == 64
