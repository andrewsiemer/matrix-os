"""
Reusable horizontal text scroller (marquee) for MatrixOS apps.

Text that fits within the view is centered; text wider than the view scrolls
right-to-left and wraps around. Any app can use this to display overflowing
labels without reimplementing the offset bookkeeping.

Example:
    self._scroller = ScrollingText(self.width, speed=2)
    ...
    def update(self):
        self._scroller.tick()

    def render(self):
        text_width = measure(text)
        x = self._scroller.x(text_width)
        draw.text((x, y), text, ...)
"""


class ScrollingText:
    """Tracks the horizontal offset for a scrolling line of text."""

    def __init__(self, view_width: int, speed: int = 2, wrap_gap: int = 0):
        """
        Args:
            view_width: Width of the visible area in pixels.
            speed: Pixels to advance per ``tick()`` (higher = faster).
            wrap_gap: Extra blank pixels before the text repeats from the right.
        """
        self.view_width = view_width
        self.speed = max(1, int(speed))
        self.wrap_gap = max(0, int(wrap_gap))
        self.offset = view_width
        self._text_width = 0

    @property
    def scrolling(self) -> bool:
        """Whether the current text is wide enough to scroll."""
        return self._text_width > self.view_width

    def reset(self) -> None:
        """Restart the scroll from the right edge."""
        self.offset = self.view_width

    def tick(self) -> None:
        """Advance the scroll by ``speed`` pixels (call once per frame)."""
        if not self.scrolling:
            return
        self.offset -= self.speed
        if self.offset < -(self._text_width + self.wrap_gap):
            self.offset = self.view_width

    def x(self, text_width: int) -> int:
        """Record the text width and return the x to draw the text at.

        Centers text that fits; returns the scroll offset otherwise.
        """
        self._text_width = text_width
        if not self.scrolling:
            return (self.view_width - text_width) // 2
        return self.offset
