"""Right-hand pane: browsing and managing the remote (ADB) filesystem."""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QModelIndex, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressDialog,
    QSizePolicy,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openadb.core.capabilities import DeviceCapabilities
from openadb.core.errors import DeviceUnavailableError, OpenADBError, ProtectedPathError, UnsafePathError
from openadb.core.knulli import KNULLI_SHORTCUTS
from openadb.core.operations import BatchOutcome, delete_remote, mkdir_remote, paste_remote, protected_among, rename_remote
from openadb.core.paths import normalize_remote, remote_parent
from openadb.core.process import CancelToken
from openadb.core.progress import format_bytes
from openadb.core.remote_fs import RemoteEntry, RemoteFS
from openadb.gui.context import AppContext, ClipboardState
from openadb.gui.dialogs import DeleteConfirmDialog, NameDialog, PropertiesDialog, ask_yes_no, show_error
from openadb.gui.i18n import tr
from openadb.gui.icons import icon
from openadb.gui.remote_model import ENTRY_ROLE, RemoteModel, RemoteProxy, decode_remote_mime
from openadb.gui.widgets import FileTableView, PathBar, add_shortcut, remote_crumbs


class RemotePane(QWidget):
    upload_requested = Signal(list, str, bool)  # local paths, remote dest dir, move
    download_requested = Signal(list, object, bool)  # remote paths, local dest (None = local pane dir), move
    status_message = Signal(str)
    directory_changed = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.serial: str | None = None
        self.caps: DeviceCapabilities | None = None
        self.cwd = ""
        self.back_stack: list[str] = []
        self.forward_stack: list[str] = []
        self._generation = 0
        self._loading = False
        self._last_dir: dict[str, str] = {}

        self.model = RemoteModel(self)
        self.proxy = RemoteProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.show_hidden = ctx.settings.show_hidden

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        header = QHBoxLayout()
        self.title = QLabel(tr("Device"))
        self.title.setObjectName("paneTitle")
        header.addWidget(self.title)
        header.addStretch(1)
        self.space_label = QLabel()
        self.space_label.setObjectName("hint")
        header.addWidget(self.space_label)
        lay.addLayout(header)

        self.toolbar = QToolBar()
        self.toolbar.setIconSize(self.toolbar.iconSize() * 0.8)
        lay.addWidget(self.toolbar)
        self.path_bar = PathBar(remote_crumbs)
        self.path_bar.path_entered.connect(self.navigate)
        lay.addWidget(self.path_bar)

        self.stack = QStackedWidget()
        self.view = FileTableView()
        self.view.setModel(self.proxy)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, 6):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        hh.resizeSection(1, 90)
        hh.resizeSection(2, 110)
        hh.resizeSection(3, 130)
        hh.resizeSection(4, 95)
        hh.resizeSection(5, 110)
        self.view.doubleClicked.connect(self._activated)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.accept_check = self._accept_drop
        self.view.drop_handler = self._handle_drop
        self.view.selectionModel().selectionChanged.connect(self._update_actions)
        self.placeholder = QLabel()
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setObjectName("hint")
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.placeholder)
        lay.addWidget(self.stack, 1)
        self.info = QLabel()
        self.info.setObjectName("hint")
        lay.addWidget(self.info)

        self._build_actions()
        self.set_device(None, None)

    # ----------------------------------------------------------------- actions
    def _act(self, icon_name: str, text: str, slot: Callable[[], None], shortcut: Any = None, tip: str = "") -> QAction:
        a = QAction(icon(icon_name), text, self)
        a.triggered.connect(slot)
        if shortcut is not None:
            a.setShortcut(QKeySequence(shortcut))
            a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(a)
        if tip:
            a.setToolTip(tip)
        return a

    def _build_actions(self) -> None:
        self.a_back = self._act("go-previous", tr("Back"), self.go_back, "Alt+Left")
        self.a_forward = self._act("go-next", tr("Forward"), self.go_forward, "Alt+Right")
        self.a_up = self._act("go-up", tr("Up"), self.go_up, "Alt+Up")
        self.a_home = self._act("go-home", tr("Home"), self.go_home, "Alt+Home", tr("Default remote folder"))
        self.a_refresh = self._act("view-refresh", tr("Refresh"), self.refresh, QKeySequence.StandardKey.Refresh)
        self.a_mkdir = self._act("folder-new", tr("New folder"), self.new_folder, "Ctrl+Shift+N")
        self.a_rename = self._act("edit-rename", tr("Rename"), self.rename_selected, "F2")
        self.a_delete = self._act("edit-delete", tr("Delete permanently"), self.delete_selected, QKeySequence.StandardKey.Delete)
        self.a_props = self._act("document-properties", tr("Properties"), self.show_properties, "Alt+Return")
        self.a_download = self._act("go-previous-view", tr("Download to local folder"), self.download_selected, "Ctrl+D",
                                    tr("Download selection into the folder shown in the local pane"))
        self.a_copy = self._act("edit-copy", tr("Copy"), lambda: self.copy_selected(False), QKeySequence.StandardKey.Copy)
        self.a_cut = self._act("edit-cut", tr("Cut"), lambda: self.copy_selected(True), QKeySequence.StandardKey.Cut)
        self.a_paste = self._act("edit-paste", tr("Paste"), self.paste, QKeySequence.StandardKey.Paste)
        self.a_copy_path = self._act("edit-copy", tr("Copy path"), self.copy_paths, "Ctrl+Shift+C")
        self.a_select_all = self._act("", tr("Select all"), self.view.selectAll, QKeySequence.StandardKey.SelectAll)
        self.a_edit_path = self._act("", tr("Edit path"), self.path_bar.edit_mode, "Ctrl+L")
        add_shortcut(self, "Backspace", self.go_up)
        add_shortcut(self, "Return", self._open_current)
        add_shortcut(self, "Enter", self._open_current)
        add_shortcut(self, "Ctrl+F", lambda: self.filter_edit.setFocus())

        for a in (self.a_back, self.a_forward, self.a_up, self.a_home, self.a_refresh):
            self.toolbar.addAction(a)
        self.toolbar.addSeparator()
        for a in (self.a_mkdir, self.a_rename, self.a_delete, self.a_props):
            self.toolbar.addAction(a)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.a_download)
        self.shortcut_btn = QToolButton()
        self.shortcut_btn.setIcon(icon("applications-games"))
        self.shortcut_btn.setText(tr("Knulli"))
        self.shortcut_btn.setToolTip(tr("Knulli shortcuts"))
        self.shortcut_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.shortcut_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.shortcut_menu = QMenu(self)
        self.shortcut_btn.setMenu(self.shortcut_menu)
        self.shortcut_action = self.toolbar.addWidget(self.shortcut_btn)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar.addWidget(spacer)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(tr("Filter…"))
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(180)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.toolbar.addWidget(self.filter_edit)
        self.a_hidden = QAction(icon("view-hidden"), tr("Show hidden files"), self)
        self.a_hidden.setCheckable(True)
        self.a_hidden.setChecked(self.ctx.settings.show_hidden)
        self.a_hidden.toggled.connect(lambda _c: self._apply_filter())
        self.toolbar.addAction(self.a_hidden)

    def _apply_filter(self) -> None:
        self.proxy.set_filter(self.filter_edit.text(), self.a_hidden.isChecked())
        self._update_info()

    # ----------------------------------------------------------------- device
    def set_device(self, serial: str | None, caps: DeviceCapabilities | None) -> None:
        changed = serial != self.serial
        self.serial, self.caps = serial, caps
        if serial is None:
            self.model.clear()
            self.cwd = ""
            self.path_bar.set_path("")
            self.title.setText(tr("Device"))
            self.space_label.setText("")
            self._show_placeholder(tr("No device selected.\n\nConnect a device over USB (enable ADB on the device) or use "
                                      "Device ▸ Connect network device…"))
            self._update_actions()
            return
        self.title.setText(self.ctx.display_name(serial))
        self._rebuild_shortcuts()
        if caps is None:
            self._show_placeholder(tr("Probing device {serial}…").format(serial=serial))
            self._update_actions()
            return
        if changed or not self.cwd:
            self.back_stack.clear()
            self.forward_stack.clear()
            self.cwd = ""
            self.navigate(self._last_dir.get(serial) or self.ctx.settings.remote_home(serial), record=False)
        else:
            self.refresh()

    def device_lost(self, serial: str) -> None:
        if serial != self.serial:
            return
        self.caps = None
        self.model.clear()
        self._show_placeholder(tr("Device {serial} disconnected.\n\nReconnect it to continue; the last folder will be "
                                  "reopened automatically.").format(serial=serial))
        self._update_actions()

    def _rebuild_shortcuts(self) -> None:
        self.shortcut_menu.clear()
        enabled = self.ctx.knulli_enabled(self.serial)
        existing = set(self.caps.existing_dirs) if self.caps else set()
        prof = self.ctx.settings.device_profiles.get(self.serial or "")
        for label, path in KNULLI_SHORTCUTS:
            a = self.shortcut_menu.addAction(icon("folder"), f"{tr(label)}  —  {path}")
            a.setEnabled(path in existing)
            if path not in existing:
                a.setToolTip(tr("Not present on this device"))
            a.triggered.connect(lambda _=False, p=path: self.navigate(p))
        if prof and prof.bookmarks:
            self.shortcut_menu.addSeparator()
            for b in prof.bookmarks:
                self.shortcut_menu.addAction(icon("folder"), b).triggered.connect(lambda _=False, p=b: self.navigate(p))
        self.shortcut_menu.addSeparator()
        self.shortcut_menu.addAction(tr("Bookmark current folder")).triggered.connect(self._bookmark)
        self.shortcut_action.setVisible(True)
        self.shortcut_btn.setText(tr("Knulli") if enabled else tr("Places"))

    def _bookmark(self) -> None:
        if not self.serial or not self.cwd:
            return
        prof = self.ctx.settings.profile(self.serial)
        if self.cwd not in prof.bookmarks:
            prof.bookmarks.append(self.cwd)
            self.ctx.save_settings()
        self._rebuild_shortcuts()

    def _show_placeholder(self, text: str) -> None:
        self.placeholder.setText(text)
        self.stack.setCurrentWidget(self.placeholder)

    # ----------------------------------------------------------------- navigation
    def rfs(self) -> RemoteFS:
        if not self.serial:
            raise OpenADBError(tr("No device selected"))
        return self.ctx.rfs_for(self.serial)

    def navigate(self, path: str, record: bool = True) -> None:
        if not self.serial or self.caps is None:
            return
        try:
            path = normalize_remote(path.strip())
        except UnsafePathError as exc:
            show_error(self, tr("Invalid path"), str(exc))
            self.path_bar.set_path(self.cwd)
            return
        self._load(path, record=record)

    def _load(self, path: str, record: bool, select: str | None = None) -> None:
        self._generation += 1
        gen = self._generation
        serial = self.serial
        self._loading = True
        self.info.setText(tr("Loading {path}…").format(path=path))
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        prev = self.cwd

        def work() -> list[RemoteEntry]:
            rfs = self.ctx.rfs_for(serial)  # type: ignore[arg-type]
            return rfs.list_dir(path)

        def done(entries: list[RemoteEntry]) -> None:
            QApplication.restoreOverrideCursor()
            if gen != self._generation or serial != self.serial:
                return
            self._loading = False
            if record and prev and prev != path:
                self.back_stack.append(prev)
                self.forward_stack.clear()
            self.cwd = path
            self._last_dir[serial] = path  # type: ignore[index]
            self.model.set_entries(serial, path, entries)  # type: ignore[arg-type]
            self.path_bar.set_path(path)
            self.stack.setCurrentWidget(self.view)
            self._update_info()
            self._update_actions()
            self.directory_changed.emit(path)
            self.ctx.logbook.debug(f"Directory listing: {path} ({len(entries)} entries)", device=serial, op_type="list")
            if select:
                self.select_names([select])
            self._update_space()

        def fail(exc: BaseException) -> None:
            QApplication.restoreOverrideCursor()
            if gen != self._generation:
                return
            self._loading = False
            self.path_bar.set_path(self.cwd)
            self.ctx.logbook.error(f"Directory listing failed: {path}", device=serial, op_type="list", source=path,
                                   error=str(exc))
            if isinstance(exc, DeviceUnavailableError):
                self.device_lost(serial or "")
                self.ctx.refresh_devices()
                return
            self._update_info()
            show_error(self, tr("Cannot open folder"), str(exc))

        self.ctx.tasks.submit(work, on_done=done, on_error=fail)

    def _update_space(self) -> None:
        serial, path = self.serial, self.cwd
        if not serial or not path or not (self.caps and self.caps.has("df")):
            self.space_label.setText("")
            return

        def work():  # type: ignore[no-untyped-def]
            return self.ctx.rfs_for(serial).disk_space(path)

        def done(space) -> None:  # type: ignore[no-untyped-def]
            if serial == self.serial and path == self.cwd:
                self.space_label.setText(
                    tr("{free} free of {total}").format(free=format_bytes(space.available), total=format_bytes(space.total))
                    if space else "")

        self.ctx.tasks.submit(work, on_done=done, on_error=lambda e: self.space_label.setText(""))

    def refresh(self, select: str | None = None) -> None:
        if self.cwd and self.serial and self.caps:
            self._load(self.cwd, record=False, select=select)
        elif self.serial and self.caps:
            self.navigate(self.ctx.settings.remote_home(self.serial), record=False)

    def go_back(self) -> None:
        if self.back_stack:
            target = self.back_stack.pop()
            self.forward_stack.append(self.cwd)
            self._load(target, record=False)

    def go_forward(self) -> None:
        if self.forward_stack:
            target = self.forward_stack.pop()
            self.back_stack.append(self.cwd)
            self._load(target, record=False)

    def go_up(self) -> None:
        if self.cwd and self.cwd != "/":
            child = posixpath.basename(self.cwd)
            self._load(remote_parent(self.cwd), record=True, select=child)

    def go_home(self) -> None:
        if self.serial:
            self.navigate(self.ctx.settings.remote_home(self.serial))

    def _activated(self, index: QModelIndex) -> None:
        entry: RemoteEntry = index.data(ENTRY_ROLE)
        if entry is None:
            return
        if entry.navigable:
            self.navigate(entry.path)
        else:
            self.show_properties()

    def _open_current(self) -> None:
        idx = self.view.currentIndex()
        if idx.isValid():
            self._activated(idx)

    # ----------------------------------------------------------------- selection helpers
    def selected_entries(self) -> list[RemoteEntry]:
        rows = {self.proxy.mapToSource(i).row() for i in self.view.selectionModel().selectedRows()}
        return [self.model.entries[r] for r in sorted(rows)]

    def select_names(self, names: list[str]) -> None:
        sm = self.view.selectionModel()
        sm.clearSelection()
        first = None
        for row, e in enumerate(self.model.entries):
            if e.name in names:
                idx = self.proxy.mapFromSource(self.model.index(row, 0))
                if idx.isValid():
                    sm.select(idx, sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
                    first = first or idx
        if first is not None:
            self.view.scrollTo(first)
            sm.setCurrentIndex(first, sm.SelectionFlag.NoUpdate)

    def _update_info(self) -> None:
        if not self.cwd:
            self.info.setText("")
            return
        entries = self.model.entries
        n_dirs = sum(1 for e in entries if e.navigable)
        size = sum(e.size or 0 for e in entries if not e.navigable)
        shown = self.proxy.rowCount()
        txt = tr("{dirs} folders, {files} files ({size})").format(dirs=n_dirs, files=len(entries) - n_dirs,
                                                                  size=format_bytes(size))
        if shown != len(entries):
            txt += " · " + tr("{n} shown").format(n=shown)
        sel = self.selected_entries()
        if sel:
            txt += " · " + tr("{n} selected").format(n=len(sel))
        self.info.setText(txt)

    def _update_actions(self) -> None:
        ready = bool(self.serial and self.caps and self.cwd)
        sel = self.selected_entries() if ready else []
        for a in (self.a_refresh, self.a_mkdir, self.a_home, self.a_up, self.a_paste, self.a_select_all, self.a_edit_path):
            a.setEnabled(ready)
        self.a_back.setEnabled(ready and bool(self.back_stack))
        self.a_forward.setEnabled(ready and bool(self.forward_stack))
        for a in (self.a_delete, self.a_download, self.a_copy, self.a_cut, self.a_copy_path):
            a.setEnabled(bool(sel))
        self.a_rename.setEnabled(len(sel) == 1)
        self.a_props.setEnabled(len(sel) == 1)
        self.shortcut_btn.setEnabled(bool(self.serial and self.caps))
        self._update_info()

    # ----------------------------------------------------------------- context menu
    def _context_menu(self, pos: QPoint) -> None:
        if not self.serial or not self.caps:
            return
        sel = self.selected_entries()
        m = QMenu(self)
        if len(sel) == 1 and sel[0].navigable:
            m.addAction(icon("folder"), tr("Open")).triggered.connect(lambda: self.navigate(sel[0].path))
        if sel:
            m.addAction(self.a_download)
            m.addAction(icon("go-previous-view"), tr("Move to local folder")).triggered.connect(
                lambda: self.download_selected(move=True))
            m.addSeparator()
            m.addAction(self.a_cut)
            m.addAction(self.a_copy)
        m.addAction(self.a_paste)
        m.addSeparator()
        if sel:
            m.addAction(self.a_rename)
            m.addAction(self.a_delete)
            m.addAction(self.a_copy_path)
            m.addSeparator()
        m.addAction(self.a_mkdir)
        m.addAction(self.a_refresh)
        if len(sel) == 1:
            m.addSeparator()
            m.addAction(self.a_props)
        m.exec(self.view.viewport().mapToGlobal(pos))

    # ----------------------------------------------------------------- file operations
    def new_folder(self) -> None:
        if not self.cwd:
            return
        dlg = NameDialog(tr("New folder"), tr("Folder name (in {dir}):").format(dir=self.cwd), "", allow_nested=True,
                         existing={e.name for e in self.model.entries}, parent=self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        name = dlg.value().strip("/")
        serial, cwd = self.serial, self.cwd

        def done(res) -> None:  # type: ignore[no-untyped-def]
            if not res.ok:
                show_error(self, tr("Create folder failed"), res.message)
            self.refresh(select=name.split("/")[0])

        self.ctx.tasks.submit(lambda: mkdir_remote(self.ctx.rfs_for(serial), cwd, name, self.ctx.logbook),  # type: ignore[arg-type]
                              on_done=done, on_error=lambda e: show_error(self, tr("Create folder failed"), str(e)))

    def rename_selected(self) -> None:
        sel = self.selected_entries()
        if len(sel) != 1:
            return
        entry = sel[0]
        if entry.unsafe_name:
            show_error(self, tr("Rename"), tr("This name cannot be handled safely over this device's shell."))
            return
        if protected_among([entry.path], tuple(self.ctx.settings.extra_protected_paths)):
            show_error(self, tr("Rename"), tr("{path} is a protected location and cannot be renamed.").format(path=entry.path))
            return
        dlg = NameDialog(tr("Rename"), tr("New name for {name}:").format(name=entry.name), entry.name,
                         existing={e.name for e in self.model.entries}, parent=self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        new = dlg.value()
        serial = self.serial

        def done(res) -> None:  # type: ignore[no-untyped-def]
            if not res.ok:
                show_error(self, tr("Rename failed"), res.message)
            self.refresh(select=new if res.ok else entry.name)

        self.ctx.tasks.submit(lambda: rename_remote(self.ctx.rfs_for(serial), entry.path, new, self.ctx.logbook),  # type: ignore[arg-type]
                              on_done=done, on_error=lambda e: show_error(self, tr("Rename failed"), str(e)))

    def delete_selected(self) -> None:
        sel = self.selected_entries()
        if not sel or not self.serial:
            return
        paths = [e.path for e in sel]
        unsafe = [e.path for e in sel if e.unsafe_name]
        if unsafe:
            show_error(self, tr("Delete"), tr("Some names cannot be handled safely over this device's shell:"),
                       "\n".join(unsafe))
            return
        protected = protected_among(paths, tuple(self.ctx.settings.extra_protected_paths))
        if protected:
            show_error(self, tr("Delete refused"),
                       tr("The selection contains protected locations that cannot be deleted:") + "\n\n" + "\n".join(protected))
            self.ctx.logbook.warning("Delete refused: protected location selected", device=self.serial, op_type="delete",
                                     extra={"protected": protected, "confirmed": False})
            return
        serial = self.serial
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)

        def inspected(inspections) -> None:  # type: ignore[no-untyped-def]
            QApplication.restoreOverrideCursor()
            dlg = DeleteConfirmDialog(inspections, self.ctx.display_name(serial), parent=self)
            if dlg.exec() != dlg.DialogCode.Accepted:
                self.ctx.logbook.info(f"Delete cancelled by user ({len(paths)} item(s))", device=serial, op_type="delete",
                                      extra={"confirmed": False})
                return
            self._run_delete(serial, paths, dlg.recursive)

        def fail(exc: BaseException) -> None:
            QApplication.restoreOverrideCursor()
            show_error(self, tr("Delete"), tr("Could not inspect the selection: {err}").format(err=exc))

        self.ctx.tasks.submit(lambda: self.ctx.rfs_for(serial).inspect_for_delete(paths), on_done=inspected, on_error=fail)

    def _run_delete(self, serial: str, paths: list[str], recursive: bool) -> None:
        cancel = CancelToken()
        dlg = QProgressDialog(tr("Deleting…"), tr("Stop"), 0, len(paths), self)
        dlg.setWindowTitle(tr("Deleting"))
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(300)
        dlg.canceled.connect(cancel.cancel)
        bridge = _ProgressRelay(self)
        bridge.progressed.connect(lambda n, total, name: (dlg.setValue(n), dlg.setLabelText(
            tr("Deleted {n} of {total}\n{name}").format(n=n, total=total, name=name))))

        def work() -> BatchOutcome:
            return delete_remote(self.ctx.rfs_for(serial), paths, recursive=recursive, confirmed=True,
                                 logbook=self.ctx.logbook, cancel=cancel,
                                 on_progress=lambda n, t, r: bridge.progressed.emit(n, t, r.path))

        def done(outcome: BatchOutcome) -> None:
            dlg.reset()
            dlg.deleteLater()
            self.refresh()
            failed = outcome.failed
            if failed or outcome.cancelled:
                text = tr("{ok} of {total} item(s) deleted.").format(ok=len(outcome.succeeded), total=len(paths))
                if outcome.cancelled:
                    text += "\n" + tr("The operation was stopped before all items were processed.")
                show_error(self, tr("Delete finished with problems"), text,
                           "\n".join(f"{r.path}: {r.message} (exit {r.returncode})" for r in failed))
            else:
                self.status_message.emit(tr("Deleted {n} item(s)").format(n=len(outcome.succeeded)))

        def fail(exc: BaseException) -> None:
            dlg.reset()
            self.refresh()
            show_error(self, tr("Delete failed"), str(exc))

        self.ctx.tasks.submit(work, on_done=done, on_error=fail)

    def show_properties(self) -> None:
        sel = self.selected_entries()
        if len(sel) != 1 or not self.serial:
            return
        serial = self.serial
        entry = sel[0]

        def calc(callback) -> None:  # type: ignore[no-untyped-def]
            self.ctx.tasks.submit(lambda: self.ctx.rfs_for(serial).dir_usage(entry.path), on_done=callback,
                                  on_error=lambda e: callback(None))

        def show(fresh: RemoteEntry | None) -> None:
            PropertiesDialog(fresh or entry, serial, self, size_callback=calc if (self.caps and self.caps.has("du")) else None).exec()

        self.ctx.tasks.submit(lambda: self.ctx.rfs_for(serial).stat(entry.path), on_done=show, on_error=lambda e: show(None))

    def copy_paths(self) -> None:
        paths = [e.path for e in self.selected_entries()]
        if paths:
            QGuiApplication.clipboard().setText("\n".join(paths))
            self.status_message.emit(tr("Copied {n} path(s) to the clipboard").format(n=len(paths)))

    def copy_selected(self, cut: bool) -> None:
        sel = self.selected_entries()
        if not sel or not self.serial:
            return
        if cut:
            protected = protected_among([e.path for e in sel], tuple(self.ctx.settings.extra_protected_paths))
            if protected:
                show_error(self, tr("Cut"), tr("Protected locations cannot be moved:") + "\n" + "\n".join(protected))
                return
        self.ctx.clipboard = ClipboardState("remote", [e.path for e in sel], cut, self.serial)
        QGuiApplication.clipboard().setText("\n".join(e.path for e in sel))
        self.status_message.emit((tr("Cut {n} item(s)") if cut else tr("Copied {n} item(s)")).format(n=len(sel)))

    def paste(self, target_dir: str | None = None) -> None:
        clip = self.ctx.clipboard
        dest = target_dir or self.cwd
        if not clip or not dest or not self.serial:
            return
        if clip.kind == "local":
            self.upload_requested.emit(clip.paths, dest, clip.cut)
            if clip.cut:
                self.ctx.clipboard = None
            return
        if clip.serial != self.serial:
            show_error(self, tr("Paste"), tr("Copying directly between two devices is not supported. "
                                             "Download to the computer first, then upload."))
            return
        self._remote_paste(clip.paths, dest, clip.cut)
        if clip.cut:
            self.ctx.clipboard = None

    def _remote_paste(self, sources: list[str], dest: str, move: bool) -> None:
        serial = self.serial
        if any(dest == s or dest.startswith(s.rstrip("/") + "/") for s in sources):
            show_error(self, tr("Paste"), tr("A folder cannot be copied or moved into itself."))
            return
        relay = _ConflictRelay(self)
        cancel = CancelToken()
        dlg = QProgressDialog(tr("Working…"), tr("Stop"), 0, len(sources), self)
        dlg.setWindowTitle(tr("Moving") if move else tr("Copying"))
        dlg.setMinimumDuration(400)
        dlg.canceled.connect(cancel.cancel)
        prog = _ProgressRelay(self)
        prog.progressed.connect(lambda n, total, name: (dlg.setValue(n - 1), dlg.setLabelText(name)))

        def work() -> BatchOutcome:
            return paste_remote(self.ctx.rfs_for(serial), sources, dest, move=move, logbook=self.ctx.logbook,  # type: ignore[arg-type]
                                on_conflict=relay.ask, cancel=cancel,
                                on_progress=lambda n, t, name: prog.progressed.emit(n, t, name))

        def done(outcome: BatchOutcome) -> None:
            dlg.reset()
            self.refresh()
            failed = [r for r in outcome.failed if not r.message.startswith("skipped")]
            if failed:
                show_error(self, tr("Some items failed"), tr("{n} item(s) could not be processed.").format(n=len(failed)),
                           "\n".join(f"{r.path}: {r.message}" for r in failed))

        def fail(exc: BaseException) -> None:
            dlg.reset()
            show_error(self, tr("Paste failed"), str(exc))

        self.ctx.tasks.submit(work, on_done=done, on_error=fail)

    def download_selected(self, move: bool = False) -> None:
        sel = self.selected_entries()
        if not sel:
            return
        if move:
            protected = protected_among([e.path for e in sel], tuple(self.ctx.settings.extra_protected_paths))
            if protected:
                show_error(self, tr("Move"), tr("Protected locations cannot be moved:") + "\n" + "\n".join(protected))
                return
        self.download_requested.emit([e.path for e in sel], None, move)

    # ----------------------------------------------------------------- drag & drop
    def _drop_target_dir(self, pos: QPoint) -> str:
        idx = self.view.indexAt(pos)
        if idx.isValid():
            entry: RemoteEntry = idx.data(ENTRY_ROLE)
            if entry and entry.navigable:
                return entry.path
        return self.cwd

    def _accept_drop(self, md: Any) -> bool:
        if not (self.serial and self.caps and self.cwd):
            return False
        return md.hasUrls() or decode_remote_mime(md) is not None

    def _handle_drop(self, md: Any, pos: QPoint, action: Qt.DropAction) -> bool:
        dest = self._drop_target_dir(pos)
        remote = decode_remote_mime(md)
        if remote is not None:
            serial, paths = remote
            if serial != self.serial:
                show_error(self, tr("Drop"), tr("Copying directly between two devices is not supported."))
                return False
            paths = [p for p in paths if remote_parent(p) != dest and p != dest]
            if not paths:
                return False
            menu = QMenu(self)
            a_move = menu.addAction(icon("edit-cut"), tr("Move here"))
            a_copy = menu.addAction(icon("edit-copy"), tr("Copy here"))
            menu.addSeparator()
            menu.addAction(tr("Cancel"))
            chosen = menu.exec(self.view.viewport().mapToGlobal(pos))
            if chosen is a_move:
                protected = protected_among(paths, tuple(self.ctx.settings.extra_protected_paths))
                if protected:
                    show_error(self, tr("Move"), tr("Protected locations cannot be moved:") + "\n" + "\n".join(protected))
                    return False
                self._remote_paste(paths, dest, True)
            elif chosen is a_copy:
                self._remote_paste(paths, dest, False)
            return chosen in (a_move, a_copy)
        local = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        if not local:
            return False
        self.upload_requested.emit(local, dest, False)
        return True



class _ProgressRelay(QObject):
    progressed = Signal(int, int, str)


class _ConflictRelay(QObject):
    """Ask the user (on the GUI thread) how to handle a remote copy/move conflict."""

    _ask = Signal(object)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        self._ask.connect(self._on_ask, Qt.ConnectionType.QueuedConnection)
        self._sticky: str | None = None

    def ask(self, src: str, dst: str) -> str:
        import threading

        if self._sticky:
            return self._sticky
        holder: dict[str, Any] = {"event": threading.Event(), "answer": "skip", "src": src, "dst": dst}
        self._ask.emit(holder)
        holder["event"].wait()
        return holder["answer"]

    def _on_ask(self, holder: dict[str, Any]) -> None:
        from PySide6.QtWidgets import QCheckBox, QMessageBox

        box = QMessageBox(QMessageBox.Icon.Question, tr("Item already exists"),
                          tr("{dst} already exists.\nExisting items are never overwritten.").format(dst=holder["dst"]),
                          parent=self._parent)
        keep = box.addButton(tr("Keep both (rename)"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("Skip"), QMessageBox.ButtonRole.RejectRole)
        cb = QCheckBox(tr("Apply to all"))
        box.setCheckBox(cb)
        box.exec()
        holder["answer"] = "rename" if box.clickedButton() is keep else "skip"
        if cb.isChecked():
            self._sticky = holder["answer"]
        holder["event"].set()


__all__ = ["RemotePane", "ProtectedPathError", "ask_yes_no"]
