import pytest
from PIL import Image


@pytest.fixture
def make_image(tmp_path):
    """Factory: make_image(name, size=(w,h), color, fmt) -> path."""
    def _make(name, size=(64, 48), color=(200, 30, 30), fmt=None, mode="RGB"):
        path = tmp_path / name
        im = Image.new(mode, size, color)
        im.save(path, format=fmt)
        return str(path)
    return _make
