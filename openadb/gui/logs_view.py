"""Activity & Logs panel: live structured log viewer with filters and exports."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt, QTimer, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from openadb.core.diagnostics import create_bundle
from openadb.core.logbook import Level, LogBook, LogEvent
from openadb.gui.context import AppContext
from openadb.gui.dialogs import show_error
from openadb.gui.i18n import tr
from openadb.gui.icons import icon

COLS = ("Time", "Level", "Device", "Operation", "Message", "Details")
EVENT_ROLE = Qt.ItemDataRole.UserRole + 20
MAX_ROWS = 50000


def _details(ev: LogEvent) -> str:
    parts = []
    if ev.source:
        parts.append(f"src: {ev.source}")
    if ev.destination:
        parts.append(f"dst: {ev.destination}")
    if ev.duration is not None:
        parts.append(f"{ev.duration:.3f}s")
    if ev.result:
        parts.append(f"result: {ev.result}")
    if ev.error:
        parts.append(f"error: {ev.error}")
    for k, v in ev.extra.items():
        if v not in (None, "", [], {}):
            parts.append(f"{k}: {v}")
    return " · ".join(parts)


class LogModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[LogEvent] = []
        self._pending: list[LogEvent] = []
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    def add(self, ev: LogEvent) -> None:
        self._pending.append(ev)

    def add_many(self, evs: list[LogEvent]) -> None:
        self._pending.extend(evs)
        self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        new, self._pending = self._pending, []
        overflow = len(self.events) + len(new) - MAX_ROWS
        if overflow > 0:
            self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
            del self.events[:overflow]
            self.endRemoveRows()
        start = len(self.events)
        self.beginInsertRows(QModelIndex(), start, start + len(new) - 1)
        self.events.extend(new)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self.events.clear()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.events)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr(COLS[section])
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        ev = self.events[index.row()]
        col = index.column()
        if role == EVENT_ROLE:
            return ev
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                ev.ts.strftime("%H:%M:%S.%f")[:-3],
                ev.level.label,
                ev.device or "",
                f"{ev.op_type or ''}{('#' + ev.op_id) if ev.op_id else ''}",
                ev.message,
                _details(ev),
            )[col]
        if role == Qt.ItemDataRole.ToolTipRole:
            return ev.format_text()
        if role == Qt.ItemDataRole.ForegroundRole and col in (1, 4):
            if ev.level >= Level.ERROR:
                return QBrush(QColor(220, 60, 50))
            if ev.level >= Level.WARNING:
                return QBrush(QColor(210, 140, 20))
            if ev.level <= Level.DEBUG:
                return QBrush(QColor(130, 130, 130))
        return None


class LogFilter(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.min_level = Level.DEBUG
        self.text = ""
        self.device = ""
        self.op_type = ""

    def update(self, min_level: Level, text: str, device: str, op_type: str) -> None:
        self.min_level, self.text, self.device, self.op_type = min_level, text.lower(), device, op_type
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex | QPersistentModelIndex) -> bool:  # noqa: N802
        ev: LogEvent = self.sourceModel().events[row]  # type: ignore[attr-defined]
        if ev.level < self.min_level:
            return False
        if self.device and ev.device != self.device:
            return False
        if self.op_type and ev.op_type != self.op_type:
            return False
        if self.text:
            hay = (ev.message + " " + _details(ev) + " " + (ev.device or "") + " " + (ev.op_id or "")).lower()
            return self.text in hay
        return True


class LogsView(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.model = LogModel()
        self.proxy = LogFilter()
        self.proxy.setSourceModel(self.model)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        self.level = QComboBox()
        for lv in (Level.DEBUG, Level.INFO, Level.WARNING, Level.ERROR):
            self.level.addItem(tr("{lv} and above").format(lv=lv.label), lv)
        self.level.setCurrentIndex(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search logs…"))
        self.search.setClearButtonEnabled(True)
        self.device = QComboBox()
        self.device.addItem(tr("All devices"), "")
        self.op = QComboBox()
        self.op.addItem(tr("All operations"), "")
        self.autoscroll = QCheckBox(tr("Follow"))
        self.autoscroll.setChecked(True)
        for w in (QLabel(tr("Severity:")), self.level, self.search, self.device, self.op, self.autoscroll):
            row.addWidget(w)
        row.setStretch(2, 1)
        lay.addLayout(row)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, w in ((0, 95), (1, 60), (2, 120), (3, 130), (4, 420)):
            hh.resizeSection(col, w)
        hh.setStretchLastSection(True)
        lay.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setObjectName("hint")
        buttons.addWidget(self.count_label)
        buttons.addStretch(1)
        for text, ic, slot in (
            (tr("Copy selected"), "edit-copy", self.copy_selected),
            (tr("Export TXT…"), "document-save", lambda: self.export("txt")),
            (tr("Export JSONL…"), "document-save", lambda: self.export("jsonl")),
            (tr("Diagnostic bundle…"), "document-save", self.save_bundle),
            (tr("Open log folder"), "folder", self.open_folder),
            (tr("Clear view"), "edit-clear-history", self.model.clear),
        ):
            b = QPushButton(icon(ic), text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        lay.addLayout(buttons)

        self.level.currentIndexChanged.connect(self._refilter)
        self.search.textChanged.connect(self._refilter)
        self.device.currentIndexChanged.connect(self._refilter)
        self.op.currentIndexChanged.connect(self._refilter)
        self.model.rowsInserted.connect(self._rows_added)
        copy_sc = QKeySequence(QKeySequence.StandardKey.Copy)
        from PySide6.QtGui import QShortcut

        QShortcut(copy_sc, self.table, activated=self.copy_selected)
        ctx.log_event.connect(self.add_event)
        self.model.add_many(ctx.logbook.events())
        self._refilter()

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        super().showEvent(event)
        if self.autoscroll.isChecked():
            QTimer.singleShot(0, self.table.scrollToBottom)

    def add_event(self, ev: LogEvent) -> None:
        self.model.add(ev)

    def _rows_added(self, _parent: QModelIndex, first: int, last: int) -> None:
        for ev in self.model.events[first:last + 1]:
            if ev.device and self.device.findData(ev.device) < 0:
                self.device.addItem(ev.device, ev.device)
            if ev.op_type and self.op.findData(ev.op_type) < 0:
                self.op.addItem(ev.op_type, ev.op_type)
        self._update_count()
        if self.autoscroll.isChecked():
            self.table.scrollToBottom()

    def _refilter(self) -> None:
        self.proxy.update(self.level.currentData(), self.search.text(), self.device.currentData() or "",
                          self.op.currentData() or "")
        self._update_count()

    def _update_count(self) -> None:
        self.count_label.setText(tr("{shown} of {total} events").format(shown=self.proxy.rowCount(),
                                                                        total=self.model.rowCount()))

    def visible_events(self) -> list[LogEvent]:
        return [self.proxy.index(r, 0).data(EVENT_ROLE) for r in range(self.proxy.rowCount())]

    def copy_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        evs = [self.proxy.index(r, 0).data(EVENT_ROLE) for r in rows]
        if evs:
            QGuiApplication.clipboard().setText("\n".join(e.format_text() for e in evs))

    def export(self, kind: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        default = os.path.join(self.ctx.settings.local_home(), f"openadb-log-{stamp}.{kind}")
        path, _ = QFileDialog.getSaveFileName(self, tr("Export logs"), default,
                                              "Text (*.txt)" if kind == "txt" else "JSON Lines (*.jsonl)")
        if not path:
            return
        events = self.visible_events()
        try:
            n = LogBook.export_txt(path, events) if kind == "txt" else LogBook.export_jsonl(path, events)
        except OSError as exc:
            show_error(self, tr("Export failed"), str(exc))
            return
        self.ctx.logbook.info(f"Exported {n} log events ({kind})", op_type="export", destination=path, result="success")
        QMessageBox.information(self, tr("Export logs"), tr("Exported {n} events (current filter) to\n{path}").format(n=n, path=path))

    def save_bundle(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        default = os.path.join(self.ctx.settings.local_home(), f"openadb-diagnostics-{stamp}.zip")
        path, _ = QFileDialog.getSaveFileName(self, tr("Save diagnostic bundle"), default, "ZIP (*.zip)")
        if not path:
            return
        snap = self.ctx.snapshot_for_diagnostics()
        from openadb.core.paths import data_dir

        try:
            create_bundle(path, self.ctx.logbook, settings=snap["settings"], capabilities=snap["capabilities"],
                          devices=snap["devices"], adb_version=snap["adb_version"],
                          queue_file=data_dir() / "transfer_queue.json")
        except OSError as exc:
            show_error(self, tr("Diagnostic bundle"), str(exc))
            return
        self.ctx.logbook.info("Diagnostic bundle saved", op_type="export", destination=path, result="success")
        QMessageBox.information(self, tr("Diagnostic bundle"),
                                tr("Saved to {path}.\nIt stays on this computer; nothing is uploaded.").format(path=path))

    def open_folder(self) -> None:
        if self.ctx.logbook.directory:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.ctx.logbook.directory)))
