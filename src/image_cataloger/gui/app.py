"""GUI entry point: `image-cataloger` or `python -m image_cataloger`."""
from __future__ import annotations

import logging
import sys


def _check_qt_path() -> None:
    """Qt cannot load its platform plugins from a path with non-ASCII characters
    (e.g. a folder named "Spencer’s MacBook Pro"). Fail with a clear message."""
    import PySide6

    location = PySide6.__file__ or ""
    if not location.isascii():
        sys.stderr.write(
            "Image Cataloger cannot start: PySide6 is installed under a path containing "
            f"non-ASCII characters, which Qt cannot load plugins from:\n  {location}\n"
            "Create the virtual environment at an ASCII-only path (e.g. ~/Projects/image-cataloger) "
            "and install again.\n"
        )
        sys.exit(2)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _check_qt_path()
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Image Cataloger")
    app.setOrganizationName("image-cataloger")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
