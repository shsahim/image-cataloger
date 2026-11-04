import io

import pytest
from PIL import Image

from image_cataloger.imaging import UnreadableImage, estimate_image_tokens, prepare_image, read_dimensions


def test_prepare_downscales_long_side(make_image):
    p = make_image("wide.png", size=(4000, 1000))
    out = prepare_image(p, max_side=1568, quality=85)
    assert out.media_type == "image/jpeg"
    assert out.width == 1568 and out.height == 392
    assert (out.source_width, out.source_height) == (4000, 1000)
    im = Image.open(io.BytesIO(out.data))
    assert im.format == "JPEG" and im.size == (1568, 392)


def test_prepare_keeps_small_images_unscaled(make_image):
    p = make_image("small.jpg", size=(300, 200))
    out = prepare_image(p)
    assert (out.width, out.height) == (300, 200)


def test_prepare_flattens_transparency(make_image):
    p = make_image("alpha.png", size=(20, 20), color=(0, 0, 0, 0), mode="RGBA")
    out = prepare_image(p)
    im = Image.open(io.BytesIO(out.data)).convert("RGB")
    assert im.getpixel((5, 5)) == (255, 255, 255)


def test_prepare_applies_exif_orientation(tmp_path):
    p = tmp_path / "rot.jpg"
    im = Image.new("RGB", (100, 50), (10, 20, 30))
    exif = im.getexif()
    exif[0x0112] = 6  # rotate 90 CW
    im.save(p, exif=exif.tobytes())
    out = prepare_image(str(p))
    assert (out.width, out.height) == (50, 100)


def test_gif_first_frame(tmp_path):
    p = tmp_path / "anim.gif"
    frames = [Image.new("P", (30, 30), i) for i in (1, 2, 3)]
    frames[0].save(p, save_all=True, append_images=frames[1:])
    out = prepare_image(str(p))
    assert (out.width, out.height) == (30, 30)


def test_unreadable_raises(tmp_path):
    p = tmp_path / "junk.jpg"
    p.write_bytes(b"this is not an image at all")
    with pytest.raises(UnreadableImage):
        read_dimensions(p)
    with pytest.raises(UnreadableImage):
        prepare_image(p)


def test_truncated_jpeg_is_unreadable(make_image):
    p = make_image("ok.jpg", size=(500, 500))
    data = open(p, "rb").read()
    open(p, "wb").write(data[: len(data) // 3])
    with pytest.raises(UnreadableImage):
        prepare_image(p)


def test_token_estimate():
    assert estimate_image_tokens(1568, 1176) == int(1568 * 1176 / 750)
    assert estimate_image_tokens(1, 1) == 1
