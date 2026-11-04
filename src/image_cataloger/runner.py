"""Orchestrates a cataloging run: scan -> hash/dedupe -> describe in parallel -> export.

Everything is resumable: state lives in the SQLite DB, every batch result is
committed as soon as it arrives, and a new run only sends files still pending.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .config import RunConfig
from .db import CatalogDB
from .engines.base import DescribeResult, Engine, RunControl
from .export import duplicate_summary, export_csv, export_duplicates_csv, export_json
from .hashing import assign_duplicates, hash_file
from .imaging import UnreadableImage, read_dimensions
from .scanner import scan_folder

log = logging.getLogger(__name__)


@dataclass
class Progress:
    phase: str = "idle"  # scanning | hashing | describing | exporting | done | stopped | failed
    total: int = 0
    done: int = 0  # ok + duplicate + unreadable + error
    ok: int = 0
    duplicates: int = 0
    unreadable: int = 0
    errors: int = 0
    pending: int = 0
    described_this_run: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    rate_per_min: float = 0.0
    eta_seconds: float | None = None
    elapsed_seconds: float = 0.0
    message: str = ""
    extra: dict = field(default_factory=dict)


class CatalogRunner:
    def __init__(
        self,
        config: RunConfig,
        engine: Engine | None,
        control: RunControl | None = None,
        on_progress: Callable[[Progress], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.engine = engine
        self.control = control or RunControl()
        self.on_progress = on_progress or (lambda p: None)
        self.on_log = on_log or (lambda s: log.info(s))
        self.db: CatalogDB | None = None
        self.progress = Progress()
        self._lock = threading.Lock()
        self._describe_started: float | None = None
        self.fatal_error: str | None = None

    # ---- public -----------------------------------------------------------
    def run(self) -> Progress:
        cfg = self.config
        cfg.resolve_paths()
        self.db = CatalogDB(cfg.db_path)
        self.db.set_meta("folder", os.path.abspath(cfg.folder))
        started = time.time()
        try:
            self._set_phase("scanning", f"Scanning {cfg.folder} ...")
            paths = scan_folder(cfg.folder, cfg.recursive)
            self._log(f"Found {len(paths)} image file(s).")
            removed = self.db.delete_missing(paths)
            if removed:
                self._log(f"Dropped {removed} row(s) for files no longer present.")
            if self.control.stopped:
                return self._finish("stopped", started)

            self._set_phase("hashing", "Hashing files and reading dimensions ...")
            self._hash_phase(paths)
            if self.control.stopped:
                return self._finish("stopped", started)

            dups = self._dedupe_phase()
            retried = self.db.reset_errors()
            if retried:
                self._log(f"Re-trying {retried} file(s) that errored last time.")
            pending = self.db.pending_paths()
            self._log(
                f"{len(pending)} to describe, {dups} duplicate(s) linked, "
                f"{self.db.stats()['unreadable']} unreadable, "
                f"{self.db.stats()['ok']} already done."
            )
            self._refresh_counts()

            if cfg.dedupe_only:
                self._log("Dedupe-only mode: skipping descriptions (no model calls).")
                return self._finish("done", started)

            self._set_phase("describing", f"Describing with {self.engine.name} / {self.engine.model} ...")
            self._describe_phase(pending)
            return self._finish("stopped" if self.control.stopped else "done", started)
        except Exception as e:  # noqa: BLE001 - surface anything to the UI
            self.fatal_error = f"{type(e).__name__}: {e}"
            self._log(f"FAILED: {self.fatal_error}")
            try:
                self._export()
            except Exception as ex:  # noqa: BLE001
                self._log(f"Export failed: {ex}")
            self._set_phase("failed", self.fatal_error)
            return self.progress
        finally:
            if self.db:
                self.db.close()

    # ---- phases -----------------------------------------------------------
    def _hash_phase(self, paths: list[str]) -> None:
        db = self.db
        assert db is not None
        existing = db.get_many(paths)
        to_hash: list[str] = []
        for p in paths:
            row = existing.get(p)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if (
                row is not None
                and row["sha256"]
                and row["size"] == st.st_size
                and abs((row["mtime"] or 0) - st.st_mtime) < 1e-6
            ):
                continue
            to_hash.append(p)
        self._log(f"{len(to_hash)} file(s) to hash ({len(paths) - len(to_hash)} already known).")
        with self._lock:
            self.progress.total = len(paths)
            self.progress.done = 0
            self.progress.extra["hash_total"] = len(to_hash)
            self.progress.extra["hashed"] = 0
        self._emit()

        def work(p: str) -> None:
            if self.control.stopped:
                return
            self.control.wait_if_paused()
            try:
                st = os.stat(p)
                md5, sha = hash_file(p)
            except OSError as e:
                db.upsert_hashed(p, 0, 0.0, "", "", None, None, "error", f"read failed: {e}")
                return
            try:
                w, h = read_dimensions(p)
                db.upsert_hashed(p, st.st_size, st.st_mtime, md5, sha, w, h, "pending")
            except UnreadableImage as e:
                db.upsert_hashed(p, st.st_size, st.st_mtime, md5, sha, None, None, "unreadable", str(e))
            with self._lock:
                self.progress.extra["hashed"] += 1
                n = self.progress.extra["hashed"]
            if n % 25 == 0 or n == len(to_hash):
                self.progress.message = f"Hashed {n}/{len(to_hash)}"
                self._emit()

        workers = max(1, min(self.config.workers * 2, 16))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hash") as pool:
            list(pool.map(work, to_hash))

    def _dedupe_phase(self) -> int:
        db = self.db
        assert db is not None
        rows = db.hashed_rows()
        preferred = [r["path"] for r in rows if r["status"] == "ok"]
        mapping = assign_duplicates(
            [(r["path"], r["sha256"] or None) for r in rows if r["status"] != "unreadable"],
            preferred=preferred,
        )
        count = 0
        for r in rows:
            canonical = mapping.get(r["path"])
            if canonical:
                count += 1
                if r["status"] != "duplicate" or r["duplicate_of"] != canonical:
                    if r["status"] != "ok":  # never discard a summary we already paid for
                        db.mark_duplicate(r["path"], canonical)
            elif r["status"] == "duplicate":
                db.clear_duplicate(r["path"])
        return count

    def _describe_phase(self, pending: list[str]) -> None:
        if not pending:
            return
        size = max(1, self.config.batch_size)
        batches: queue.Queue[list[str]] = queue.Queue()
        for i in range(0, len(pending), size):
            batches.put(pending[i : i + size])
        self._describe_started = time.time()
        self._emit()

        def worker() -> None:
            while not self.control.stopped:
                if not self.control.wait_if_paused():
                    return
                try:
                    batch = batches.get_nowait()
                except queue.Empty:
                    return
                try:
                    assert self.engine is not None
                    results = self.engine.describe(batch, self.control)
                except Exception as e:  # noqa: BLE001
                    fatal = type(e).__name__ == "FatalEngineError"
                    msg = f"{type(e).__name__}: {e}"[:300]
                    results = [DescribeResult(p, status="error", error=msg) for p in batch]
                    if fatal and not self.fatal_error:
                        self.fatal_error = msg
                        self._log(f"FATAL: {msg} - stopping run.")
                        self.control.stop()
                self._store(results)

        threads = [
            threading.Thread(target=worker, name=f"describe-{i}", daemon=True)
            for i in range(max(1, self.config.workers))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # ---- bookkeeping ------------------------------------------------------
    def _store(self, results: list[DescribeResult]) -> None:
        db = self.db
        assert db is not None
        rows = []
        for r in results:
            if r.status == "error" and r.error == "stopped":
                continue  # leave pending so the next run picks it up
            rows.append((r.path, r.status, r.summary, r.error, getattr(self.engine, "model", None),
                         r.input_tokens, r.output_tokens, r.cost_usd))
            if r.status == "error":
                self._log(f"error: {os.path.basename(r.path)}: {r.error}")
        if rows:
            db.set_results(rows)
        with self._lock:
            self.progress.described_this_run += sum(1 for r in results if r.status == "ok")
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        db = self.db
        assert db is not None
        s = db.stats()
        with self._lock:
            p = self.progress
            p.total = s["total"]
            p.ok, p.duplicates, p.unreadable, p.errors, p.pending = (
                s["ok"], s["duplicate"], s["unreadable"], s["error"], s["pending"])
            p.done = p.ok + p.duplicates + p.unreadable + p.errors
            p.input_tokens, p.output_tokens, p.cost_usd = s["input_tokens"], s["output_tokens"], s["cost_usd"]
            if self._describe_started:
                elapsed = max(1e-6, time.time() - self._describe_started)
                p.rate_per_min = p.described_this_run / elapsed * 60
                p.eta_seconds = (p.pending / p.rate_per_min * 60) if p.rate_per_min > 0 else None
        self._emit()

    def _export(self) -> None:
        db = self.db
        assert db is not None
        n = export_csv(db, self.config.output_csv)
        export_json(db, self.config.output_json)
        self._log(f"Wrote {n} row(s) to {self.config.output_csv} and {self.config.output_json}")
        d = duplicate_summary(db)
        with self._lock:
            self.progress.extra["duplicate_groups"] = d["groups"]
            self.progress.extra["duplicate_bytes"] = d["bytes"]
        if d["duplicates"] or self.config.dedupe_only:
            export_duplicates_csv(db, self.config.output_duplicates_csv)
            self._log(
                f"{d['duplicates']} duplicate file(s) in {d['groups']} group(s), "
                f"{_human_bytes(d['bytes'])} reclaimable; listed in {self.config.output_duplicates_csv}"
            )

    def _finish(self, phase: str, started: float) -> Progress:
        self._set_phase("exporting", "Writing CSV and JSON ...")
        self._export()
        self._refresh_counts()
        with self._lock:
            self.progress.elapsed_seconds = time.time() - started
        self._set_phase(phase, "Done." if phase == "done" else "Stopped; progress saved.")
        return self.progress

    def _set_phase(self, phase: str, message: str) -> None:
        with self._lock:
            self.progress.phase = phase
            self.progress.message = message
        self._log(message)
        self._emit()

    def _log(self, text: str) -> None:
        self.on_log(text)

    def _emit(self) -> None:
        with self._lock:
            snapshot = Progress(**{**self.progress.__dict__, "extra": dict(self.progress.extra)})
        self.on_progress(snapshot)


def build_engine(cfg: RunConfig) -> Engine:
    if cfg.engine == "cli":
        from .engines.cli_engine import CliEngine

        return CliEngine(root=os.path.abspath(cfg.folder), model=cfg.model)
    from .engines.api_engine import ApiEngine

    return ApiEngine(
        api_key=cfg.api_key,
        model=cfg.model,
        max_side=cfg.max_side,
        jpeg_quality=cfg.jpeg_quality,
        effort=cfg.effort,
        timeout=cfg.request_timeout,
        max_attempts=cfg.max_attempts,
    )


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
