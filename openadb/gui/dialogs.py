"""Dialogs: network connect, delete confirmation, conflicts, properties, naming, job details."""

from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from openadb.core.capabilities import DeviceCapabilities
from openadb.core.errors import UnsafePathError
from openadb.core.paths import validate_component
from openadb.core.progress import format_bytes
from openadb.core.remote_fs import DeleteInspection, RemoteEntry
from openadb.core.transfer import ConflictAction, ConflictQuery, Direction, TransferJob
from openadb.gui.i18n import tr
from openadb.gui.icons import icon


def show_error(parent: QWidget | None, title: str, text: str, details: str = "") -> None:
    box = QMessageBox(QMessageBox.Icon.Critical, title, text, QMessageBox.StandardButton.Ok, parent)
    if details:
        box.setDetailedText(details)
    box.exec()


def ask_yes_no(parent: QWidget | None, title: str, text: str, default_no: bool = True) -> bool:
    btn = QMessageBox.question(
        parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No if default_no else QMessageBox.StandardButton.Yes,
    )
    return btn == QMessageBox.StandardButton.Yes


class NameDialog(QDialog):
    """Ask for a file/folder name with live validation."""

    def __init__(self, title: str, label: str, initial: str = "", allow_nested: bool = False,
                 existing: set[str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.allow_nested = allow_nested
        self.existing = existing or set()
        self.initial = initial
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(label))
        self.edit = QLineEdit(initial)
        lay.addWidget(self.edit)
        self.msg = QLabel()
        self.msg.setObjectName("warning")
        lay.addWidget(self.msg)
        if allow_nested:
            hint = QLabel(tr("Use '/' to create nested folders, e.g. roms/gba/hacks"))
            hint.setObjectName("hint")
            lay.addWidget(hint)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.edit.textChanged.connect(self._validate)
        stem_end = initial.rfind(".")
        self.edit.setSelection(0, stem_end if stem_end > 0 else len(initial))
        self._validate()
        self.resize(420, self.sizeHint().height())

    def _validate(self) -> None:
        text = self.edit.text()
        error = ""
        parts = [p for p in text.split("/")] if self.allow_nested else [text]
        if self.allow_nested:
            parts = [p for p in parts if p]
        try:
            if not parts:
                raise UnsafePathError(tr("Name is empty"))
            for p in parts:
                validate_component(p)
        except UnsafePathError as exc:
            error = str(exc)
        if not error and text != self.initial and text in self.existing:
            error = tr("An item with this name already exists")
        if not error and text == self.initial and self.initial:
            error = " "
        self.msg.setText(error.strip())
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not error)

    def value(self) -> str:
        return self.edit.text()


class ConnectDialog(QDialog):
    def __init__(self, recent: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Connect network device"))
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Enter the device address (adb connect HOST:PORT).\n"
                                "On Knulli enable network ADB/SSH in the system settings; the default port is 5555.")))
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.addItems(recent)
        self.combo.setCurrentText(recent[0] if recent else "")
        self.combo.lineEdit().setPlaceholderText("192.168.1.50:5555")
        lay.addWidget(self.combo)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Connect"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def target(self) -> str:
        t = self.combo.currentText().strip()
        if t and ":" not in t.rsplit("]", 1)[-1]:
            t += ":5555"
        return t


