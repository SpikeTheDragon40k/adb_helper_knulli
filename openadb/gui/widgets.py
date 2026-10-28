"""Reusable widgets: breadcrumb/editable path bar, file table view with drag & drop hooks."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QStackedWidget,
    QTableView,
    QToolButton,
    QWidget,
)

from openadb.gui.i18n import tr


def remote_crumbs(path: str) -> list[tuple[str, str]]:
    crumbs = [("/", "/")]
    acc = ""
    for part in [p for p in path.split("/") if p]:
        acc += "/" + part
        crumbs.append((part, acc))
    return crumbs


def local_crumbs(path: str) -> list[tuple[str, str]]:
    path = os.path.abspath(path)
    drive, rest = os.path.splitdrive(path)
    root = drive + os.sep if sys.platform.startswith("win") else "/"
    crumbs = [(drive or "/", root)]
    acc = root
    for part in [p for p in rest.split(os.sep) if p]:
        acc = os.path.join(acc, part)
        crumbs.append((part, acc))
    return crumbs


class PathBar(QWidget):
    """Breadcrumb buttons that switch to an editable line edit on click/Ctrl+L."""

    path_entered = Signal(str)

    def __init__(self, crumbs: Callable[[str], list[tuple[str, str]]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._crumbs = crumbs
        self._path = ""
        self.stack = QStackedWidget(self)
        # breadcrumb page
        self.crumb_frame = QFrame()
        self.crumb_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.crumb_layout = QHBoxLayout(self.crumb_frame)
        self.crumb_layout.setContentsMargins(4, 0, 4, 0)
        self.crumb_layout.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.crumb_frame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setFixedHeight(30)
        self.scroll.mousePressEvent = lambda _e: self.edit_mode()  # type: ignore[method-assign]
        self.edit = QLineEdit()
        self.edit.setClearButtonEnabled(True)
        self.edit.returnPressed.connect(self._commit)
        self.edit.editingFinished.connect(self._editing_finished)
        self.stack.addWidget(self.scroll)
        self.stack.addWidget(self.edit)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stack)
        self.setFixedHeight(32)
        self.edit_btn = QToolButton()
        self.edit_btn.setText("✎")
        self.edit_btn.setToolTip(tr("Edit path (Ctrl+L)"))
        self.edit_btn.setAutoRaise(True)
        self.edit_btn.clicked.connect(self.edit_mode)
        lay.addWidget(self.edit_btn)

    def path(self) -> str:
        return self._path

    def set_path(self, path: str) -> None:
        self._path = path
        self.edit.setText(path)
        while self.crumb_layout.count():
            item = self.crumb_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        crumbs = self._crumbs(path) if path else []
        for i, (label, target) in enumerate(crumbs):
            if i > 0 and i < len(crumbs):
                sep = QToolButton()
                sep.setText("›")
                sep.setAutoRaise(True)
                sep.setEnabled(False)
                self.crumb_layout.addWidget(sep)
            btn = QToolButton()
            btn.setObjectName("crumb")
            btn.setText(label)
            btn.setAutoRaise(True)
            btn.setToolTip(target)
            if i == len(crumbs) - 1:
                f = btn.font()
                f.setBold(True)
                btn.setFont(f)
            btn.clicked.connect(lambda _=False, t=target: self.path_entered.emit(t))
            self.crumb_layout.addWidget(btn)
        self.crumb_layout.addStretch(1)
        self.stack.setCurrentWidget(self.scroll)
        self.scroll.horizontalScrollBar().setValue(self.scroll.horizontalScrollBar().maximum())

    def edit_mode(self) -> None:
        self.stack.setCurrentWidget(self.edit)
        self.edit.setFocus()
        self.edit.selectAll()

    def _commit(self) -> None:
        text = self.edit.text().strip()
        if text:
            self.path_entered.emit(text)
        self.stack.setCurrentWidget(self.scroll)

    def _editing_finished(self) -> None:
        if not self.edit.hasFocus():
            self.edit.setText(self._path)
            self.stack.setCurrentWidget(self.scroll)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.edit.setText(self._path)
            self.stack.setCurrentWidget(self.scroll)
            return
        super().keyPressEvent(event)


DropHandler = Callable[[QMimeData, QPoint, Qt.DropAction], bool]
AcceptCheck = Callable[[QMimeData], bool]


class FileTableView(QTableView):
    """Table view with pluggable drop handling (the models stay read-only)."""

    activated_path = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(26)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setStretchLastSection(False)
        self.accept_check: AcceptCheck | None = None
        self.drop_handler: DropHandler | None = None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self.accept_check and self.accept_check(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802
        if self.accept_check and self.accept_check(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if self.drop_handler and self.drop_handler(event.mimeData(), event.position().toPoint(), event.proposedAction()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()


def add_shortcut(widget: QWidget, keys: QKeySequence | QKeySequence.StandardKey | str, slot: Callable[[], None]) -> QShortcut:
    sc = QShortcut(QKeySequence(keys), widget)
    sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    sc.activated.connect(slot)
    return sc
