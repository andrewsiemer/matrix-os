import logging
from PIL import Image

log = logging.getLogger(__name__)


class ImageViewer:
    def __init__(self, offscreen_canvas, path):
        self.framerate = 100
        self.offscreen_canvas = offscreen_canvas

        image = Image.open(path)
        # Use Pillow 10+ Resampling when available, with fallbacks for older versions
        if hasattr(Image, "Resampling"):
            resample = Image.Resampling.LANCZOS
        else:
            resample = getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", 1))

        image.thumbnail((offscreen_canvas.width, offscreen_canvas.height), resample)
        self.image = image.convert("RGB")

    def get_framerate(self):
        return self.framerate

    def show(self, matrix):
        self.offscreen_canvas.SetImage(self.image)
        self.offscreen_canvas = matrix.SwapOnVSync(self.offscreen_canvas)
