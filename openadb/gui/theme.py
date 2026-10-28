"""Light/dark themes. 'system' keeps the platform style (e.g. Breeze on KDE Plasma)."""

from __future__ import annotations

from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

_default_style: str | None = None
_default_palette: QPalette | None = None


def _dark_palette() -> QPalette:
    p = QPalette()
    window, base, alt, text = QColor(37, 39, 43), QColor(28, 30, 33), QColor(44, 47, 52), QColor(230, 232, 235)
    highlight = QColor(61, 142, 230)
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, alt)
    p.setColor(QPalette.ColorRole.ToolTipBase, alt)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(140, 144, 150))
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, alt)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 90, 90))
    p.setColor(QPalette.ColorRole.Highlight, highlight)
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.Link, QColor(110, 170, 255))
    p.setColor(QPalette.ColorRole.Mid, QColor(60, 63, 68))
    p.setColor(QPalette.ColorRole.Dark, QColor(20, 21, 23))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(120, 123, 128))
    return p


def _light_palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(246, 247, 249))
    p.setColor(QPalette.ColorRole.WindowText, QColor(30, 32, 36))
    p.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(242, 244, 247))
    p.setColor(QPalette.ColorRole.Text, QColor(30, 32, 36))
    p.setColor(QPalette.ColorRole.Button, QColor(238, 240, 243))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(30, 32, 36))
    p.setColor(QPalette.ColorRole.Highlight, QColor(52, 120, 220))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 240))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(30, 32, 36))
    return p


STYLESHEET = """
QToolBar { spacing: 4px; padding: 3px; }
QTableView { gridline-color: transparent; }
QTableView::item { padding: 2px 6px; }
QHeaderView::section { padding: 4px 6px; }
QLabel#paneTitle { font-weight: 600; font-size: 11pt; }
QLabel#hint { color: palette(placeholder-text); }
QFrame#progressCard { border: 1px solid palette(mid); border-radius: 6px; }
QLabel#warning { color: #c0392b; font-weight: 600; }
QToolButton#crumb { border: none; padding: 2px 4px; }
QToolButton#crumb:hover { text-decoration: underline; }
"""


def apply_theme(app: QApplication, theme: str) -> None:
    global _default_style, _default_palette
    if _default_style is None:
        _default_style = app.style().name()
        _default_palette = QPalette(app.palette())
    if theme == "dark":
        app.setStyle(QStyleFactory.create("Fusion"))
        app.setPalette(_dark_palette())
    elif theme == "light":
        app.setStyle(QStyleFactory.create("Fusion"))
        app.setPalette(_light_palette())
    else:
        if _default_style:
            style = QStyleFactory.create(_default_style)
            if style is not None:
                app.setStyle(style)
        if _default_palette is not None:
            app.setPalette(_default_palette)
    app.setStyleSheet(STYLESHEET)


def is_dark() -> bool:
    pal = QGuiApplication.palette()
    return pal.color(QPalette.ColorRole.Window).lightness() < 128
