"""Runs a CatalogRunner on a QThread and relays progress to the GUI thread."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from ..config import RunConfig
from ..engines.base import RunControl
from ..runner import CatalogRunner, Progress, build_engine


class RunWorker(QThread):
    progress = Signal(object)  # Progress
    log = Signal(str)
    finished_run = Signal(object)  # Progress
    failed = Signal(str)

    def __init__(self, config: RunConfig, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.control = RunControl()
        self.runner: CatalogRunner | None = None

    def run(self) -> None:  # executes on the worker thread
        try:
            engine = build_engine(self.config)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"Cannot start engine: {e}")
            return
        self.runner = CatalogRunner(
            self.config,
            engine,
            self.control,
            on_progress=lambda p: self.progress.emit(p),
            on_log=lambda s: self.log.emit(s),
        )
        result: Progress = self.runner.run()
        if self.runner.fatal_error and result.phase == "failed":
            self.failed.emit(self.runner.fatal_error)
        self.finished_run.emit(result)
