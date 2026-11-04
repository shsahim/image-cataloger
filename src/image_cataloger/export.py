"""Write the catalog as CSV and JSON."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile

from .db import CatalogDB

COLUMNS = [
    "path", "kind", "summary", "width", "height", "duration_s", "bytes", "md5", "sha256",
    "duplicate_of", "status", "error", "model",
]


def rows_for_export(db: CatalogDB) -> list[dict]:
    rows = db.all_rows()
    by_path: dict[str, sqlite3.Row] = {r["path"]: r for r in rows}
    out: list[dict] = []
    for r in rows:
        summary = r["summary"]
        if r["status"] == "duplicate" and r["duplicate_of"] in by_path:
            summary = by_path[r["duplicate_of"]]["summary"]
        out.append(
            {
                "path": r["path"],
                "kind": r["kind"] or "image",
                "summary": summary or "",
                "width": r["width"],
                "height": r["height"],
                "duration_s": (round(r["duration"], 2) if r["duration"] is not None else None),
                "bytes": r["size"],
                "md5": r["md5"],
                "sha256": r["sha256"],
                "duplicate_of": r["duplicate_of"] or "",
                "status": r["status"],
                "error": r["error"] or "",
                "model": r["model"] or "",
            }
        )
    return out


def _atomic_write(path: str, write_fn) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            write_fn(f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export_csv(db: CatalogDB, path: str) -> int:
    rows = rows_for_export(db)

    def write(f):
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    _atomic_write(path, write)
    return len(rows)


def export_json(db: CatalogDB, path: str) -> int:
    rows = rows_for_export(db)
    _atomic_write(path, lambda f: json.dump(rows, f, indent=1, ensure_ascii=False))
    return len(rows)


DUPLICATE_COLUMNS = ["canonical", "duplicate", "bytes", "sha256", "summary"]


def duplicate_groups(db: CatalogDB) -> list[dict]:
    """Rows for the duplicates-only CSV: one per duplicate file, grouped by canonical."""
    rows = db.all_rows()
    by_path: dict[str, sqlite3.Row] = {r["path"]: r for r in rows}
    out: list[dict] = []
    for r in rows:
        if r["status"] != "duplicate" or not r["duplicate_of"]:
            continue
        canon = by_path.get(r["duplicate_of"])
        out.append(
            {
                "canonical": r["duplicate_of"],
                "duplicate": r["path"],
                "bytes": r["size"],
                "sha256": r["sha256"],
                "summary": (canon["summary"] if canon is not None else None) or "",
            }
        )
    out.sort(key=lambda d: (d["canonical"], d["duplicate"]))
    return out


def export_duplicates_csv(db: CatalogDB, path: str) -> int:
    rows = duplicate_groups(db)

    def write(f):
        w = csv.DictWriter(f, fieldnames=DUPLICATE_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    _atomic_write(path, write)
    return len(rows)


def duplicate_summary(db: CatalogDB) -> dict:
    """Counts for the log: duplicate files, groups they belong to, and reclaimable bytes."""
    rows = duplicate_groups(db)
    return {
        "duplicates": len(rows),
        "groups": len({r["canonical"] for r in rows}),
        "bytes": sum(int(r["bytes"] or 0) for r in rows),
    }
