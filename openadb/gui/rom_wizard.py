"""ROM / BIOS upload workflow: choose files → system → destination checks → queued, verified upload."""

from __future__ import annotations

import os
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from openadb.core.errors import OpenADBError, UnsafePathError
from openadb.core.knulli import RomSystem, resolve_rom_destination, suggest_system
from openadb.core.paths import normalize_remote
from openadb.core.progress import format_bytes
from openadb.core.transfer import Direction, TransferJob
from openadb.core.verify import VerifyMode
from openadb.gui.context import AppContext
from openadb.gui.i18n import tr

CUSTOM = "__custom__"


@dataclass
class DestinationCheck:
    path: str
    exists: bool
    is_dir: bool
    free: int | None
    conflicts: list[str]


class RomUploadDialog(QDialog):
    def __init__(self, ctx: AppContext, serial: str, mode: str = "rom", files: list[str] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx, self.serial, self.mode = ctx, serial, mode
        self.check: DestinationCheck | None = None
        self._check_gen = 0
        prof = ctx.settings.profile(serial)
        self.rom_root = prof.rom_root or "/userdata/roms"
        self.bios_dir = prof.bios_dir or "/userdata/bios"
        self.setWindowTitle(tr("Upload ROMs") if mode == "rom" else tr("Upload BIOS files"))
        lay = QVBoxLayout(self)

        # 1. files
        box = QGroupBox(tr("1. Files"))
        bl = QVBoxLayout(box)
        self.files = QListWidget()
        self.files.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        bl.addWidget(self.files)
        row = QHBoxLayout()
        for text, slot in ((tr("Add files…"), self.add_files), (tr("Add folder…"), self.add_folder),
                           (tr("Remove"), self.remove_files)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        self.total_label = QLabel()
        self.total_label.setObjectName("hint")
        row.addWidget(self.total_label)
        bl.addLayout(row)
        lay.addWidget(box)

        # 2. target
        box2 = QGroupBox(tr("2. Destination") if mode == "bios" else tr("2. Target system"))
        fl = QFormLayout(box2)
        self.system = QComboBox()
        if mode == "rom":
            for s in ctx.settings.rom_systems:
                self.system.addItem(f"{s.name}  ({s.folder})", s.key)
            self.system.addItem(tr("Custom folder…"), CUSTOM)
            fl.addRow(tr("System:"), self.system)
            self.suggest = QLabel()
            self.suggest.setObjectName("hint")
            fl.addRow("", self.suggest)
        self.custom = QLineEdit(self.bios_dir if mode == "bios" else self.rom_root + "/")
        fl.addRow(tr("Folder:") if mode == "bios" else tr("Custom folder:"), self.custom)
        self.dest_label = QLabel()
        self.dest_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f = self.dest_label.font()
        f.setBold(True)
        self.dest_label.setFont(f)
        fl.addRow(tr("Destination:"), self.dest_label)
        self.check_label = QLabel()
        self.check_label.setWordWrap(True)
        fl.addRow(tr("Checks:"), self.check_label)
        self.create_dir = QCheckBox(tr("Create the destination folder (it does not exist yet)"))
        self.create_dir.setVisible(False)
        self.create_dir.toggled.connect(self._update_ok)
        fl.addRow("", self.create_dir)
        lay.addWidget(box2)

        # 3. options
        box3 = QGroupBox(tr("3. Options"))
        ol = QFormLayout(box3)
        self.policy = QComboBox()
        for key, label in (("skip", tr("Skip files that already exist")), ("ask", tr("Ask for each conflict")),
                           ("rename", tr("Keep both (rename incoming)")), ("backup", tr("Back up then replace")),
                           ("overwrite", tr("Replace existing files"))):
            self.policy.addItem(label, key)
        self.policy.setCurrentIndex(0)
        ol.addRow(tr("Existing files:"), self.policy)
        self.verify = QCheckBox(tr("High-integrity mode: verify every file with SHA-256/MD5 after upload"))
        self.verify.setChecked(ctx.settings.high_integrity_roms)
        ol.addRow("", self.verify)
        lay.addWidget(box3)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.start_btn = self.buttons.addButton(tr("Upload"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        if mode == "rom":
            self.system.currentIndexChanged.connect(self._target_changed)
        self.custom.editingFinished.connect(self._target_changed)
        for p in files or []:
            self._add(p)
        self._target_changed()
        self.resize(720, 640)

    # ----------------------------------------------------------------- files
    def file_paths(self) -> list[str]:
        return [self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count())]

    def _add(self, path: str) -> None:
        if path in self.file_paths():
            return
        size = os.path.getsize(path) if os.path.isfile(path) else None
        label = os.path.basename(path.rstrip(os.sep)) + ("/" if os.path.isdir(path) else f"  ({format_bytes(size)})")
        self.files.addItem(label)
        item = self.files.item(self.files.count() - 1)
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)

    def add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Choose files"), self.ctx.settings.local_home())
        for p in paths:
            self._add(p)
        self._files_changed()

    def add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("Choose folder"), self.ctx.settings.local_home())
        if d:
            self._add(d)
            self._files_changed()

    def remove_files(self) -> None:
        for item in self.files.selectedItems():
            self.files.takeItem(self.files.row(item))
        self._files_changed()

    def _local_total(self) -> int:
        total = 0
        for p in self.file_paths():
            if os.path.isfile(p):
                total += os.path.getsize(p)
            else:
                for base, _d, names in os.walk(p):
                    total += sum(os.path.getsize(os.path.join(base, n)) for n in names
                                 if os.path.isfile(os.path.join(base, n)) and not os.path.islink(os.path.join(base, n)))
        return total

    def _files_changed(self) -> None:
        paths = self.file_paths()
        self.total_label.setText(tr("{n} item(s), {size}").format(n=len(paths), size=format_bytes(self._local_total())))
        if self.mode == "rom" and paths:
            systems = self.ctx.settings.rom_systems
            suggestions = {s.key for s in (suggest_system(p, systems) for p in paths if os.path.isfile(p)) if s}
            if len(suggestions) == 1:
                key = suggestions.pop()
                idx = self.system.findData(key)
                if idx >= 0 and self.system.currentIndex() != idx:
                    self.system.setCurrentIndex(idx)
                self.suggest.setText(tr("Suggested from the file extension"))
            elif suggestions:
                self.suggest.setText(tr("Files match several systems; choose one"))
            else:
                self.suggest.setText("")
        self._target_changed()

    # ----------------------------------------------------------------- destination
    def selected_system(self) -> RomSystem | None:
        key = self.system.currentData() if self.mode == "rom" else None
        return next((s for s in self.ctx.settings.rom_systems if s.key == key), None)

    def destination(self) -> str | None:
        try:
            if self.mode == "rom" and self.system.currentData() != CUSTOM:
                system = self.selected_system()
                return resolve_rom_destination(self.rom_root, system) if system else None
            return normalize_remote(self.custom.text().strip())
        except UnsafePathError:
            return None

    def _target_changed(self) -> None:
        if self.mode == "rom":
            self.custom.setEnabled(self.system.currentData() == CUSTOM)
        dest = self.destination()
        self.dest_label.setText(dest or tr("(invalid path)"))
        self.check = None
        self.create_dir.setVisible(False)
        self.create_dir.setChecked(False)
        self._update_ok()
        if not dest:
            return
        self._check_gen += 1
        gen = self._check_gen
        names = [os.path.basename(p.rstrip(os.sep)) for p in self.file_paths()]
        self.check_label.setText(tr("Checking destination…"))

        def work() -> DestinationCheck:
            rfs = self.ctx.rfs_for(self.serial)
            entry = rfs.stat(dest)
            exists = entry is not None
            is_dir = bool(entry and entry.navigable)
            conflicts: list[str] = []
            if is_dir:
                existing = {e.name for e in rfs.list_dir(dest)}
                conflicts = [n for n in names if n in existing]
            probe = dest
            space = None
            while space is None:
                try:
                    space = rfs.disk_space(probe)
                except OpenADBError:
                    space = None
                if probe == "/" or space is not None:
                    break
                probe = os.path.dirname(probe) or "/"
            return DestinationCheck(dest, exists, is_dir, space.available if space else None, conflicts)

        def done(chk: DestinationCheck) -> None:
            if gen != self._check_gen:
                return
            self.check = chk
            lines = []
            if chk.exists and not chk.is_dir:
                lines.append("✘ " + tr("A file with this name exists; choose another folder"))
            elif chk.exists:
                lines.append("✔ " + tr("Folder exists"))
            else:
                lines.append("⚠ " + tr("Folder does not exist"))
                self.create_dir.setVisible(True)
            need = self._local_total()
            if chk.free is None:
                lines.append("⚠ " + tr("Free space unknown"))
            elif chk.free < need:
                lines.append("✘ " + tr("Not enough space: {need} needed, {free} free").format(need=format_bytes(need),
                                                                                             free=format_bytes(chk.free)))
            else:
                lines.append("✔ " + tr("{free} free ({need} needed)").format(free=format_bytes(chk.free),
                                                                           need=format_bytes(need)))
            if chk.conflicts:
                lines.append("⚠ " + tr("{n} item(s) already exist: {names}").format(
                    n=len(chk.conflicts), names=", ".join(chk.conflicts[:5]) + ("…" if len(chk.conflicts) > 5 else "")))
            elif chk.is_dir:
                lines.append("✔ " + tr("No name conflicts"))
            self.check_label.setText("\n".join(lines))
            self._update_ok()

        def fail(exc: BaseException) -> None:
            if gen == self._check_gen:
                self.check_label.setText("✘ " + str(exc))

        self.ctx.tasks.submit(work, on_done=done, on_error=fail)

    def _update_ok(self) -> None:
        chk = self.check
        ok = bool(self.file_paths()) and chk is not None
        if chk is not None:
            if chk.exists and not chk.is_dir:
                ok = False
            if not chk.exists and not self.create_dir.isChecked():
                ok = False
            if chk.free is not None and chk.free < self._local_total():
                ok = False
        self.start_btn.setEnabled(ok)

    # ----------------------------------------------------------------- result
    def build_job(self) -> TransferJob:
        dest = self.destination()
        assert dest
        system = self.selected_system()
        label_target = system.name if system else dest
        verb = tr("ROMs") if self.mode == "rom" else tr("BIOS")
        return TransferJob(
            serial=self.serial,
            direction=Direction.UPLOAD,
            sources=self.file_paths(),
            dest_dir=dest,
            overwrite=self.policy.currentData(),
            verify=VerifyMode.HASH if self.verify.isChecked() else VerifyMode.SIZE,
            label=f"{verb} → {label_target} ({len(self.file_paths())} item(s))",
        )