class DeleteConfirmDialog(QDialog):
    """Lists every target, flags recursive deletion and requires explicit confirmation."""

    def __init__(self, inspections: list[DeleteInspection], location: str, permanent: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Confirm permanent deletion"))
        self.inspections = inspections
        lay = QVBoxLayout(self)
        n = len(inspections)
        head = QLabel(tr("Permanently delete {n} item(s) on {loc}?").format(n=n, loc=location))
        head.setWordWrap(True)
        f = head.font()
        f.setBold(True)
        head.setFont(f)
        lay.addWidget(head)
        if permanent:
            warn = QLabel(tr("This cannot be undone: the device has no recycle bin."))
            warn.setObjectName("warning")
            lay.addWidget(warn)
        table = QTableWidget(n, 3)
        table.setHorizontalHeaderLabels([tr("Path"), tr("Type"), tr("Will remove")])
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.recursive_dirs = [i for i in inspections if i.kind == "dir" and i.non_empty]
        for row, ins in enumerate(inspections):
            kind = {"dir": tr("Folder"), "file": tr("File"), "symlink": tr("Symbolic link"), "missing": tr("Missing"),
                    "other": tr("Special file")}.get(ins.kind, ins.kind)
            if ins.kind == "dir" and ins.non_empty:
                what = (tr("folder and ~{n} items inside (recursive)").format(n=ins.item_count)
                        if ins.item_count is not None else tr("folder and all its contents (recursive)"))
            elif ins.kind == "dir":
                what = tr("empty folder")
            elif ins.kind == "symlink":
                what = tr("only the link (target untouched)")
            elif ins.kind == "missing":
                what = tr("nothing (already gone)")
            else:
                what = tr("the file")
            for col, text in enumerate((ins.path, kind, what)):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                table.setItem(row, col, item)
        table.setMinimumHeight(min(320, 60 + 26 * n))
        lay.addWidget(table)
        self.recursive_box: QCheckBox | None = None
        if self.recursive_dirs:
            self.recursive_box = QCheckBox(
                tr("I understand that {n} folder(s) and everything inside them will be deleted recursively").format(
                    n=len(self.recursive_dirs))
            )
            self.recursive_box.setObjectName("warning")
            lay.addWidget(self.recursive_box)
        bb = QDialogButtonBox()
        self.delete_btn = bb.addButton(tr("Delete permanently"), QDialogButtonBox.ButtonRole.DestructiveRole)
        self.delete_btn.setIcon(icon("edit-delete"))
        cancel = bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setDefault(True)
        cancel.setFocus()
        self.delete_btn.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        lay.addWidget(bb)
        if self.recursive_box is not None:
            self.delete_btn.setEnabled(False)
            self.recursive_box.toggled.connect(self.delete_btn.setEnabled)
        self.resize(720, self.sizeHint().height())

    @property
    def recursive(self) -> bool:
        return bool(self.recursive_box and self.recursive_box.isChecked())


class ConflictDialog(QDialog):
    def __init__(self, query: ConflictQuery, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("File already exists"))
        self.action = ConflictAction.SKIP
        item, job = query.item, query.job
        where = tr("on the device") if job.direction == Direction.UPLOAD else tr("on this computer")
        lay = QVBoxLayout(self)
        lbl = QLabel(tr("<b>{name}</b> already exists {where}.").format(name=os.path.basename(item.destination.rstrip('/')),
                                                                        where=where))
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(lbl)
        grid = QGridLayout()
        grid.addWidget(QLabel(tr("Existing:")), 0, 0)
        grid.addWidget(QLabel(f"{item.destination}  ({format_bytes(query.existing_size)})"), 0, 1)
        grid.addWidget(QLabel(tr("Incoming:")), 1, 0)
        grid.addWidget(QLabel(f"{item.source}  ({format_bytes(item.size)})"), 1, 1)
        lay.addLayout(grid)
        self.apply_all = QCheckBox(tr("Apply to all remaining conflicts in this transfer"))
        lay.addWidget(self.apply_all)
        row = QHBoxLayout()
        for action, text in (
            (ConflictAction.SKIP, tr("Skip")),
            (ConflictAction.RENAME, tr("Keep both (rename incoming)")),
            (ConflictAction.BACKUP, tr("Back up && replace")),
            (ConflictAction.OVERWRITE, tr("Replace")),
            (ConflictAction.CANCEL, tr("Cancel transfer")),
        ):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, a=action: self._choose(a))
            if action == ConflictAction.SKIP:
                b.setDefault(True)
            row.addWidget(b)
        lay.addLayout(row)

    def _choose(self, action: ConflictAction) -> None:
        self.action = action
        self.accept()

    def reject(self) -> None:
        self.action = ConflictAction.SKIP
        super().reject()


