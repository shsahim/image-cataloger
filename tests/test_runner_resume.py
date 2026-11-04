"""End-to-end runner behaviour with a fake engine: dedupe, resume, stop, export."""
import csv
import json
import shutil
import threading

from image_cataloger.config import RunConfig
from image_cataloger.db import CatalogDB
from image_cataloger.engines.base import DescribeResult, RunControl
from image_cataloger.runner import CatalogRunner


class FakeEngine:
    name = "fake"
    model = "fake-model"

    def __init__(self, fail_paths=(), stop_control=None, stop_after=None):
        self.calls = []
        self.fail_paths = set(fail_paths)
        self.stop_control = stop_control
        self.stop_after = stop_after
        self.lock = threading.Lock()

    def describe(self, paths, control):
        with self.lock:
            self.calls.append(list(paths))
            n = len(self.calls)
        out = []
        for p in paths:
            if p in self.fail_paths:
                out.append(DescribeResult(p, status="error", error="boom"))
            else:
                out.append(DescribeResult(p, summary=f"desc of {p.rsplit('/', 1)[-1]}", status="ok",
                                          input_tokens=100, output_tokens=10, cost_usd=0.001))
        if self.stop_after is not None and n >= self.stop_after and self.stop_control:
            self.stop_control.stop()
        return out


def _cfg(tmp_path, folder, **kw):
    base = tmp_path / "out" / "cat"
    return RunConfig(folder=str(folder), workers=kw.pop("workers", 2), batch_size=kw.pop("batch_size", 2),
                     db_path=str(base) + ".sqlite", output_csv=str(base) + ".csv",
                     output_json=str(base) + ".json", **kw)


def _folder(tmp_path, make_image, n=5):
    folder = tmp_path / "photos"
    folder.mkdir()
    paths = []
    for i in range(n):
        p = folder / f"img{i}.png"
        shutil.copy(make_image(f"src{i}.png", size=(20 + i, 20)), p)
        paths.append(str(p))
    return folder, paths


def test_full_run_with_duplicates_and_unreadable(tmp_path, make_image):
    folder, paths = _folder(tmp_path, make_image, 4)
    dup_path = str(folder / "img0 copy.png")  # byte-identical duplicate; longer name loses
    shutil.copy(paths[0], dup_path)
    (folder / "broken.jpg").write_bytes(b"not an image")
    (folder / "notes.txt").write_text("ignored")
    cfg = _cfg(tmp_path, folder)
    engine = FakeEngine()
    result = CatalogRunner(cfg, engine, RunControl()).run()

    assert result.phase == "done"
    assert result.total == 6 and result.ok == 4 and result.duplicates == 1 and result.unreadable == 1
    described = sorted(p for call in engine.calls for p in call)
    assert described == sorted(paths)  # the duplicate and the broken file were never sent

    rows = {r["path"]: r for r in csv.DictReader(open(cfg.output_csv))}
    assert len(rows) == 6
    dup = rows[dup_path]
    assert dup["status"] == "duplicate" and dup["duplicate_of"] == str(folder / "img0.png")
    assert dup["summary"] == rows[str(folder / "img0.png")]["summary"] == "desc of img0.png"
    assert rows[str(folder / "broken.jpg")]["status"] == "unreadable"
    assert rows[str(folder / "img1.png")]["width"] == "21"
    assert rows[str(folder / "img1.png")]["sha256"]
    js = json.load(open(cfg.output_json))
    assert len(js) == 6 and js[0]["path"] == rows[js[0]["path"]]["path"]


def test_stop_then_resume_never_redescribes(tmp_path, make_image):
    folder, paths = _folder(tmp_path, make_image, 8)
    control = RunControl()
    engine1 = FakeEngine(stop_control=control, stop_after=1)
    cfg = _cfg(tmp_path, folder, workers=1, batch_size=2)
    r1 = CatalogRunner(cfg, engine1, control).run()
    assert r1.phase == "stopped"
    assert 0 < r1.ok < 8 and r1.pending == 8 - r1.ok
    # partial CSV still written
    rows = list(csv.DictReader(open(cfg.output_csv)))
    assert sum(r["status"] == "pending" for r in rows) == r1.pending

    engine2 = FakeEngine()
    r2 = CatalogRunner(_cfg(tmp_path, folder, workers=3, batch_size=3), engine2, RunControl()).run()
    assert r2.phase == "done" and r2.ok == 8 and r2.pending == 0
    sent_second = {p for call in engine2.calls for p in call}
    sent_first = {p for call in engine1.calls for p in call}
    assert sent_second.isdisjoint(sent_first)
    assert sent_first | sent_second == set(paths)


