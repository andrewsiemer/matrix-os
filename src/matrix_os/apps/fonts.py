"""
Font utilities for MatrixOS apps.

Handles loading BDF fonts for use with PIL.
"""

import logging
import os
import tempfile

from PIL import BdfFontFile, ImageFont

log = logging.getLogger(__name__)

# Cache for loaded fonts
_font_cache = {}

# Cache directory for converted fonts
_cache_dir = None


def _get_cache_dir() -> str:
    """Get or create a cache directory for converted fonts."""
    global _cache_dir
    if _cache_dir is None:
        # Use a subdirectory in temp that persists for this session
        _cache_dir = os.path.join(tempfile.gettempdir(), "matrixos_fonts")
        os.makedirs(_cache_dir, exist_ok=True)
    return _cache_dir


def load_bdf_font(bdf_path: str) -> ImageFont.ImageFont:
    """
    Load a BDF font file and convert to PIL format.

    PIL requires fonts in its own format (.pil), so we convert
    BDF files on first load and cache the result.
    """
    # Check cache first
    if bdf_path in _font_cache:
        return _font_cache[bdf_path]

    # Get font name for cache files
    font_name = os.path.basename(bdf_path).replace(".bdf", "")
    cache_dir = _get_cache_dir()
    pil_path = os.path.join(cache_dir, f"{font_name}.pil")

    try:
        # If .pil version doesn't exist in cache, create it
        if not os.path.exists(pil_path):
            log.info("Converting BDF font to PIL format: %s -> %s", bdf_path, pil_path)
            with open(bdf_path, "rb") as fp:
                p = BdfFontFile.BdfFontFile(fp)
                p.save(pil_path)

        font = ImageFont.load(pil_path)
        _font_cache[bdf_path] = font
        return font

    except Exception as e:
        log.warning("Failed to load BDF font %s: %s", bdf_path, e)
        raise


def get_font(font_path: str) -> ImageFont.ImageFont:
    """
    Load a font from a BDF file.

    Args:
        font_path: Path to BDF font file

    Returns:
        Loaded font object
    """
    return load_bdf_font(font_path)
