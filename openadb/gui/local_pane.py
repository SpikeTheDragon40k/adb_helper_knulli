"""Left-hand pane: browsing and managing the local filesystem."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QDir, QFile, QMimeData, QModelIndex, QPoint, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from openadb.core.local_fs import copy_local, delete_local, local_unique_path, make_dir, move_local, rename_local
from openadb.core.logbook import new_op_id
from openadb.core.paths import is_protected_local
from openadb.gui.context import AppContext, ClipboardState
from openadb.gui.dialogs import NameDialog, show_error
from openadb.gui.i18n import tr
from openadb.gui.icons import icon
from openadb.gui.remote_model import decode_remote_mime
from openadb.gui.widgets import FileTableView, PathBar, add_shortcut, local_crumbs


class LocalPane(QWidget):
    upload_requested = Signal(list, object, bool)  # local paths, remote dest (None = remote pane dir), move
    download_requested = Signal(list, str, bool)  # remote paths, local dest dir, move
    status_message = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.cwd = ""
        self.back_stack: list[str] = []
        self.forward_stack: list[str] = []

        self.model = QFileSystemModel(self)
        self.model.setReadOnly(True)
        self.model.setRootPath("")
        self._filters_base = QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.AllDirs
        self.model.setNameFilterDisables(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        header = QHBoxLayout()
        title = QLabel(tr("This computer"))
        title.setObjectName("paneTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.space_label = QLabel()
        self.space_label.setObjectName("hint")
        header.addWidget(self.space_label)
        lay.addLayout(header)
        self.toolbar = QToolBar()
        self.toolbar.setIconSize(self.toolbar.iconSize() * 0.8)
        lay.addWidget(self.toolbar)
        self.path_bar = PathBar(local_crumbs)
        self.path_bar.path_entered.connect(self.navigate)
        lay.addWidget(self.path_bar)

        self.view = FileTableView()
        self.view.setModel(self.model)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        hh.resizeSection(1, 90)
        hh.resizeSection(2, 110)
        hh.resizeSection(3, 140)
        self.view.doubleClicked.connect(self._activated)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.accept_check = self._accept_drop
        self.view.drop_handler = self._handle_drop
        self.view.selectionModel().selectionChanged.connect(lambda *_: self._update_actions())
        self.model.directoryLoaded.connect(lambda _p: self._update_info())
        lay.addWidget(self.view, 1)
        self.info = QLabel()
        self.info.setObjectName("hint")
        lay.addWidget(self.info)
        self._build_actions()
        self._apply_filter()
        self.navigate(ctx.settings.local_home(), record=False)

    # ----------------------------------------------------------------- actions
    def _act(self, icon_name: str, text: str, slot: Callable[[], None], shortcut: Any = None) -> QAction:
        a = QAction(icon(icon_name), text, self)
        a.triggered.connect(slot)
        if shortcut is not None:
            a.setShortcut(QKeySequence(shortcut))
            a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(a)
        return a

    def _build_actions(self) -> None:
        self.a_back = self._act("go-previous", tr("Back"), self.go_back, "Alt+Left")
        self.a_forward = self._act("go-next", tr("Forward"), self.go_forward, "Alt+Right")
        self.a_up = self._act("go-up", tr("Up"), self.go_up, "Alt+Up")
        self.a_home = self._act("go-home", tr("Home"), lambda: self.navigate(self.ctx.settings.local_home()), "Alt+Home")
        self.a_refresh = self._act("view-refresh", tr("Refresh"), self.refresh, QKeySequence.StandardKey.Refresh)
        self.a_mkdir = self._act("folder-new", tr("New folder"), self.new_folder, "Ctrl+Shift+N")
        self.a_rename = self._act("edit-rename", tr("Rename"), self.rename_selected, "F2")
        self.a_trash = self._act("edit-delete", tr("Move to trash"), lambda: self.delete_selected(False),
                                 QKeySequence.StandardKey.Delete)
        self.a_delete = self._act("edit-delete", tr("Delete permanently"), lambda: self.delete_selected(True), "Shift+Del")
        self.a_upload = self._act("go-next-view", tr("Upload to device"), self.upload_selected, "Ctrl+U")
        self.a_copy = self._act("edit-copy", tr("Copy"), lambda: self.copy_selected(False), QKeySequence.StandardKey.Copy)
        self.a_cut = self._act("edit-cut", tr("Cut"), lambda: self.copy_selected(True), QKeySequence.StandardKey.Cut)
        self.a_paste = self._act("edit-paste", tr("Paste"), self.paste, QKeySequence.StandardKey.Paste)
        self.a_copy_path = self._act("edit-copy", tr("Copy path"), self.copy_paths, "Ctrl+Shift+C")
        self.a_edit_path = self._act("", tr("Edit path"), self.path_bar.edit_mode, "Ctrl+L")
        self.a_select_all = self._act("", tr("Select all"), self.view.selectAll, QKeySequence.StandardKey.SelectAll)
        add_shortcut(self, "Backspace", self.go_up)
        add_shortcut(self, "Return", self._open_current)
        add_shortcut(self, "Enter", self._open_current)
        add_shortcut(self, "Ctrl+F", lambda: self.filter_edit.setFocus())
        for a in (self.a_back, self.a_forward, self.a_up, self.a_home, self.a_refresh):
            self.toolbar.addAction(a)
        self.toolbar.addSeparator()
        for a in (self.a_mkdir, self.a_rename, self.a_trash):
            self.toolbar.addAction(a)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.a_upload)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar.addWidget(spacer)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(tr("Filter…"))
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(180)
        self.filter_edit.textChanged.connect(lambda _t: self._apply_filter())
        self.toolbar.addWidget(self.filter_edit)
        self.a_hidden = QAction(icon("view-hidden"), tr("Show hidden files"), self)
        self.a_hidden.setCheckable(True)
        self.a_hidden.setChecked(self.ctx.settings.show_hidden)
        self.a_hidden.toggled.connect(lambda _c: self._apply_filter())
        self.toolbar.addAction(self.a_hidden)

    def _apply_filter(self) -> None:
        flt = self._filters_base
        if self.a_hidden.isChecked():
            flt |= QDir.Filter.Hidden | QDir.Filter.System
        self.model.setFilter(flt)
        text = self.filter_edit.text().strip()
        self.model.setNameFilters([f"*{text}*"] if text else [])
        self._update_info()

    # ----------------------------------------------------------------- navigation
    def navigate(self, path: str, record: bool = True) -> None:
        path = os.path.abspath(os.path.expanduser(path.strip()))
        if not os.path.isdir(path):
            show_error(self, tr("Cannot open folder"), tr("{path} is not an accessible folder.").format(path=path))
            self.path_bar.set_path(self.cwd)
            return
        if not os.access(path, os.R_OK | os.X_OK):
            show_error(self, tr("Cannot open folder"), tr("Permission denied: {path}").format(path=path))
            self.path_bar.set_path(self.cwd)
            return
        if record and self.cwd and self.cwd != path:
            self.back_stack.append(self.cwd)
            self.forward_stack.clear()
        self.cwd = path
        self.model.setRootPath(path)
        self.view.setRootIndex(self.model.index(path))
        self.path_bar.set_path(path)
        self._update_actions()
        self._update_space()

    def refresh(self) -> None:
        if self.cwd:
            # QFileSystemModel watches directories; re-setting the root forces a re-read.
            self.model.setRootPath("")
            self.model.setRootPath(self.cwd)
            self.view.setRootIndex(self.model.index(self.cwd))
            self._update_space()

    def go_back(self) -> None:
        if self.back_stack:
            self.forward_stack.append(self.cwd)
            self.navigate(self.back_stack.pop(), record=False)

    def go_forward(self) -> None:
        if self.forward_stack:
            self.back_stack.append(self.cwd)
            self.navigate(self.forward_stack.pop(), record=False)

    def go_up(self) -> None:
        parent = os.path.dirname(self.cwd)
        if parent and parent != self.cwd:
            child = self.cwd
            self.navigate(parent)
            self.view.setCurrentIndex(self.model.index(child))

    def _activated(self, index: QModelIndex) -> None:
        path = self.model.filePath(index)
        if self.model.isDir(index):
            self.navigate(path)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_current(self) -> None:
        idx = self.view.currentIndex()
        if idx.isValid():
            self._activated(idx.siblingAtColumn(0))

    def _update_space(self) -> None:
        from openadb.core.local_fs import free_space
        from openadb.core.progress import format_bytes

        free = free_space(self.cwd) if self.cwd else None
        self.space_label.setText(tr("{free} free").format(free=format_bytes(free)) if free is not None else "")

    # ----------------------------------------------------------------- selection
    def selected_paths(self) -> list[str]:
        return [self.model.filePath(i) for i in self.view.selectionModel().selectedRows(0)]

    def _update_info(self) -> None:
        if not self.cwd:
            return
        root = self.model.index(self.cwd)
        n = self.model.rowCount(root)
        sel = self.selected_paths()
        txt = tr("{n} items").format(n=n)
        if sel:
            txt += " · " + tr("{n} selected").format(n=len(sel))
        self.info.setText(txt)

    def _update_actions(self) -> None:
        sel = self.selected_paths()
        for a in (self.a_trash, self.a_delete, self.a_upload, self.a_copy, self.a_cut, self.a_copy_path):
            a.setEnabled(bool(sel))
        self.a_rename.setEnabled(len(sel) == 1)
        self.a_back.setEnabled(bool(self.back_stack))
        self.a_forward.setEnabled(bool(self.forward_stack))
        self._update_info()

    def _context_menu(self, pos: QPoint) -> None:
        sel = self.selected_paths()
        m = QMenu(self)
        if sel:
            m.addAction(self.a_upload)
            m.addAction(icon("go-next-view"), tr("Move to device")).triggered.connect(lambda: self.upload_selected(move=True))
            m.addSeparator()
            if len(sel) == 1:
                m.addAction(icon("document-open"), tr("Open")).triggered.connect(self._open_current)
            m.addAction(self.a_cut)
            m.addAction(self.a_copy)
        m.addAction(self.a_paste)
        m.addSeparator()
        if sel:
            m.addAction(self.a_rename)
            m.addAction(self.a_trash)
            m.addAction(self.a_delete)
            m.addAction(self.a_copy_path)
            m.addSeparator()
        m.addAction(self.a_mkdir)
        m.addAction(self.a_refresh)
        m.exec(self.view.viewport().mapToGlobal(pos))

    # ----------------------------------------------------------------- operations
    def new_folder(self) -> None:
        dlg = NameDialog(tr("New folder"), tr("Folder name (in {dir}):").format(dir=self.cwd), "", allow_nested=True, parent=self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        try:
            target = make_dir(self.cwd, dlg.value())
        except (OSError, ValueError) as exc:
            show_error(self, tr("Create folder failed"), str(exc))
            self.ctx.logbook.error("Create local folder failed", op_type="mkdir", destination=os.path.join(self.cwd, dlg.value()),
                                   error=str(exc))
            return
        self.ctx.logbook.info("Local folder created", op_type="mkdir", destination=target, result="success")

    def rename_selected(self) -> None:
        sel = self.selected_paths()
        if len(sel) != 1:
            return
        old = sel[0]
        dlg = NameDialog(tr("Rename"), tr("New name for {name}:").format(name=os.path.basename(old)), os.path.basename(old),
                         existing=set(os.listdir(self.cwd)) if os.path.isdir(self.cwd) else set(), parent=self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        try:
            new = rename_local(old, dlg.value())
        except (OSError, ValueError) as exc:
            show_error(self, tr("Rename failed"), str(exc))
            self.ctx.logbook.error("Local rename failed", op_type="rename", source=old, error=str(exc))
            return
        self.ctx.logbook.info("Local item renamed", op_type="rename", source=old, destination=new, result="success")

    def delete_selected(self, permanent: bool) -> None:
        sel = self.selected_paths()
        if not sel:
            return
        protected = [p for p in sel if is_protected_local(p)]
        if protected:
            show_error(self, tr("Delete refused"), tr("Protected locations cannot be deleted:") + "\n" + "\n".join(protected))
            return
        dirs = [p for p in sel if os.path.isdir(p) and not os.path.islink(p)]
        listing = "\n".join(sel[:15]) + ("\n…" if len(sel) > 15 else "")
        if permanent:
            text = tr("Permanently delete {n} item(s)? This cannot be undone.").format(n=len(sel))
            if dirs:
                text += "\n\n" + tr("{n} folder(s) will be deleted recursively with all their contents.").format(n=len(dirs))
        else:
            text = tr("Move {n} item(s) to the trash?").format(n=len(sel))
        box = QMessageBox(QMessageBox.Icon.Warning, tr("Confirm deletion"), text, parent=self)
        box.setDetailedText(listing)
        ok = box.addButton(tr("Delete permanently") if permanent else tr("Move to trash"), QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not ok:
            self.ctx.logbook.info(f"Local delete cancelled ({len(sel)} item(s))", op_type="delete", extra={"confirmed": False})
            return
        op_id = new_op_id("del")
        log = self.ctx.logbook

        def work() -> list[str]:
            failures = []
            for p in sel:
                try:
                    if permanent:
                        delete_local(p)
                    elif not QFile.moveToTrash(p):
                        raise OSError(tr("The trash is not available for this location"))
                    log.info(f"Local {'deleted' if permanent else 'moved to trash'}: {p}", op_id=op_id, op_type="delete",
                             source=p, result="success", extra={"confirmed": True, "permanent": permanent})
                except (OSError, ValueError) as exc:
                    failures.append(f"{p}: {exc}")
                    log.error(f"Local delete failed: {p}", op_id=op_id, op_type="delete", source=p, error=str(exc),
                              extra={"confirmed": True, "permanent": permanent})
            return failures

        def done(failures: list[str]) -> None:
            if failures:
                show_error(self, tr("Delete finished with problems"),
                           tr("{n} item(s) could not be deleted.").format(n=len(failures)), "\n".join(failures))
            else:
                self.status_message.emit(tr("Deleted {n} item(s)").format(n=len(sel)))

        self.ctx.tasks.submit(work, on_done=done, on_error=lambda e: show_error(self, tr("Delete failed"), str(e)))

    def copy_paths(self) -> None:
        paths = self.selected_paths()
        QGuiApplication.clipboard().setText("\n".join(paths))

    def copy_selected(self, cut: bool) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        self.ctx.clipboard = ClipboardState("local", paths, cut)
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(p) for p in paths])
        md.setText("\n".join(paths))
        QGuiApplication.clipboard().setMimeData(md)
        self.status_message.emit((tr("Cut {n} item(s)") if cut else tr("Copied {n} item(s)")).format(n=len(paths)))

    def paste(self) -> None:
        clip = self.ctx.clipboard
        if clip is None or not self.cwd:
            return
        if clip.kind == "remote":
            self.download_requested.emit(clip.paths, self.cwd, clip.cut)
            if clip.cut:
                self.ctx.clipboard = None
            return
        op_id = new_op_id("mov" if clip.cut else "cpy")
        cwd, log = self.cwd, self.ctx.logbook
        if clip.cut:
            self.ctx.clipboard = None

        def work() -> list[str]:
            problems = []
            for src in clip.paths:
                name = os.path.basename(src.rstrip(os.sep))
                dst = os.path.join(cwd, name)
                if os.path.abspath(src) == os.path.abspath(dst) and clip.cut:
                    continue
                if os.path.lexists(dst):
                    dst = local_unique_path(cwd, name)
                try:
                    if clip.cut:
                        how = move_local(src, dst)
                        log.info(f"Local move ({how}): {name}", op_id=op_id, op_type="move", source=src, destination=dst,
                                 result="success")
                    else:
                        copy_local(src, dst)
                        log.info(f"Local copy: {name}", op_id=op_id, op_type="copy", source=src, destination=dst,
                                 result="success")
                except (OSError, ValueError) as exc:
                    problems.append(f"{src}: {exc}")
                    log.error(f"Local {'move' if clip.cut else 'copy'} failed: {name}", op_id=op_id,
                              op_type="move" if clip.cut else "copy", source=src, destination=dst, error=str(exc))
            return problems

        def done(problems: list[str]) -> None:
            if problems:
                show_error(self, tr("Paste finished with problems"), tr("{n} item(s) failed.").format(n=len(problems)),
                           "\n".join(problems))

        self.status_message.emit(tr("Pasting {n} item(s)…").format(n=len(clip.paths)))
        self.ctx.tasks.submit(work, on_done=done, on_error=lambda e: show_error(self, tr("Paste failed"), str(e)))

    def upload_selected(self, move: bool = False) -> None:
        paths = self.selected_paths()
        if paths:
            self.upload_requested.emit(paths, None, move)

    # ----------------------------------------------------------------- drag & drop
    def _accept_drop(self, md: Any) -> bool:
        return decode_remote_mime(md) is not None

    def _handle_drop(self, md: Any, pos: QPoint, action: Qt.DropAction) -> bool:
        remote = decode_remote_mime(md)
        if remote is None:
            return False
        idx = self.view.indexAt(pos)
        dest = self.cwd
        if idx.isValid() and self.model.isDir(idx.siblingAtColumn(0)):
            dest = self.model.filePath(idx.siblingAtColumn(0))
        self.download_requested.emit(remote[1], dest, False)
        return True
