"""Item model for a remote directory listing."""

from __future__ import annotations

import json
import posixpath
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QIcon
from PySide6.QtCore import QMimeDatabase

from openadb.core.progress import format_bytes
from openadb.core.remote_fs import RemoteEntry
from openadb.gui.i18n import tr
from openadb.gui.icons import icon

MIME_REMOTE = "application/x-openadb-remote-paths"
ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
SORT_ROLE = Qt.ItemDataRole.UserRole + 2

COLUMNS = ("Name", "Size", "Type", "Modified", "Permissions", "Owner")

_mime_db = QMimeDatabase()
_icon_cache: dict[str, QIcon] = {}


def entry_icon(entry: RemoteEntry) -> QIcon:
    if entry.navigable:
        key = "folder-link" if entry.is_link else "folder"
        if key not in _icon_cache:
            _icon_cache[key] = icon("inode-directory") if not entry.is_link else icon("folder")
        return _icon_cache[key]
    if entry.is_link:
        return _icon_cache.setdefault("link", icon("emblem-symbolic-link"))
    ext = posixpath.splitext(entry.name)[1].lower()
    if ext not in _icon_cache:
        mime = _mime_db.mimeTypeForFile(entry.name, QMimeDatabase.MatchMode.MatchExtension)
        ic = QIcon.fromTheme(mime.iconName())
        if ic.isNull():
            ic = QIcon.fromTheme(mime.genericIconName())
        if ic.isNull():
            ic = icon("text-x-generic")
        _icon_cache[ext] = ic
    return _icon_cache[ext]


def display_name(entry: RemoteEntry) -> str:
    name = entry.name
    if any(c in name for c in "\n\r\t"):
        name = name.replace("\n", "⏎").replace("\r", "␍").replace("\t", "⇥")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        name = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return name


def encode_remote_mime(serial: str, paths: list[str]) -> QMimeData:
    md = QMimeData()
    md.setData(MIME_REMOTE, json.dumps({"serial": serial, "paths": paths}).encode("utf-8", "surrogateescape"))
    md.setText("\n".join(paths))
    return md


def decode_remote_mime(md: QMimeData) -> tuple[str, list[str]] | None:
    if not md.hasFormat(MIME_REMOTE):
        return None
    try:
        data = json.loads(bytes(md.data(MIME_REMOTE)).decode("utf-8", "surrogateescape"))
        return str(data["serial"]), [str(p) for p in data["paths"]]
    except (ValueError, KeyError):
        return None


class RemoteModel(QAbstractTableModel):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.entries: list[RemoteEntry] = []
        self.serial = ""
        self.directory = ""

    def set_entries(self, serial: str, directory: str, entries: list[RemoteEntry]) -> None:
        self.beginResetModel()
        self.serial, self.directory, self.entries = serial, directory, entries
        self.endResetModel()

    def clear(self) -> None:
        self.set_entries("", "", [])

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr(COLUMNS[section])
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.TextAlignmentRole and section == 1:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        col = index.column()
        if role == ENTRY_ROLE:
            return e
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return display_name(e)
            if col == 1:
                return "" if e.navigable or e.size is None else format_bytes(e.size)
            if col == 2:
                return e.type_label
            if col == 3:
                return e.mtime_dt.strftime("%Y-%m-%d %H:%M") if e.mtime_dt else ""
            if col == 4:
                return e.permissions
            if col == 5:
                return f"{e.owner}:{e.group}" if e.owner else ""
        if role == SORT_ROLE:
            if col == 0:
                return e.name.lower()
            if col == 1:
                return -1 if e.size is None else e.size
            if col == 2:
                return e.type_label
            if col == 3:
                return e.mtime or 0
            if col == 4:
                return e.permissions
            return e.owner
        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return entry_icon(e)
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 1:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = e.path
            if e.is_link:
                tip += f"\n→ {e.link_target}"
            if e.unsafe_name:
                tip += "\n" + tr("This name contains characters the device shell cannot transport safely.")
            if e.size is not None and not e.navigable:
                tip += f"\n{e.size:,} bytes"
            return tip
        if role == Qt.ItemDataRole.ForegroundRole and e.name.startswith("."):
            from PySide6.QtGui import QPalette
            from PySide6.QtWidgets import QApplication

            return QApplication.palette().brush(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        if index.isValid():
            return base | Qt.ItemFlag.ItemIsDragEnabled
        return base

    def mimeTypes(self) -> list[str]:  # noqa: N802
        return [MIME_REMOTE, "text/plain"]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:  # noqa: N802
        rows = sorted({i.row() for i in indexes if i.isValid()})
        return encode_remote_mime(self.serial, [self.entries[r].path for r in rows])

    def supportedDragActions(self) -> Qt.DropAction:  # noqa: N802
        return Qt.DropAction.CopyAction


class RemoteProxy(QSortFilterProxyModel):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.show_hidden = True
        self.text = ""
        self.setSortRole(SORT_ROLE)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_filter(self, text: str, show_hidden: bool) -> None:
        self.text, self.show_hidden = text.lower(), show_hidden
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex | QPersistentModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        e: RemoteEntry = model.entries[row]  # type: ignore[attr-defined]
        if not self.show_hidden and e.name.startswith("."):
            return False
        return not self.text or self.text in e.name.lower()

    def lessThan(self, left: QModelIndex | QPersistentModelIndex, right: QModelIndex | QPersistentModelIndex) -> bool:  # noqa: N802
        le: RemoteEntry = left.data(ENTRY_ROLE)
        re_: RemoteEntry = right.data(ENTRY_ROLE)
        asc = self.sortOrder() == Qt.SortOrder.AscendingOrder
        if le.navigable != re_.navigable:
            # folders first regardless of sort direction
            return le.navigable if asc else not le.navigable
        lv, rv = left.data(SORT_ROLE), right.data(SORT_ROLE)
        if lv == rv:
            return le.name.lower() < re_.name.lower()
        return lv < rv