def test_errors_are_retried_on_next_run_and_done_files_are_not(tmp_path, make_image):
    folder, paths = _folder(tmp_path, make_image, 3)
    cfg = _cfg(tmp_path, folder)
    engine1 = FakeEngine(fail_paths={paths[1]})
    r1 = CatalogRunner(cfg, engine1, RunControl()).run()
    assert r1.ok == 2 and r1.errors == 1

    engine2 = FakeEngine()
    r2 = CatalogRunner(cfg, engine2, RunControl()).run()
    assert r2.ok == 3 and r2.errors == 0
    assert [p for call in engine2.calls for p in call] == [paths[1]]


def test_changed_file_is_rehashed_and_redescribed(tmp_path, make_image):
    folder, paths = _folder(tmp_path, make_image, 2)
    cfg = _cfg(tmp_path, folder)
    CatalogRunner(cfg, FakeEngine(), RunControl()).run()
    shutil.copy(make_image("new.png", size=(77, 77), color=(1, 2, 3)), paths[0])
    engine2 = FakeEngine()
    r2 = CatalogRunner(cfg, engine2, RunControl()).run()
    assert [p for call in engine2.calls for p in call] == [paths[0]]
    assert r2.ok == 2
    db = CatalogDB(cfg.db_path)
    assert db.get(paths[0])["width"] == 77
    db.close()


def test_fatal_engine_error_stops_run(tmp_path, make_image):
    from image_cataloger.engines.api_engine import FatalEngineError

    folder, paths = _folder(tmp_path, make_image, 6)

    class Fatal(FakeEngine):
        def describe(self, paths, control):
            raise FatalEngineError("bad key")

    cfg = _cfg(tmp_path, folder, workers=2, batch_size=2)
    runner = CatalogRunner(cfg, Fatal(), RunControl())
    r = runner.run()
    assert runner.fatal_error and "bad key" in runner.fatal_error
    assert r.phase == "stopped" and r.ok == 0


def test_deleted_files_drop_out_of_catalog(tmp_path, make_image):
    folder, paths = _folder(tmp_path, make_image, 3)
    cfg = _cfg(tmp_path, folder)
    CatalogRunner(cfg, FakeEngine(), RunControl()).run()
    import os
    os.remove(paths[2])
    r = CatalogRunner(cfg, FakeEngine(), RunControl()).run()
    assert r.total == 2


def test_dedupe_only_never_calls_engine_and_writes_duplicates_csv(tmp_path, make_image):
    folder, paths = _folder(tmp_path, make_image, 3)
    shutil.copy(paths[1], folder / "zz_copy1.png")
    shutil.copy(paths[1], folder / "zz_copy2.png")
    cfg = _cfg(tmp_path, folder, dedupe_only=True)
    engine = FakeEngine()
    r = CatalogRunner(cfg, engine, RunControl()).run()
    assert r.phase == "done" and engine.calls == []
    assert r.total == 5 and r.duplicates == 2 and r.pending == 3 and r.ok == 0
    assert r.extra["duplicate_groups"] == 1
    rows = list(csv.DictReader(open(cfg.output_csv)))
    assert sum(x["status"] == "pending" for x in rows) == 3
    dup_rows = list(csv.DictReader(open(cfg.output_duplicates_csv)))
    assert [d["duplicate"] for d in dup_rows] == [str(folder / "zz_copy1.png"), str(folder / "zz_copy2.png")]
    assert all(d["canonical"] == paths[1] for d in dup_rows)
    assert r.extra["duplicate_bytes"] == 2 * (folder / "zz_copy1.png").stat().st_size

    # dedupe-only works with no engine at all, and a later full run describes only pending files
    r2 = CatalogRunner(_cfg(tmp_path, folder, dedupe_only=True), None, RunControl()).run()
    assert r2.phase == "done" and r2.duplicates == 2
    engine3 = FakeEngine()
    r3 = CatalogRunner(_cfg(tmp_path, folder), engine3, RunControl()).run()
    assert r3.ok == 3 and sorted(p for c in engine3.calls for p in c) == sorted(paths)
    dup_rows = list(csv.DictReader(open(cfg.output_duplicates_csv)))
    assert all(d["summary"] == "desc of img1.png" for d in dup_rows)
