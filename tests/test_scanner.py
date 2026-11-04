from image_cataloger.scanner import scan_folder


def test_scan_filters_and_sorts(tmp_path):
    (tmp_path / "b.JPG").write_bytes(b"x")
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "c.txt").write_bytes(b"x")
    (tmp_path / ".hidden.jpg").write_bytes(b"x")
    (tmp_path / "._b.JPG").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.webp").write_bytes(b"x")
    (tmp_path / "@eaDir").mkdir()
    (tmp_path / "@eaDir" / "thumb.jpg").write_bytes(b"x")

    rec = scan_folder(tmp_path, recursive=True)
    assert [p.rsplit("/", 1)[-1] for p in rec] == ["a.png", "b.JPG", "d.webp"]
    flat = scan_folder(tmp_path, recursive=False)
    assert [p.rsplit("/", 1)[-1] for p in flat] == ["a.png", "b.JPG"]
