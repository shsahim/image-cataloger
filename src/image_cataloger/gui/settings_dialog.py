"""API key dialog. The key goes to the OS keychain via keyring, never to disk."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout,
)

from ..config import ENV_VAR, get_api_key, key_source, set_api_key


class SettingsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("API key")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Paste an Anthropic API key (console.anthropic.com → API keys). It is stored in "
            f"your operating system's keychain. If the {ENV_VAR} environment variable is set, "
            "it takes precedence."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-ant-...")
        existing = get_api_key()
        if existing:
            self.key_edit.setText(existing)
        row = QHBoxLayout()
        row.addWidget(self.key_edit)
        self.show_btn = QPushButton("Show")
        self.show_btn.setCheckable(True)
        self.show_btn.toggled.connect(
            lambda on: self.key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        row.addWidget(self.show_btn)
        form.addRow("API key:", row)
        self.source_label = QLabel(f"Current source: {key_source()}")
        form.addRow("", self.source_label)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        self.test_btn = QPushButton("Test key")
        self.test_btn.clicked.connect(self.test_key)
        btn_row.addWidget(self.test_btn)
        self.clear_btn = QPushButton("Remove from keychain")
        self.clear_btn.clicked.connect(self.clear_key)
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self) -> None:
        value = self.key_edit.text().strip()
        if not value:
            QMessageBox.warning(self, "API key", "Enter a key, or use 'Remove from keychain'.")
            return
        try:
            set_api_key(value)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Keychain error", f"Could not store the key:\n{e}")
            return
        self.accept()

    def clear_key(self) -> None:
        try:
            set_api_key(None)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Keychain error", str(e))
            return
        self.key_edit.clear()
        self.source_label.setText(f"Current source: {key_source()}")

    def test_key(self) -> None:
        value = self.key_edit.text().strip()
        if not value:
            QMessageBox.warning(self, "API key", "Enter a key first.")
            return
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=value, timeout=20.0, max_retries=0)
            client.models.retrieve("claude-haiku-5-5")
            QMessageBox.information(self, "API key", "The key works.")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "API key", f"The key was rejected:\n{type(e).__name__}: {e}")
        finally:
            self.unsetCursor()
