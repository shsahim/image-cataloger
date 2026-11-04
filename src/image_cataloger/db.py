"""SQLite store that makes runs resumable.

One row per file. A file is only ever described once: on a later run the scan
sees its row with status 'ok' and skips it. Status values:

    pending    - hashed, waiting to be described
    ok         - described successfully
    duplicate  - byte-identical to another file (see duplicate_of); not sent
    unreadable - Pillow could not decode it; not sent
    error      - the engine failed after retries; re-tried on the next run
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Iterable

STATUSES = ("pending", "ok", "duplicate", "unreadable", "error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'image',
    size          INTEGER,
    mtime         REAL,
    duration      REAL,
    md5           TEXT,
    sha256        TEXT,
    width         INTEGER,
    height        INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending',
    summary       TEXT,
    duplicate_of  TEXT,
    error         TEXT,
    model         TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class CatalogDB:
    def __init__(self, path: str | os.PathLike):
        self.path = str(path)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release to databases created before them."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(files)")}
        if "kind" not in cols:
            self._conn.execute("ALTER TABLE files ADD COLUMN kind TEXT NOT NULL DEFAULT 'image'")
        if "duration" not in cols:
            self._conn.execute("ALTER TABLE files ADD COLUMN duration REAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- meta -------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # ---- files ------------------------------------------------------------
    def get(self, path: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()

    def get_many(self, paths: Iterable[str]) -> dict[str, sqlite3.Row]:
        out: dict[str, sqlite3.Row] = {}
        paths = list(paths)
        with self._lock:
            for i in range(0, len(paths), 500):
                chunk = paths[i : i + 500]
                q = f"SELECT * FROM files WHERE path IN ({','.join('?' * len(chunk))})"
                for row in self._conn.execute(q, chunk):
                    out[row["path"]] = row
        return out

    def upsert_hashed(
        self,
        path: str,
        size: int,
        mtime: float,
        md5: str,
        sha256: str,
        width: int | None,
        height: int | None,
        status: str,
        error: str | None = None,
        kind: str = "image",
        duration: float | None = None,
    ) -> None:
        """Insert a freshly hashed file, or reset a row whose file changed on disk."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO files(path,kind,size,mtime,duration,md5,sha256,width,height,status,error,
                                     summary,duplicate_of,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)
                   ON CONFLICT(path) DO UPDATE SET
                     kind=excluded.kind, size=excluded.size, mtime=excluded.mtime,
                     duration=excluded.duration, md5=excluded.md5,
                     sha256=excluded.sha256, width=excluded.width, height=excluded.height,
                     status=excluded.status, error=excluded.error, summary=NULL,
                     duplicate_of=NULL, updated_at=excluded.updated_at""",
                (path, kind, size, mtime, duration, md5, sha256, width, height, status, error,
                 time.time()),
            )
            self._conn.commit()

    def set_result(
        self,
        path: str,
        status: str,
        summary: str | None,
        error: str | None,
        model: str | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        assert status in STATUSES
        with self._lock:
            self._conn.execute(
                """UPDATE files SET status=?, summary=?, error=?, model=?,
                       input_tokens=input_tokens+?, output_tokens=output_tokens+?,
                       cost_usd=cost_usd+?, updated_at=?
                   WHERE path=?""",
                (status, summary, error, model, input_tokens, output_tokens, cost, time.time(), path),
            )
            self._conn.commit()

    def set_results(self, rows: list[tuple]) -> None:
        """Batch form of set_result; rows are (path, status, summary, error, model, in, out, cost)."""
        now = time.time()
        with self._lock:
            self._conn.executemany(
                """UPDATE files SET status=?, summary=?, error=?, model=?,
                       input_tokens=input_tokens+?, output_tokens=output_tokens+?,
                       cost_usd=cost_usd+?, updated_at=?
                   WHERE path=?""",
                [(s, su, e, m, i, o, c, now, p) for (p, s, su, e, m, i, o, c) in rows],
            )
            self._conn.commit()

    def mark_duplicate(self, path: str, canonical: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE files SET status='duplicate', duplicate_of=?, summary=NULL, error=NULL, "
                "updated_at=? WHERE path=?",
                (canonical, time.time(), path),
            )
            self._conn.commit()

    def clear_duplicate(self, path: str) -> None:
        """A file previously marked duplicate whose twin vanished goes back to pending."""
        with self._lock:
            self._conn.execute(
                "UPDATE files SET status='pending', duplicate_of=NULL, updated_at=? "
                "WHERE path=? AND status='duplicate'",
                (time.time(), path),
            )
            self._conn.commit()

    def reset_errors(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE files SET status='pending', error=NULL WHERE status='error'"
            )
            self._conn.commit()
            return cur.rowcount

    def pending_paths(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM files WHERE status='pending' ORDER BY path"
            ).fetchall()
        return [r["path"] for r in rows]

    def hashed_rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT path, sha256, status, duplicate_of FROM files ORDER BY path"
            ).fetchall()

    def all_rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM files ORDER BY path").fetchall()

    def delete_missing(self, present: Iterable[str]) -> int:
        """Remove rows for files that are no longer under the scanned folder."""
        present_set = set(present)
        with self._lock:
            rows = self._conn.execute("SELECT path FROM files").fetchall()
            gone = [r["path"] for r in rows if r["path"] not in present_set]
            for i in range(0, len(gone), 500):
                chunk = gone[i : i + 500]
                self._conn.execute(
                    f"DELETE FROM files WHERE path IN ({','.join('?' * len(chunk))})", chunk
                )
            self._conn.commit()
        return len(gone)

    def stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(input_tokens),0) AS i, "
                "COALESCE(SUM(output_tokens),0) AS o, COALESCE(SUM(cost_usd),0) AS c "
                "FROM files GROUP BY status"
            ).fetchall()
        out = {s: 0 for s in STATUSES}
        out.update({"total": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        for r in rows:
            out[r["status"]] = r["n"]
            out["total"] += r["n"]
            out["input_tokens"] += r["i"]
            out["output_tokens"] += r["o"]
            out["cost_usd"] += r["c"]
        return out
