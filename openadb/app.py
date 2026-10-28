"""Application entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openadb", description="OpenADB File Manager")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--debug", action="store_true", help="verbose logging to stderr")
    parser.add_argument("--adb", help="path to the adb executable (stored in settings)")
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    from openadb import APP_NAME, __version__

    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QCoreApplication.setOrganizationName("OpenADB")
    QCoreApplication.setApplicationName("openadb")
    QCoreApplication.setApplicationVersion(__version__)
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationDisplayName(APP_NAME)
    QGuiApplication.setDesktopFileName("io.github.openadb.FileManager")

    from openadb.core.config import ConfigStore
    from openadb.gui.context import AppContext
    from openadb.gui.i18n import install_translators
    from openadb.gui.icons import app_icon
    from openadb.gui.main_window import MainWindow
    from openadb.gui.theme import apply_theme

    store = ConfigStore()
    settings = store.load()
    if args.adb:
        settings.adb_path = os.path.abspath(args.adb)
    install_translators(app, settings.language)
    apply_theme(app, settings.theme)
    app.setWindowIcon(app_icon())
    ctx = AppContext(settings, store)
    window = MainWindow(ctx)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
