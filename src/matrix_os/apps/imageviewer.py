"""
Image Viewer App

Displays a static image on the matrix.
"""

import os
from typing import Optional

from PIL import Image

from .base import BaseApp, AppManifest, Capability
from ..core.display import FrameBuffer


class ImageViewerApp(BaseApp):
    """Static image display."""
    
    @classmethod
    def get_manifest(cls) -> AppManifest:
        return AppManifest(
            name="Image Viewer",
            version="1.0.0",
            description="Display static images",
            framerate=1,
            capabilities={Capability.FILESYSTEM},
        )
    
    def __init__(self, *args, image_path: str = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._image_path = image_path
        self._image: Optional[Image.Image] = None
    
    def on_start(self) -> None:
        """Load the image."""
        if self._image_path and os.path.exists(self._image_path):
            self._image = self.load_image(self._image_path, (self.width, self.height))
    
    def update(self) -> None:
        """Nothing to update for static image."""
        pass
    
    def render(self) -> Optional[FrameBuffer]:
        """Render the image."""
        self.fb.clear()
        
        if self._image:
            self.fb.blit(self._image)
        
        return self.fb

