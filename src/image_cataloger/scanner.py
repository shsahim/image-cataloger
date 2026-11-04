"""Find image files under a folder (local or network-mounted)."""
from __future__ import annotations

import os
from pathlib import Path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}

# Directories that hold thumbnails or metadata, never user images.
SKIP_DIR_NAMES = {"@eaDir", "#recycle", ".Trashes", ".Spotlight-V100", "__MACOSX", "_notes"}


def is_image_file(name: str) -> bool:
    if name.startswith(".") or name.startswith("._"):
        return False
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS


def scan_folder(folder: str | os.PathLike, recursive: bool = True) -> list[str]:
    """Return sorted absolute paths of image files under *folder*.

    Hidden files, AppleDouble resource forks and NAS metadata folders are skipped.
    Errors on unreadable subdirectories are ignored so a flaky share never aborts a scan.
    """
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    found: list[str] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and d not in SKIP_DIR_NAMES
            )
            for name in filenames:
                if is_image_file(name):
                    found.append(os.path.join(dirpath, name))
    else:
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False) and is_image_file(entry.name):
                        found.append(entry.path)
        except OSError:
            pass
    found.sort()
    return found
