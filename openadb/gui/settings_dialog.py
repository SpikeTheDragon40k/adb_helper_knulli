"""Settings dialog."""

from __future__ import annotations

import copy

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from openadb.core.adb import AdbClient
from openadb.core.config import DeviceProfile, Settings
from openadb.core.errors import OpenADBError
from openadb.core.knulli import RomSystem, default_rom_systems
from openadb.core.verify import VerifyMode
from openadb.gui.dialogs import show_error
from openadb.gui.i18n import tr

LANGUAGES = (("system", "System default"), ("en", "English"), ("it", "Italiano"), ("de", "Deutsch"), ("fr", "Français"),
             ("es", "Español"), ("pt_BR", "Português (Brasil)"), ("ja", "日本語"))


def _browse_dir(parent: QWidget, edit: QLineEdit) -> None:
    d = QFileDialog.getExistingDirectory(parent, tr("Choose folder"), edit.text() or "")
    if d:
        edit.setText(d)


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Settings"))
        self.settings = copy.deepcopy(settings)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        tabs.addTab(self._general_tab(), tr("ADB && devices"))
        tabs.addTab(self._paths_tab(), tr("Folders"))
        tabs.addTab(self._transfers_tab(), tr("Transfers"))
        tabs.addTab(self._logging_tab(), tr("Logging"))
        tabs.addTab(self._appearance_tab(), tr("Appearance"))
        tabs.addTab(self._profiles_tab(), tr("Device profiles"))
        tabs.addTab(self._roms_tab(), tr("ROM systems"))
        tabs.addTab(self._safety_tab(), tr("Safety"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(760, 560)

    # ----------------------------------------------------------------- tabs
    def _general_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        s = self.settings
        row = QHBoxLayout()
        self.adb_path = QLineEdit(s.adb_path)
        self.adb_path.setPlaceholderText(tr("auto-detect from PATH (e.g. /usr/bin/adb)"))
        browse = QPushButton(tr("Browse…"))
        browse.clicked.connect(self._browse_adb)
        test = QPushButton(tr("Test"))
        test.clicked.connect(self._test_adb)
        row.addWidget(self.adb_path, 1)
        row.addWidget(browse)
        row.addWidget(test)
        f.addRow(tr("ADB executable:"), row)
        self.adb_status = QLabel()
        self.adb_status.setObjectName("hint")
        self.adb_status.setWordWrap(True)
        f.addRow("", self.adb_status)
        self.host = QLineEdit(s.server_host)
        f.addRow(tr("ADB server host:"), self.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(s.server_port)
        f.addRow(tr("ADB server port:"), self.port)
        self.auto_refresh = QCheckBox(tr("Detect connected/disconnected devices automatically"))
        self.auto_refresh.setChecked(s.auto_refresh)
        f.addRow("", self.auto_refresh)
        self.refresh = QDoubleSpinBox()
        self.refresh.setRange(1, 600)
        self.refresh.setSuffix(" s")
        self.refresh.setValue(s.refresh_interval)
        f.addRow(tr("Refresh interval:"), self.refresh)
        self.cmd_timeout = QDoubleSpinBox()
        self.cmd_timeout.setRange(5, 3600)
        self.cmd_timeout.setSuffix(" s")
        self.cmd_timeout.setValue(s.command_timeout)
        f.addRow(tr("Remote command timeout:"), self.cmd_timeout)
        self.console_timeout = QDoubleSpinBox()
        self.console_timeout.setRange(1, 3600)
        self.console_timeout.setSuffix(" s")
        self.console_timeout.setValue(s.console_timeout)
        f.addRow(tr("Default console timeout:"), self.console_timeout)
        return w

    def _paths_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        row = QHBoxLayout()
        self.local_dir = QLineEdit(self.settings.default_local_dir)
        self.local_dir.setPlaceholderText(tr("home folder"))
        b = QPushButton(tr("Browse…"))
        b.clicked.connect(lambda: _browse_dir(self, self.local_dir))
        row.addWidget(self.local_dir, 1)
        row.addWidget(b)
        f.addRow(tr("Default local folder:"), row)
        self.remote_dir = QLineEdit(self.settings.default_remote_dir)
        f.addRow(tr("Default remote folder:"), self.remote_dir)
        hint = QLabel(tr("Per-device defaults can be set in Device profiles."))
        hint.setObjectName("hint")
        f.addRow("", hint)
        self.show_hidden = QCheckBox(tr("Show hidden files by default"))
        self.show_hidden.setChecked(self.settings.show_hidden)
        f.addRow("", self.show_hidden)
        return w

    def _transfers_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        s = self.settings
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 4)
        self.concurrency.setValue(s.concurrency)
        f.addRow(tr("Concurrent transfers:"), self.concurrency)
        hint = QLabel(tr("1 (sequential) is recommended for USB 2.0 handhelds."))
        hint.setObjectName("hint")
        f.addRow("", hint)
        self.verify = QComboBox()
        for m in VerifyMode:
            self.verify.addItem(tr(m.label), m.value)
        self.verify.setCurrentIndex(max(0, self.verify.findData(s.verify_mode)))
        f.addRow(tr("Verification:"), self.verify)
        self.overwrite = QComboBox()
        for key, label in (("ask", tr("Ask")), ("skip", tr("Skip existing")), ("rename", tr("Keep both (rename incoming)")),
                           ("backup", tr("Back up then replace")), ("overwrite", tr("Replace"))):
            self.overwrite.addItem(label, key)
        self.overwrite.setCurrentIndex(max(0, self.overwrite.findData(s.overwrite_policy)))
        f.addRow(tr("When a file exists:"), self.overwrite)
        self.high_integrity = QCheckBox(tr("High-integrity mode for ROM uploads (hash-verify every file)"))
        self.high_integrity.setChecked(s.high_integrity_roms)
        f.addRow("", self.high_integrity)
        return w

    def _logging_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_level.setCurrentText(self.settings.log_level.upper())
        f.addRow(tr("Log verbosity:"), self.log_level)
        self.retention = QSpinBox()
        self.retention.setRange(1, 3650)
        self.retention.setSuffix(tr(" days"))
        self.retention.setValue(self.settings.log_retention_days)
        f.addRow(tr("Keep rotated logs for:"), self.retention)
        self.log_mb = QSpinBox()
        self.log_mb.setRange(1, 500)
        self.log_mb.setSuffix(" MiB")
        self.log_mb.setValue(self.settings.log_max_mb)
        f.addRow(tr("Rotate log file at:"), self.log_mb)
        from openadb.core.paths import log_dir

        loc = QLabel(str(log_dir()))
        loc.setObjectName("hint")
        f.addRow(tr("Log folder:"), loc)
        return w

    def _appearance_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.theme = QComboBox()
        for key, label in (("system", tr("System (follow desktop)")), ("light", tr("Light")), ("dark", tr("Dark"))):
            self.theme.addItem(label, key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(self.settings.theme)))
        f.addRow(tr("Theme:"), self.theme)
        self.language = QComboBox()
        for key, label in LANGUAGES:
            self.language.addItem(label, key)
        self.language.setCurrentIndex(max(0, self.language.findData(self.settings.language)))
        f.addRow(tr("Language:"), self.language)
        hint = QLabel(tr("The interface is translation-ready; languages without a translation file fall back to English. "
                         "Language changes apply after restart."))
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        f.addRow("", hint)
        return w

    def _profiles_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(tr("Saved device profiles (created automatically when a device is first seen).")))
        self.profiles = QTableWidget(0, 6)
        self.profiles.setHorizontalHeaderLabels([tr("Serial"), tr("Name"), tr("Default remote folder"), tr("Knulli profile"),
                                                 tr("ROM root"), tr("BIOS folder")])
        self.profiles.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.profiles.verticalHeader().hide()
        for prof in self.settings.device_profiles.values():
            self._add_profile_row(prof)
        v.addWidget(self.profiles)
        rm = QPushButton(tr("Forget selected profile"))
        rm.clicked.connect(lambda: self.profiles.removeRow(self.profiles.currentRow()) if self.profiles.currentRow() >= 0 else None)
        v.addWidget(rm)
        return w

    def _add_profile_row(self, prof: DeviceProfile) -> None:
        r = self.profiles.rowCount()
        self.profiles.insertRow(r)
        serial = QTableWidgetItem(prof.serial)
        from PySide6.QtCore import Qt

        serial.setFlags(serial.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.profiles.setItem(r, 0, serial)
        self.profiles.setItem(r, 1, QTableWidgetItem(prof.alias))
        self.profiles.setItem(r, 2, QTableWidgetItem(prof.default_remote_dir))
        combo = QComboBox()
        for key, label in (("auto", tr("Auto-detect")), ("on", tr("Always")), ("off", tr("Never"))):
            combo.addItem(label, key)
        combo.setCurrentIndex(max(0, combo.findData(prof.knulli_mode)))
        self.profiles.setCellWidget(r, 3, combo)
        self.profiles.setItem(r, 4, QTableWidgetItem(prof.rom_root))
        self.profiles.setItem(r, 5, QTableWidgetItem(prof.bios_dir))

    def _roms_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        lab = QLabel(tr("System → folder mappings used by the ROM upload workflow. Folders are relative to the device's "
                        "ROM root. Extensions (comma-separated) are only used to suggest a system."))
        lab.setWordWrap(True)
        v.addWidget(lab)
        self.roms = QTableWidget(0, 4)
        self.roms.setHorizontalHeaderLabels([tr("Key"), tr("Name"), tr("Folder"), tr("Extensions")])
        self.roms.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.roms.verticalHeader().hide()
        self._fill_roms(self.settings.rom_systems)
        v.addWidget(self.roms)
        row = QHBoxLayout()
        add = QPushButton(tr("Add system"))
        add.clicked.connect(lambda: self._fill_roms([RomSystem("new", "New system", "new", [])], append=True))
        rm = QPushButton(tr("Remove selected"))
        rm.clicked.connect(lambda: self.roms.removeRow(self.roms.currentRow()) if self.roms.currentRow() >= 0 else None)
        reset = QPushButton(tr("Restore defaults"))
        reset.clicked.connect(lambda: self._fill_roms(default_rom_systems()))
        for b in (add, rm, reset):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return w

    def _fill_roms(self, systems: list[RomSystem], append: bool = False) -> None:
        if not append:
            self.roms.setRowCount(0)
        for s in systems:
            r = self.roms.rowCount()
            self.roms.insertRow(r)
            for c, text in enumerate((s.key, s.name, s.folder, ", ".join(s.extensions))):
                self.roms.setItem(r, c, QTableWidgetItem(text))

    def _safety_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.confirm_console = QCheckBox(tr("Ask before running potentially destructive console commands"))
        self.confirm_console.setChecked(self.settings.confirm_console_destructive)
        f.addRow("", self.confirm_console)
        self.protected = QPlainTextEdit("\n".join(self.settings.extra_protected_paths))
        self.protected.setPlaceholderText("/userdata/roms/favourites")
        f.addRow(tr("Extra protected remote paths\n(one per line):"), self.protected)
        hint = QLabel(tr("Always protected: /, every top-level folder (e.g. /userdata, /bin, /etc, /usr) and "
                         "/userdata/roms, /userdata/bios, /userdata/saves, /userdata/system."))
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        f.addRow("", hint)
        return w

    # ----------------------------------------------------------------- adb
    def _browse_adb(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Choose adb executable"), self.adb_path.text() or "/usr/bin")
        if path:
            self.adb_path.setText(path)

    def _test_adb(self) -> None:
        try:
            client = AdbClient(self.adb_path.text().strip() or None, self.host.text().strip(), self.port.value())
            v = client.version()
            self.adb_status.setText(tr("OK: {exe} — {ver}").format(exe=client.executable, ver=v.summary))
        except OpenADBError as exc:
            self.adb_status.setText(tr("Error: {err}").format(err=exc))

    # ----------------------------------------------------------------- accept
    def _accept(self) -> None:
        s = self.settings
        s.adb_path = self.adb_path.text().strip()
        s.server_host = self.host.text().strip() or "127.0.0.1"
        s.server_port = self.port.value()
        s.auto_refresh = self.auto_refresh.isChecked()
        s.refresh_interval = self.refresh.value()
        s.command_timeout = self.cmd_timeout.value()
        s.console_timeout = self.console_timeout.value()
        s.default_local_dir = self.local_dir.text().strip()
        remote = self.remote_dir.text().strip() or "/userdata"
        if not remote.startswith("/"):
            show_error(self, tr("Settings"), tr("The default remote folder must be an absolute path."))
            return
        s.default_remote_dir = remote
        s.show_hidden = self.show_hidden.isChecked()
        s.concurrency = self.concurrency.value()
        s.verify_mode = self.verify.currentData()
        s.overwrite_policy = self.overwrite.currentData()
        s.high_integrity_roms = self.high_integrity.isChecked()
        s.log_level = self.log_level.currentText()
        s.log_retention_days = self.retention.value()
        s.log_max_mb = self.log_mb.value()
        s.theme = self.theme.currentData()
        s.language = self.language.currentData()
        s.confirm_console_destructive = self.confirm_console.isChecked()
        s.extra_protected_paths = [p.strip() for p in self.protected.toPlainText().splitlines() if p.strip().startswith("/")]
        profiles: dict[str, DeviceProfile] = {}
        for r in range(self.profiles.rowCount()):
            serial = self.profiles.item(r, 0).text()
            old = self.settings.device_profiles.get(serial, DeviceProfile(serial))
            text = lambda c: (self.profiles.item(r, c).text().strip() if self.profiles.item(r, c) else "")  # noqa: E731
            combo = self.profiles.cellWidget(r, 3)
            profiles[serial] = DeviceProfile(
                serial=serial, alias=text(1), default_remote_dir=text(2) if text(2).startswith("/") else "",
                knulli_mode=combo.currentData() if isinstance(combo, QComboBox) else "auto",
                bookmarks=old.bookmarks, rom_root=text(4) or "/userdata/roms", bios_dir=text(5) or "/userdata/bios",
                last_host=old.last_host,
            )
        s.device_profiles = profiles
        systems = []
        for r in range(self.roms.rowCount()):
            vals = [(self.roms.item(r, c).text().strip() if self.roms.item(r, c) else "") for c in range(4)]
            if not vals[0] or not vals[2] or ".." in vals[2].split("/") or vals[2].startswith("/"):
                show_error(self, tr("Settings"), tr("ROM system row {n}: key and a relative folder are required.").format(n=r + 1))
                return
            exts = [("." + e.strip().lstrip(".")).lower() for e in vals[3].split(",") if e.strip()]
            systems.append(RomSystem(vals[0], vals[1] or vals[0], vals[2], exts))
        s.rom_systems = systems
        s.sanitize()
        self.accept()
