"""Main window: device selector, dual panes, bottom panels and menus."""

from __future__ import annotations

import os

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from openadb import APP_NAME, __version__
from openadb.core.devices import DeviceInfo, DeviceState
from openadb.core.paths import is_remote_within, normalize_remote
from openadb.core.progress import format_bytes
from openadb.core.transfer import Direction, JobStatus, TransferJob
from openadb.core.verify import VerifyMode
from openadb.gui.console_view import ConsoleView
from openadb.gui.context import AppContext, ConflictRequest
from openadb.gui.dialogs import CapabilitiesDialog, ConflictDialog, ConnectDialog, ask_yes_no, show_error
from openadb.gui.i18n import tr
from openadb.gui.icons import app_icon, icon
from openadb.gui.local_pane import LocalPane
from openadb.gui.logs_view import LogsView
from openadb.gui.remote_pane import RemotePane
from openadb.gui.rom_wizard import RomUploadDialog
from openadb.gui.settings_dialog import SettingsDialog
from openadb.gui.theme import apply_theme
from openadb.gui.transfers_view import TransfersView

STATE_LABELS = {
    DeviceState.DEVICE: "",
    DeviceState.OFFLINE: "offline",
    DeviceState.UNAUTHORIZED: "unauthorized — accept the prompt on the device",
    DeviceState.NO_PERMISSIONS: "no USB permission — check udev rules",
    DeviceState.AUTHORIZING: "authorizing…",
    DeviceState.RECOVERY: "recovery",
}


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.serial: str | None = None
        self._tracked_rom_jobs: set[str] = set()
        self._closing = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1400, 880)

        self.local = LocalPane(ctx)
        self.remote = RemotePane(ctx)
        self.transfers = TransfersView(ctx)
        self.logs = LogsView(ctx)
        self.console = ConsoleView(ctx)

        panes = QSplitter(Qt.Orientation.Horizontal)
        panes.addWidget(self._wrap(self.local))
        panes.addWidget(self._wrap(self.remote))
        panes.setSizes([650, 750])
        self.bottom = QTabWidget()
        self.bottom.addTab(self.transfers, icon("go-next-view"), tr("Transfers"))
        self.bottom.addTab(self.logs, icon("dialog-information"), tr("Activity && Logs"))
        self.bottom.addTab(self.console, icon("utilities-terminal"), tr("Console"))
        self.vsplit = QSplitter(Qt.Orientation.Vertical)
        self.vsplit.addWidget(panes)
        self.vsplit.addWidget(self.bottom)
        self.vsplit.setSizes([560, 320])
        self.vsplit.setCollapsible(0, False)
        self.setCentralWidget(self.vsplit)

        self._build_toolbar()
        self._build_menus()
        self._build_status()

        # wiring
        ctx.devices_changed.connect(self._devices_changed)
        ctx.device_ready.connect(self._device_ready)
        ctx.device_lost.connect(self._device_lost)
        ctx.adb_error.connect(lambda m: self.statusBar().showMessage(m, 8000))
        ctx.bridge.conflict_requested.connect(self._resolve_conflict)
        ctx.bridge.job_changed.connect(self._job_changed)
        self.local.upload_requested.connect(self._upload_from_local)
        self.local.download_requested.connect(self.enqueue_download)
        self.remote.upload_requested.connect(self.enqueue_upload)
        self.remote.download_requested.connect(self._download_from_remote)
        for pane in (self.local, self.remote):
            pane.status_message.connect(lambda m: self.statusBar().showMessage(m, 5000))
        ctx.settings_changed.connect(self._settings_changed)

        if ctx.settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(ctx.settings.window_geometry.encode()))
        if ctx.settings.window_state:
            self.vsplit.restoreState(QByteArray.fromBase64(ctx.settings.window_state.encode()))
        self._refresh_device_combo()
        if ctx.adb is None:
            QTimer.singleShot(200, self._adb_missing)
        self.ctx.logbook.info(f"{APP_NAME} {__version__} started", op_type="app")

    @staticmethod
    def _wrap(w: QWidget) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 6, 6, 0)
        lay.addWidget(w)
        return box

    # ----------------------------------------------------------------- chrome
    def _build_toolbar(self) -> None:
        tb = QToolBar(tr("Main"))
        tb.setObjectName("mainToolbar")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        tb.addWidget(QLabel(" " + tr("Device:") + " "))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(360)
        self.device_combo.currentIndexChanged.connect(self._device_selected)
        tb.addWidget(self.device_combo)
        self.a_refresh_dev = tb.addAction(icon("view-refresh"), tr("Rescan"), self.ctx.refresh_devices)
        self.a_refresh_dev.setToolTip(tr("Re-run 'adb devices -l'"))
        self.a_connect = tb.addAction(icon("network-connect"), tr("Connect…"), self.connect_network)
        self.a_info = tb.addAction(icon("document-properties"), tr("Device info"), self.show_device_info)
        tb.addSeparator()
        self.a_rom = tb.addAction(icon("applications-games"), tr("Upload ROMs…"), lambda: self.open_rom_wizard("rom"))
        self.a_bios = tb.addAction(icon("drive-removable-media"), tr("Upload BIOS…"), lambda: self.open_rom_wizard("bios"))
        tb.addSeparator()
        tb.addAction(icon("configure"), tr("Settings"), self.open_settings)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu(tr("&File"))
        m.addAction(icon("configure"), tr("Settings…"), QKeySequence("Ctrl+,"), self.open_settings)
        m.addSeparator()
        m.addAction(tr("Quit"), QKeySequence(QKeySequence.StandardKey.Quit), self.close)
        m = mb.addMenu(tr("&Device"))
        m.addAction(self.a_refresh_dev)
        m.addAction(self.a_connect)
        self.a_disconnect = m.addAction(icon("network-disconnect"), tr("Disconnect network device"), self.disconnect_network)
        m.addAction(self.a_info)
        self.a_rename_dev = m.addAction(tr("Rename device…"), self.rename_device)
        m.addSeparator()
        self.a_es_reload = m.addAction(tr("Ask EmulationStation to reload game lists…"), self.reload_gamelists)
        m = mb.addMenu(tr("&Tools"))
        m.addAction(self.a_rom)
        m.addAction(self.a_bios)
        m.addSeparator()
        m.addAction(tr("Diagnostic bundle…"), self.logs.save_bundle)
        m.addAction(tr("Open log folder"), self.logs.open_folder)
        m = mb.addMenu(tr("&View"))
        for i, name in enumerate((tr("Transfers"), tr("Activity && Logs"), tr("Console"))):
            a = m.addAction(name, lambda i=i: self.bottom.setCurrentIndex(i))
            a.setShortcut(QKeySequence(f"Ctrl+{i + 1}"))
        m.addSeparator()
        for key, label in (("system", tr("System theme")), ("light", tr("Light theme")), ("dark", tr("Dark theme"))):
            m.addAction(label, lambda k=key: self._set_theme(k))
        m = mb.addMenu(tr("&Help"))
        m.addAction(tr("Keyboard shortcuts"), self.show_shortcuts)
        m.addAction(tr("Troubleshooting"), lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.join(os.path.dirname(__file__), "..", "..", "docs", "TROUBLESHOOTING.md"))))
        m.addAction(tr("About"), self.show_about)

    def _build_status(self) -> None:
        self.status_device = QLabel()
        self.status_queue = QLabel()
        self.statusBar().addPermanentWidget(self.status_device)
        self.statusBar().addPermanentWidget(self.status_queue)
        self._update_status()

    def _update_status(self) -> None:
        caps = self.ctx.caps.get(self.serial or "")
        if self.serial and caps:
            self.status_device.setText(f"{self.serial} · {caps.device_class.value} · {caps.os_pretty}"
                                       + (" · Knulli" if caps.knulli else ""))
        elif self.serial:
            self.status_device.setText(f"{self.serial} · " + tr("probing…"))
        else:
            self.status_device.setText(tr("No device"))
        finished, total, active = self.ctx.queue.overall()
        self.status_queue.setText(tr("Transfers: {a} running, {p} queued").format(
            a=active, p=sum(1 for j in self.ctx.queue.jobs if j.status == JobStatus.PENDING)))
        enabled = bool(self.serial and caps)
        for a in (self.a_info, self.a_rom, self.a_bios, self.a_rename_dev):
            a.setEnabled(enabled)
        dev = self.ctx.device(self.serial)
        self.a_disconnect.setEnabled(bool(dev and dev.transport == "tcp"))
        self.a_es_reload.setEnabled(enabled and self.ctx.knulli_enabled(self.serial))

    # ----------------------------------------------------------------- devices
    def _refresh_device_combo(self) -> None:
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for d in self.ctx.devices:
            self.device_combo.addItem(self._device_label(d), d.serial)
            if not d.usable:
                idx = self.device_combo.count() - 1
                self.device_combo.setItemData(idx, STATE_LABELS.get(d.state, d.state.value), Qt.ItemDataRole.ToolTipRole)
        if not self.ctx.devices:
            self.device_combo.addItem(tr("No devices — connect via USB or use Connect…"), None)
        idx = self.device_combo.findData(self.serial) if self.serial else -1
        if idx < 0:
            idx = next((i for i, d in enumerate(self.ctx.devices) if d.usable), 0)
        self.device_combo.setCurrentIndex(idx)
        self.device_combo.blockSignals(False)
        self._device_selected()

    def _device_label(self, d: DeviceInfo) -> str:
        name = self.ctx.display_name(d.serial)
        transport = {"usb": "USB", "tcp": "Wi-Fi/LAN", "emulator": "emulator"}.get(d.transport, d.transport)
        state = STATE_LABELS.get(d.state, d.state.value)
        caps = self.ctx.caps.get(d.serial)
        cls = f" · {caps.device_class.value}" if caps else ""
        return f"{name} — {transport}{cls}" + (f"  [{tr(state)}]" if state else "")

    def _devices_changed(self, _devices: list[DeviceInfo]) -> None:
        self._refresh_device_combo()

    def _device_selected(self) -> None:
        serial = self.device_combo.currentData()
        dev = self.ctx.device(serial)
        if dev is not None and not dev.usable:
            serial_usable = None
        else:
            serial_usable = serial
        if serial_usable != self.serial or (serial_usable and self.remote.caps is None and serial_usable in self.ctx.caps):
            self.serial = serial_usable
            self.remote.set_device(serial_usable, self.ctx.caps.get(serial_usable) if serial_usable else None)
            self.console.set_device(serial_usable)
            if dev is not None and not dev.usable:
                self.remote._show_placeholder(tr("Device {serial} is {state}.").format(
                    serial=dev.serial, state=tr(STATE_LABELS.get(dev.state, dev.state.value))) +
                    ("\n\n" + dev.state_detail if dev.state_detail else ""))
        self._update_status()

    def _device_ready(self, serial: str, caps) -> None:  # type: ignore[no-untyped-def]
        prof = self.ctx.settings.profile(serial)
        if not prof.default_remote_dir and caps.knulli:
            prof.default_remote_dir = "/userdata"
        self.ctx.save_settings()
        idx = self.device_combo.findData(serial)
        if idx >= 0:
            dev = self.ctx.device(serial)
            if dev:
                self.device_combo.setItemText(idx, self._device_label(dev))
        if self.serial is None and self.device_combo.currentData() == serial:
            self._device_selected()
        elif serial == self.serial:
            self.remote.set_device(serial, caps)
        elif self.serial is None:
            self.device_combo.setCurrentIndex(idx)
        self._update_status()

    def _device_lost(self, serial: str) -> None:
        self.remote.device_lost(serial)
        self.statusBar().showMessage(tr("Device {serial} disconnected").format(serial=serial), 8000)
        self._update_status()

    def connect_network(self) -> None:
        if self.ctx.adb is None:
            self._adb_missing()
            return
        dlg = ConnectDialog(self.ctx.settings.recent_connections, self)
        if dlg.exec() != dlg.DialogCode.Accepted or not dlg.target():
            return
        target = dlg.target()
        adb = self.ctx.adb
        self.statusBar().showMessage(tr("Connecting to {t}…").format(t=target))

        def done(result) -> None:  # type: ignore[no-untyped-def]
            ok, msg = result
            self.ctx.logbook.log(20 if ok else 40, f"adb connect {target}: {msg}", device=target, op_type="connect",  # type: ignore[arg-type]
                                 result="success" if ok else "failed")
            if ok:
                rec = [t for t in self.ctx.settings.recent_connections if t != target]
                self.ctx.settings.recent_connections = [target] + rec[:9]
                self.ctx.save_settings()
                self.statusBar().showMessage(msg, 5000)
                self.ctx.refresh_devices()
            else:
                show_error(self, tr("Connection failed"), msg)

        self.ctx.tasks.submit(adb.connect, target, on_done=done, on_error=lambda e: show_error(self, tr("Connection failed"), str(e)))

    def disconnect_network(self) -> None:
        dev = self.ctx.device(self.serial)
        if not dev or dev.transport != "tcp" or self.ctx.adb is None:
            return
        running = [j for j in self.ctx.queue.jobs if j.serial == dev.serial and j.status.value in ("Preparing", "Transferring", "Verifying")]
        if running and not ask_yes_no(self, tr("Disconnect"), tr("Transfers are running on this device. Disconnect anyway?")):
            return
        ok, msg = self.ctx.adb.disconnect(dev.serial)
        self.ctx.logbook.log(20 if ok else 40, f"adb disconnect {dev.serial}: {msg}", device=dev.serial,  # type: ignore[arg-type]
                             op_type="disconnect")
        self.ctx.refresh_devices()

    def rename_device(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        if not self.serial:
            return
        prof = self.ctx.settings.profile(self.serial)
        name, ok = QInputDialog.getText(self, tr("Rename device"), tr("Display name for {s}:").format(s=self.serial),
                                        text=prof.alias)
        if ok:
            prof.alias = name.strip()
            self.ctx.save_settings()
            self._refresh_device_combo()
            self.remote.title.setText(self.ctx.display_name(self.serial))

    def show_device_info(self) -> None:
        caps = self.ctx.caps.get(self.serial or "")
        dev = self.ctx.device(self.serial)
        if caps:
            CapabilitiesDialog(caps, dev.attributes if dev else {}, self).exec()

    def reload_gamelists(self) -> None:
        if not self.serial:
            return
        caps = self.ctx.caps.get(self.serial)
        if not caps or not (caps.has("curl") or caps.has("wget")):
            show_error(self, tr("Reload game lists"), tr("Neither curl nor wget is available on the device."))
            return
        if not ask_yes_no(self, tr("Reload game lists"),
                          tr("Ask EmulationStation on the device to rescan its game lists now?\n\n"
                             "This calls the local EmulationStation web API (http://127.0.0.1:1234/reloadgames) on the "
                             "device. It does not edit gamelist.xml files."), default_no=True):
            return
        cmd = ("curl -s -m 20 http://127.0.0.1:1234/reloadgames" if caps.has("curl")
               else "wget -q -T 20 -O - http://127.0.0.1:1234/reloadgames")
        serial = self.serial

        def done(res) -> None:  # type: ignore[no-untyped-def]
            ok = res.returncode == 0
            self.ctx.logbook.log(20 if ok else 40, "EmulationStation reload requested", device=serial,  # type: ignore[arg-type]
                                 op_type="es-reload", result="success" if ok else "failed",
                                 error=None if ok else (res.stderr_text or res.stdout_text)[:300],
                                 extra={"confirmed": True, "exit_code": res.returncode})
            self.statusBar().showMessage(tr("Reload requested") if ok else tr("Reload request failed"), 5000)

        self.ctx.tasks.submit(self.ctx.adb.shell_raw, serial, cmd, 30, on_done=done,  # type: ignore[union-attr]
                              on_error=lambda e: show_error(self, tr("Reload game lists"), str(e)))

    # ----------------------------------------------------------------- transfers
    def _default_verify(self) -> VerifyMode:
        return VerifyMode(self.ctx.settings.verify_mode)

    def _upload_from_local(self, paths: list[str], dest: str | None, move: bool) -> None:
        self.enqueue_upload(paths, dest or self.remote.cwd, move)

    def _download_from_remote(self, paths: list[str], dest: str | None, move: bool) -> None:
        self.enqueue_download(paths, dest or self.local.cwd, move)

    def enqueue_upload(self, paths: list[str], dest: str, move: bool = False) -> TransferJob | None:
        if not self.serial or not self.ctx.caps.get(self.serial):
            show_error(self, tr("Upload"), tr("No device is ready. Connect a device first."))
            return None
        if not dest:
            show_error(self, tr("Upload"), tr("Open a folder on the device first."))
            return None
        paths = [p for p in paths if p]
        preview = "\n".join(paths[:10]) + ("\n…" if len(paths) > 10 else "")
        if move:
            if not ask_yes_no(self, tr("Move to device"),
                              tr("Move {n} item(s) to {dest} on {dev}?\n\nThe local originals are deleted only after each "
                                 "file was transferred and verified.\n\n{preview}").format(
                                  n=len(paths), dest=dest, dev=self.ctx.display_name(self.serial), preview=preview),
                              default_no=False):
                return None
        job = TransferJob(self.serial, Direction.UPLOAD, paths, normalize_remote(dest), overwrite=self.ctx.settings.overwrite_policy,
                          verify=self._default_verify(), move=move)
        self.ctx.queue.add(job)
        self.bottom.setCurrentWidget(self.transfers)
        self.statusBar().showMessage(tr("Queued upload of {n} item(s) to {dest}").format(n=len(paths), dest=dest), 5000)
        return job

    def enqueue_download(self, paths: list[str], dest: str, move: bool = False) -> TransferJob | None:
        if not self.serial:
            show_error(self, tr("Download"), tr("No device is ready."))
            return None
        if move and not ask_yes_no(self, tr("Move to computer"),
                                   tr("Move {n} item(s) from the device to {dest}?\n\nFiles on the device are deleted only "
                                      "after they were downloaded and verified.").format(n=len(paths), dest=dest),
                                   default_no=False):
            return None
        job = TransferJob(self.serial, Direction.DOWNLOAD, paths, dest, overwrite=self.ctx.settings.overwrite_policy,
                          verify=self._default_verify(), move=move)
        self.ctx.queue.add(job)
        self.bottom.setCurrentWidget(self.transfers)
        self.statusBar().showMessage(tr("Queued download of {n} item(s) to {dest}").format(n=len(paths), dest=dest), 5000)
        return job

    def _resolve_conflict(self, req: ConflictRequest) -> None:
        dlg = ConflictDialog(req.query, self)
        dlg.exec()
        req.answer = (dlg.action, dlg.apply_all.isChecked())
        req.event.set()

    def _job_changed(self, job: TransferJob) -> None:
        self._update_status()
        if not job.finished_state:
            return
        if job.direction == Direction.UPLOAD and job.serial == self.serial and self.remote.cwd and (
                is_remote_within(self.remote.cwd, job.dest_dir) or is_remote_within(job.dest_dir, self.remote.cwd)):
            self.remote.refresh()
        if job.direction == Direction.DOWNLOAD and job.move and job.serial == self.serial:
            self.remote.refresh()
        if job.status == JobStatus.CANCELLED and job.partial_paths:
            self.statusBar().showMessage(tr("Transfer cancelled — partial file(s) may remain; see Transfers ▸ Remove partial files"),
                                         10000)
        if job.id in self._tracked_rom_jobs:
            self._tracked_rom_jobs.discard(job.id)
            self._rom_summary(job)

    def _rom_summary(self, job: TransferJob) -> None:
        lines = [
            tr("Status: {s}").format(s=tr(job.status.value)),
            tr("Uploaded: {n} file(s), {size}").format(n=job.files_done, size=format_bytes(job.bytes_done)),
            tr("Skipped (already present): {n}").format(n=job.files_skipped),
            tr("Failed: {n}").format(n=job.files_failed),
            tr("Verification: {ok} passed, {bad} failed, {unk} unverifiable").format(
                ok=job.verify_ok, bad=job.verify_failed, unk=job.verify_unknown),
            tr("Destination: {d}").format(d=job.dest_dir),
        ]
        if job.error:
            lines.append(job.error)
        box = QMessageBox(QMessageBox.Icon.Information if job.status == JobStatus.COMPLETED else QMessageBox.Icon.Warning,
                          tr("Transfer summary"), "\n".join(lines), parent=self)
        reload_btn = None
        if job.status == JobStatus.COMPLETED and job.files_done and self.ctx.knulli_enabled(job.serial):
            reload_btn = box.addButton(tr("Reload game lists…"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if reload_btn is not None and box.clickedButton() is reload_btn:
            self.reload_gamelists()

    def open_rom_wizard(self, mode: str) -> None:
        if not self.serial or not self.ctx.caps.get(self.serial):
            show_error(self, tr("Upload"), tr("No device is ready."))
            return
        files = self.local.selected_paths()
        dlg = RomUploadDialog(self.ctx, self.serial, mode, files, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        job = dlg.build_job()
        self._tracked_rom_jobs.add(job.id)
        self.ctx.queue.add(job)
        self.bottom.setCurrentWidget(self.transfers)

    # ----------------------------------------------------------------- settings / misc
    def open_settings(self) -> None:
        dlg = SettingsDialog(self.ctx.settings, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        old = self.ctx.settings
        new = dlg.settings
        adb_changed = (old.adb_path, old.server_host, old.server_port) != (new.adb_path, new.server_host, new.server_port)
        new.window_geometry, new.window_state = old.window_geometry, old.window_state
        self.ctx.settings = new
        self.ctx.save_settings()
        self.ctx.logbook.info("Settings saved", op_type="settings")
        if adb_changed:
            self.ctx.reload_adb()

    def _settings_changed(self) -> None:
        apply_theme(QApplication.instance(), self.ctx.settings.theme)  # type: ignore[arg-type]
        self._refresh_device_combo()

    def _set_theme(self, theme: str) -> None:
        self.ctx.settings.theme = theme
        self.ctx.save_settings()

    def _adb_missing(self) -> None:
        QMessageBox.warning(self, tr("ADB not found"),
                            tr("The adb executable could not be found.\n\nOn Arch Linux install it with:\n"
                               "    sudo pacman -S android-tools\n\nor set its location in Settings."))

    def show_shortcuts(self) -> None:
        QMessageBox.information(self, tr("Keyboard shortcuts"), tr(
            "Enter — open folder\nBackspace / Alt+Up — parent folder\nAlt+Left / Alt+Right — back / forward\n"
            "F5 — refresh\nCtrl+L — edit path\nCtrl+F — filter\nF2 — rename\nDelete — delete (local: trash)\n"
            "Shift+Delete — delete permanently (local)\nCtrl+Shift+N — new folder\nCtrl+C / Ctrl+X / Ctrl+V — copy / cut / paste\n"
            "Ctrl+Shift+C — copy path\nCtrl+U — upload selection\nCtrl+D — download selection\nAlt+Enter — properties\n"
            "Ctrl+1/2/3 — Transfers / Logs / Console"))

    def show_about(self) -> None:
        QMessageBox.about(self, tr("About"), f"<b>{APP_NAME}</b> {__version__}<br>" + tr(
            "Dual-pane file manager for devices reachable through ADB, including non-Android Linux devices such as "
            "Knulli handhelds.<br>ADB: {adb}").format(adb=self.ctx.adb_version or "?"))

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.ctx.queue.running_ids() and not ask_yes_no(
            self, tr("Transfers running"),
            tr("Transfers are still running. Quit anyway?\n\nRunning transfers will be stopped and marked as interrupted; "
               "the current file may be left incomplete.")):
            event.ignore()
            return
        self.ctx.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.ctx.settings.window_state = bytes(self.vsplit.saveState().toBase64()).decode()
        try:
            self.ctx.save_settings()
        except OSError:
            pass
        self.ctx.shutdown()
        event.accept()
