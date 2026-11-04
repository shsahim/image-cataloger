"""Content hashing and byte-identical duplicate detection."""
from __future__ import annotations

import hashlib
import os
from typing import Iterable


def hash_file(path: str | os.PathLike, chunk_size: int = 1 << 20) -> tuple[str, str]:
    """Return (md5, sha256) hex digests computed in a single pass over the file."""
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            md5.update(block)
            sha.update(block)
    return md5.hexdigest(), sha.hexdigest()


def assign_duplicates(
    items: Iterable[tuple[str, str | None]], preferred: Iterable[str] = ()
) -> dict[str, str | None]:
    """Map each path to the canonical path it duplicates, or None if it is canonical.

    *items* is an iterable of (path, sha256). Within a group of identical hashes the
    canonical file is the first one listed in *preferred* (e.g. files already
    described in an earlier run), otherwise the one with the shortest file name and
    then the lexicographically smallest path ("IMG_1.jpg" beats "IMG_1 copy.jpg"),
    so the choice is stable across runs. Items with a None hash are never grouped.
    """
    preferred_set = set(preferred)
    groups: dict[str, list[str]] = {}
    result: dict[str, str | None] = {}
    for path, digest in items:
        result[path] = None
        if digest:
            groups.setdefault(digest, []).append(path)
    for paths in groups.values():
        if len(paths) < 2:
            continue
        ordered = sorted(paths, key=lambda p: (len(os.path.basename(p)), p))
        canonical = next((p for p in ordered if p in preferred_set), ordered[0])
        for p in ordered:
            if p != canonical:
                result[p] = canonical
    return result
