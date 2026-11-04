"""Main window: pick a folder, set threads, Start / Pause / Stop, watch progress."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from .. import __version__
from ..config import RunConfig, default_output_base, get_api_key, key_source
from ..engines.cli_engine import CLI_MODELS, find_claude_cli
from ..pricing import API_MODELS, DEFAULT_MODEL, MODEL_LABELS
from ..runner import Progress
from .settings_dialog import SettingsDialog
from .worker import RunWorker


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}h {m:02d}m" if h else f"{m:d}m {s:02d}s"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Image Cataloger {__version__}")
        self.setMinimumWidth(760)
        self.settings = QSettings("image-cataloger", "image-cataloger")
        self.worker: RunWorker | None = None
        self.last_config: RunConfig | None = None
        self._last_progress: Progress | None = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ---- source --------------------------------------------------------
        src_box = QGroupBox("Source")
        src = QFormLayout(src_box)
        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(self.settings.value("folder", ""))
        self.folder_edit.setPlaceholderText("/Volumes/share/Photos  (local folder or mounted network share)")
        self.folder_edit.textChanged.connect(self._folder_changed)
        folder_row.addWidget(self.folder_edit)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.pick_folder)
        folder_row.addWidget(browse)
        src.addRow("Folder:", folder_row)
        self.recursive_cb = QCheckBox("Include subfolders")
        self.recursive_cb.setChecked(self.settings.value("recursive", True, type=bool))
        src.addRow("", self.recursive_cb)
        self.output_label = QLabel("")
        self.output_label.setWordWrap(True)
        self.output_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.out_btn = QPushButton("Change…")
        self.out_btn.clicked.connect(self.pick_output)
        src.addRow("Output:", self.out_btn)
        src.addRow("", self.output_label)
        self.output_base: str = ""
        root.addWidget(src_box)

        # ---- engine --------------------------------------------------------
        eng_box = QGroupBox("Engine")
        eng = QGridLayout(eng_box)
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("Claude API (recommended; needs API key)", "api")
        self.engine_combo.addItem("Local Claude Code CLI (subscription, no key)", "cli")
        self.engine_combo.setCurrentIndex(0 if self.settings.value("engine", "api") == "api" else 1)
        self.engine_combo.currentIndexChanged.connect(self._engine_changed)
        eng.addWidget(QLabel("Engine:"), 0, 0)
        eng.addWidget(self.engine_combo, 0, 1, 1, 3)

        self.model_combo = QComboBox()
        eng.addWidget(QLabel("Model:"), 1, 0)
        eng.addWidget(self.model_combo, 1, 1, 1, 3)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 64)
        self.workers_spin.setValue(self.settings.value("workers", 4, type=int))
        self.workers_spin.setToolTip("Parallel requests in flight. Raise until you see rate-limit retries in the log.")
        eng.addWidget(QLabel("Threads:"), 2, 0)
        eng.addWidget(self.workers_spin, 2, 1)

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 20)
        self.batch_spin.setValue(self.settings.value("batch_size", 6, type=int))
        self.batch_spin.setToolTip("Images per request. 4-8 is the sweet spot; batches that come back malformed are re-asked one image at a time.")
        eng.addWidget(QLabel("Images per request:"), 2, 2)
        eng.addWidget(self.batch_spin, 2, 3)

        self.max_side_spin = QSpinBox()
        self.max_side_spin.setRange(256, 4096)
        self.max_side_spin.setSingleStep(64)
        self.max_side_spin.setValue(self.settings.value("max_side", 1568, type=int))
        self.max_side_spin.setToolTip("Long side after downscaling. 1568 is the most Claude uses; smaller is cheaper but less accurate.")
        eng.addWidget(QLabel("Max image side (px):"), 3, 0)
        eng.addWidget(self.max_side_spin, 3, 1)

        self.effort_combo = QComboBox()
        for e in ("low", "medium", "high"):
            self.effort_combo.addItem(e, e)
        self.effort_combo.setCurrentText(self.settings.value("effort", "low"))
        self.effort_combo.setToolTip("Model thinking effort. 'low' is right for captions; higher costs more output tokens.")
        eng.addWidget(QLabel("Effort:"), 3, 2)
        eng.addWidget(self.effort_combo, 3, 3)

        key_row = QHBoxLayout()
        self.key_label = QLabel("")
        key_row.addWidget(self.key_label)
        key_btn = QPushButton("API key…")
        key_btn.clicked.connect(self.open_settings)
        key_row.addWidget(key_btn)
        key_row.addStretch()
        eng.addLayout(key_row, 4, 0, 1, 4)
        root.addWidget(eng_box)

        # ---- controls ------------------------------------------------------
        ctl = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self.start)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.toggled.connect(self.toggle_pause)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.open_btn = QPushButton("Open CSV")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_csv)
        for b in (self.start_btn, self.pause_btn, self.stop_btn):
            ctl.addWidget(b)
        ctl.addStretch()
        ctl.addWidget(self.open_btn)
        root.addLayout(ctl)

        # ---- progress ------------------------------------------------------
        prog_box = QGroupBox("Progress")
        pg = QGridLayout(prog_box)
        self.phase_label = QLabel("Idle")
        pg.addWidget(self.phase_label, 0, 0, 1, 6)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat("%v / %m")
        pg.addWidget(self.bar, 1, 0, 1, 6)
        self.stat_labels: dict[str, QLabel] = {}
        stats = [("done", "Done"), ("rate", "Images / min"), ("eta", "ETA"),
                 ("cost", "Est. cost"), ("tokens", "Tokens in / out"), ("errors", "Errors"),
                 ("dups", "Duplicates"), ("unreadable", "Unreadable"), ("elapsed", "Elapsed")]
        for i, (key, title) in enumerate(stats):
            r, c = divmod(i, 3)
            box = QVBoxLayout()
            t = QLabel(title)
            t.setStyleSheet("color: gray; font-size: 11px;")
            v = QLabel("--")
            v.setStyleSheet("font-size: 18px; font-weight: 600;")
            box.addWidget(t)
            box.addWidget(v)
            pg.addLayout(box, 2 + r, c * 2, 1, 2)
            self.stat_labels[key] = v
        root.addWidget(prog_box)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setPlaceholderText("Log")
        root.addWidget(self.log_view, 1)

        self._engine_changed()
        self._refresh_key_label()
        self._folder_changed()
        self.run_started_at: float | None = None
        self.tick = QTimer(self)
        self.tick.setInterval(1000)
        self.tick.timeout.connect(self._tick)

    # ---- UI helpers --------------------------------------------------------
    def _engine_changed(self) -> None:
        engine = self.engine_combo.currentData()
        self.model_combo.clear()
        if engine == "api":
            for m in API_MODELS:
                self.model_combo.addItem(MODEL_LABELS.get(m, m), m)
            saved = self.settings.value("model_api", DEFAULT_MODEL)
        else:
            for m in CLI_MODELS:
                self.model_combo.addItem(m, m)
            saved = self.settings.value("model_cli", "haiku")
        idx = self.model_combo.findData(saved)
        self.model_combo.setCurrentIndex(idx if idx >= 0 else 0)
        for w in (self.max_side_spin, self.effort_combo):
            w.setEnabled(engine == "api")
        self._refresh_key_label()

    def _refresh_key_label(self) -> None:
        if self.engine_combo.currentData() == "api":
            self.key_label.setText(f"API key: {key_source()}")
        else:
            cli = find_claude_cli()
            self.key_label.setText(f"claude CLI: {cli or 'NOT FOUND on PATH'}")

    def _folder_changed(self) -> None:
        folder = self.folder_edit.text().strip()
        if folder and os.path.isdir(os.path.expanduser(folder)):
            if not self.output_base or self.settings.value("output_auto", True, type=bool):
                self.output_base = str(default_output_base(folder))
        self._refresh_output_label()

    def _refresh_output_label(self) -> None:
        if self.output_base:
            self.output_label.setText(f"{self.output_base}  (.csv / .json / .sqlite)")
        else:
            self.output_label.setText("(choose a folder)")

    def pick_folder(self) -> None:
        start = self.folder_edit.text().strip() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Choose the folder to catalog", start)
        if chosen:
            self.folder_edit.setText(chosen)

    def pick_output(self) -> None:
        start = (self.output_base + ".csv") if self.output_base else os.path.expanduser("~")
        chosen, _ = QFileDialog.getSaveFileName(self, "Catalog output (CSV; JSON and DB use the same base name)",
                                                start, "CSV (*.csv)")
        if chosen:
            base = chosen[:-4] if chosen.lower().endswith(".csv") else chosen
            self.output_base = base
            self.settings.setValue("output_auto", False)
            self._refresh_output_label()

    def open_settings(self) -> None:
        dlg = SettingsDialog(self)
        dlg.exec()
        self._refresh_key_label()

    def append_log(self, text: str) -> None:
        self.log_view.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    # ---- run control -------------------------------------------------------
    def _build_config(self) -> RunConfig | None:
        folder = os.path.expanduser(self.folder_edit.text().strip())
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "Folder", "Choose an existing folder first.")
            return None
        engine = self.engine_combo.currentData()
        api_key = get_api_key() if engine == "api" else None
        if engine == "api" and not api_key:
            QMessageBox.warning(self, "API key", "No API key found. Click 'API key…' to add one, "
                                                 "or switch to the Local Claude Code CLI engine.")
            return None
        if engine == "cli" and not find_claude_cli():
            QMessageBox.warning(self, "Claude CLI", "The 'claude' command was not found on PATH.")
            return None
        if not self.output_base:
            self.output_base = str(default_output_base(folder))
        cfg = RunConfig(
            folder=folder,
            recursive=self.recursive_cb.isChecked(),
            workers=self.workers_spin.value(),
            batch_size=self.batch_spin.value(),
            engine=engine,
            model=self.model_combo.currentData(),
            effort=self.effort_combo.currentData(),
            max_side=self.max_side_spin.value(),
            db_path=self.output_base + ".sqlite",
            output_csv=self.output_base + ".csv",
            output_json=self.output_base + ".json",
            api_key=api_key,
        )
        s = self.settings
        s.setValue("folder", folder)
        s.setValue("recursive", cfg.recursive)
        s.setValue("workers", cfg.workers)
        s.setValue("batch_size", cfg.batch_size)
        s.setValue("engine", engine)
        s.setValue("model_api" if engine == "api" else "model_cli", cfg.model)
        s.setValue("effort", cfg.effort)
        s.setValue("max_side", cfg.max_side)
        return cfg

    def start(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        cfg = self._build_config()
        if cfg is None:
            return
        self.last_config = cfg
        self.log_view.clear()
        self.append_log(f"Starting: {cfg.folder}  engine={cfg.engine} model={cfg.model} "
                        f"threads={cfg.workers} batch={cfg.batch_size}")
        self.worker = RunWorker(cfg)
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self.append_log)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished_run.connect(self.on_finished)
        self.run_started_at = time.time()
        self._set_running(True)
        self.worker.start()
        self.tick.start()

    def toggle_pause(self, paused: bool) -> None:
        if not self.worker:
            return
        if paused:
            self.worker.control.pause()
            self.pause_btn.setText("Resume")
            self.phase_label.setText("Paused (in-flight requests finish first)")
            self.append_log("Paused.")
        else:
            self.worker.control.resume()
            self.pause_btn.setText("Pause")
            self.append_log("Resumed.")

    def stop(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.control.stop()
            self.stop_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
            self.phase_label.setText("Stopping after in-flight requests… progress is saved.")
            self.append_log("Stop requested.")

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.pause_btn.setChecked(False)
        self.pause_btn.setText("Pause")
        self.stop_btn.setEnabled(running)
        for w in (self.folder_edit, self.recursive_cb, self.engine_combo, self.model_combo,
                  self.workers_spin, self.batch_spin, self.max_side_spin, self.effort_combo, self.out_btn):
            w.setEnabled(not running)
        if not running:
            self._engine_changed()

    # ---- slots --------------------------------------------------------------
    def on_progress(self, p: Progress) -> None:
        self._last_progress = p
        if not self.pause_btn.isChecked():
            self.phase_label.setText(p.message or p.phase)
        if p.phase == "hashing":
            total = p.extra.get("hash_total", 0)
            self.bar.setRange(0, max(1, total))
            self.bar.setValue(min(total, p.extra.get("hashed", 0)))
        else:
            self.bar.setRange(0, max(1, p.total))
            self.bar.setValue(min(p.total, p.done))
        L = self.stat_labels
        L["done"].setText(f"{p.done} / {p.total}")
        L["rate"].setText(f"{p.rate_per_min:.1f}" if p.rate_per_min else "--")
        L["eta"].setText(_fmt_eta(p.eta_seconds) if p.phase == "describing" else "--")
        L["cost"].setText(f"${p.cost_usd:.4f}")
        L["tokens"].setText(f"{p.input_tokens:,} / {p.output_tokens:,}")
        L["errors"].setText(str(p.errors))
        L["dups"].setText(str(p.duplicates))
        L["unreadable"].setText(str(p.unreadable))

    def _tick(self) -> None:
        if self.run_started_at:
            el = int(time.time() - self.run_started_at)
            self.stat_labels["elapsed"].setText(_fmt_eta(el) if el >= 60 else f"{el}s")

    def on_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Run failed", message)

    def on_finished(self, p: Progress) -> None:
        self.tick.stop()
        self._set_running(False)
        self.open_btn.setEnabled(bool(self.last_config and os.path.exists(self.last_config.output_csv)))
        self.phase_label.setText(
            f"{p.phase.capitalize()}: {p.ok} described, {p.duplicates} duplicates, "
            f"{p.unreadable} unreadable, {p.errors} errors, {p.pending} pending. Est. ${p.cost_usd:.4f}"
        )

    def open_csv(self) -> None:
        if self.last_config:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_config.output_csv))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.worker and self.worker.isRunning():
            answer = QMessageBox.question(
                self, "Run in progress",
                "A run is in progress. Stop it and quit? (Progress is saved; re-running resumes.)")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.control.stop()
            self.worker.wait(30_000)
        event.accept()
