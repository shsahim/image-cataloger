from image_cataloger.hashing import assign_duplicates, hash_file


def test_hash_file_md5_and_sha256(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello world")
    md5, sha = hash_file(p)
    assert md5 == "5eb63bbbe01eeed093cb22bb8f5acdc3"
    assert sha == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_hash_file_chunked_matches_whole(tmp_path):
    p = tmp_path / "big.bin"
    data = bytes(range(256)) * 5000
    p.write_bytes(data)
    assert hash_file(p, chunk_size=1000) == hash_file(p, chunk_size=1 << 20)


def test_assign_duplicates_links_to_first_sorted_path():
    items = [("/x/b.jpg", "h1"), ("/x/a.jpg", "h1"), ("/x/c.jpg", "h2"), ("/x/d.jpg", None)]
    m = assign_duplicates(items)
    assert m["/x/a.jpg"] is None
    assert m["/x/b.jpg"] == "/x/a.jpg"
    assert m["/x/c.jpg"] is None
    assert m["/x/d.jpg"] is None


def test_assign_duplicates_prefers_already_described_file():
    items = [("/x/a.jpg", "h1"), ("/x/b.jpg", "h1"), ("/x/c.jpg", "h1")]
    m = assign_duplicates(items, preferred=["/x/c.jpg"])
    assert m["/x/c.jpg"] is None
    assert m["/x/a.jpg"] == "/x/c.jpg" and m["/x/b.jpg"] == "/x/c.jpg"


def test_none_hashes_never_group():
    m = assign_duplicates([("/a", None), ("/b", None)])
    assert m == {"/a": None, "/b": None}
