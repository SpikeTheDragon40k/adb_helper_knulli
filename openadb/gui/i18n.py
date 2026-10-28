"""Localization helpers. All user-visible strings go through :func:`tr`."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QTranslator

_CONTEXT = "OpenADB"
_installed: list[QTranslator] = []


def tr(text: str, disambiguation: str | None = None, n: int = -1) -> str:
    return QCoreApplication.translate(_CONTEXT, text, disambiguation, n)


def install_translators(app: QCoreApplication, language: str = "system") -> str:
    """Load Qt's own and the application's translations for ``language``.

    Translation files are looked up as ``openadb/i18n/openadb_<locale>.qm``.
    Returns the locale name that was requested.
    """
    for t in _installed:
        app.removeTranslator(t)
    _installed.clear()
    locale = QLocale.system() if language in ("", "system") else QLocale(language)
    qt = QTranslator(app)
    if qt.load(locale, "qtbase", "_", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt)
        _installed.append(qt)
    own = QTranslator(app)
    i18n_dir = Path(__file__).resolve().parent.parent / "i18n"
    if own.load(locale, "openadb", "_", str(i18n_dir)):
        app.installTranslator(own)
        _installed.append(own)
    return locale.name()