class PropertiesDialog(QDialog):
    def __init__(self, entry: RemoteEntry, serial: str, parent: QWidget | None = None, size_callback=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.setWindowTitle(tr("Properties — {name}").format(name=entry.name))
        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        def add(label: str, value: str) -> QLabel:
            w = QLabel(value)
            w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            w.setWordWrap(True)
            form.addRow(label, w)
            return w

        add(tr("Name:"), entry.name)
        add(tr("Path:"), entry.path)
        add(tr("Device:"), serial)
        add(tr("Type:"), entry.type_label)
        if entry.is_link:
            add(tr("Link target:"), f"{entry.link_target} ({entry.link_target_kind})")
        size_text = format_bytes(entry.size) + (f" ({entry.size:,} bytes)" if entry.size is not None else "")
        self.size_label = add(tr("Size:"), size_text if not entry.is_dir else tr("(folder) — click Calculate"))
        add(tr("Permissions:"), f"{entry.permissions}  ({oct(entry.mode & 0o7777) if entry.mode is not None else '?'})")
        add(tr("Owner:"), f"{entry.owner or '?'} (uid {entry.uid if entry.uid is not None else '?'})")
        add(tr("Group:"), f"{entry.group or '?'} (gid {entry.gid if entry.gid is not None else '?'})")
        add(tr("Modified:"), entry.mtime_dt.strftime("%Y-%m-%d %H:%M:%S") if entry.mtime_dt else "?")
        if entry.nlink is not None:
            add(tr("Hard links:"), str(entry.nlink))
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if entry.is_dir and size_callback is not None:
            calc = bb.addButton(tr("Calculate size"), QDialogButtonBox.ButtonRole.ActionRole)

            def run() -> None:
                calc.setEnabled(False)
                self.size_label.setText(tr("Calculating…"))
                size_callback(lambda n: self.size_label.setText(
                    tr("{size} on disk (du)").format(size=format_bytes(n)) if n is not None else tr("unavailable")))

            calc.clicked.connect(run)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
        self.resize(520, self.sizeHint().height())


class CapabilitiesDialog(QDialog):
    def __init__(self, caps: DeviceCapabilities, adb_attrs: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Device information — {serial}").format(serial=caps.serial))
        lay = QVBoxLayout(self)
        form = QFormLayout()
        rows = [
            (tr("Serial"), caps.serial),
            (tr("Classification"), caps.device_class.value),
            (tr("Operating system"), caps.os_pretty),
            (tr("Kernel"), caps.kernel),
            (tr("uname -a"), caps.uname_a),
            (tr("Knulli detected"), tr("yes") if caps.knulli else tr("no")),
            (tr("Android getprop"), (caps.android_model or tr("available")) if caps.has_getprop else tr("not available")),
            (tr("Shell transport"), f"{caps.transport} ({tr('binary-safe') if caps.binary_safe else tr('PTY/CRLF translated')})"),
            (tr("Listing strategy"), caps.listing_strategy),
            (tr("Hash tool"), caps.hash_command[0] if caps.hash_command else tr("none")),
            (tr("Remote uid"), caps.uid),
            (tr("Utilities"), ", ".join(sorted(caps.commands))),
            (tr("ADB-reported metadata"), " ".join(f"{k}:{v}" for k, v in adb_attrs.items()) +
             ("\n" + tr("(informational only — not used to identify the device)") if adb_attrs else "")),
            (tr("Notes"), "\n".join(caps.notes) or "—"),
            (tr("Probe duration"), f"{caps.probe_duration:.2f}s"),
        ]
        for k, v in rows:
            lab = QLabel(v)
            lab.setWordWrap(True)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(k + ":", lab)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(640, self.sizeHint().height())


class JobDetailsDialog(QDialog):
    def __init__(self, job: TransferJob, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Transfer details — {title}").format(title=job.title))
        lay = QVBoxLayout(self)
        info = QLabel(
            f"<b>{job.status.value}</b> · {job.summary_text()}<br>"
            f"{tr('Device')}: {job.serial} · {tr('Destination')}: {job.dest_dir}<br>"
            f"{tr('Overwrite')}: {job.overwrite} · {tr('Verification')}: {job.verify.label}"
            + (f"<br>{tr('Started')}: {datetime.fromtimestamp(job.started):%Y-%m-%d %H:%M:%S}" if job.started else "")
            + (f"<br><span style='color:#c0392b'>{job.error}</span>" if job.error else "")
        )
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(info)
        files = [i for i in job.items if i.kind == "file"]
        table = QTableWidget(len(files), 6)
        table.setHorizontalHeaderLabels([tr("File"), tr("Size"), tr("Status"), tr("Verification"), tr("Destination"), tr("Error")])
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for r, it in enumerate(files):
            ver = ""
            if it.verify:
                ver = {True: "✔ ", False: "✘ ", None: "? "}[it.verify.ok] + f"{it.verify.method}: {it.verify.detail}"
            for c, text in enumerate((it.rel, format_bytes(it.size), it.status.value + (f" ({it.action})" if it.action else ""),
                                      ver, it.destination, it.error)):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                table.setItem(r, c, cell)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.resizeColumnsToContents()
        lay.addWidget(table)
        if not files and not job.items:
            lay.addWidget(QLabel(tr("Per-file details are available for transfers run in this session.")))
        if job.warnings or job.partial_paths:
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText("\n".join(job.warnings + [tr("Possible partial file: ") + p for p in job.partial_paths]))
            text.setMaximumHeight(120)
            lay.addWidget(text)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(900, 520)
