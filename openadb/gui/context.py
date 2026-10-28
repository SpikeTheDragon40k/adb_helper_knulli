"""Application context: settings, ADB client, device monitor, transfer queue and their Qt bridges."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from openadb.core.adb import AdbClient
from openadb.core.capabilities import DeviceCapabilities, probe_capabilities
from openadb.core.config import ConfigStore, Settings
from openadb.core.devices import DeviceInfo, DeviceState, diff_devices
from openadb.core.errors import AdbNotFoundError, OpenADBError
from openadb.core.logbook import Level, LogBook, LogEvent
from openadb.core.paths import data_dir, log_dir
from openadb.core.progress import ProgressSnapshot
from openadb.core.remote_fs import RemoteFS
from openadb.core.transfer import ConflictAction, ConflictQuery, TransferJob, TransferQueue
from openadb.gui.tasks import TaskRunner


@dataclass
class ConflictRequest:
    query: ConflictQuery
    event: threading.Event = field(default_factory=threading.Event)
    answer: tuple[ConflictAction, bool] = (ConflictAction.SKIP, False)


class TransferBridge(QObject):
    """Adapts the transfer engine's listener protocol to Qt signals."""

    job_changed = Signal(object)
    progress = Signal(object)
    conflict_requested = Signal(object)

    closing = False

    def resolve_conflict(self, query: ConflictQuery) -> tuple[ConflictAction, bool]:
        # Called from a worker thread: ask the GUI and wait for its answer.
        req = ConflictRequest(query)
        self.conflict_requested.emit(req)
        while not req.event.wait(0.25):
            if self.closing:
                return ConflictAction.CANCEL, True
        return req.answer

    # Protocol methods (called from worker threads; signals are queued to the GUI thread).
    def job_changed_cb(self, job: TransferJob) -> None:
        self.job_changed.emit(job)

    def progress_cb(self, snap: ProgressSnapshot) -> None:
        self.progress.emit(snap)


class _ListenerAdapter:
    def __init__(self, bridge: TransferBridge) -> None:
        self.bridge = bridge

    def job_changed(self, job: TransferJob) -> None:
        self.bridge.job_changed_cb(job)

    def progress(self, snapshot: ProgressSnapshot) -> None:
        self.bridge.progress_cb(snapshot)

    def resolve_conflict(self, query: ConflictQuery) -> tuple[ConflictAction, bool]:
        return self.bridge.resolve_conflict(query)


@dataclass
class ClipboardState:
    kind: str  # "local" or "remote"
    paths: list[str]
    cut: bool
    serial: str | None = None


class AppContext(QObject):
    devices_changed = Signal(list)  # list[DeviceInfo]
    device_ready = Signal(str, object)  # serial, DeviceCapabilities
    device_lost = Signal(str)
    log_event = Signal(object)  # LogEvent
    adb_error = Signal(str)
    settings_changed = Signal()

    def __init__(self, settings: Settings | None = None, store: ConfigStore | None = None,
                 start_monitor: bool = True, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.store = store or ConfigStore()
        self.settings = settings or self.store.load()
        self.tasks = TaskRunner(parent=self)
        self.logbook = LogBook(
            log_dir(),
            level=Level.parse(self.settings.log_level),
            max_bytes=self.settings.log_max_mb * 1024 * 1024,
            retention_days=self.settings.log_retention_days,
        )
        self.logbook.subscribe(self._forward_log)
        self.adb: AdbClient | None = None
        self.adb_version = ""
        self.devices: list[DeviceInfo] = []
        self.caps: dict[str, DeviceCapabilities] = {}
        self._probing: set[str] = set()
        self._refreshing = False
        self.clipboard: ClipboardState | None = None
        self.bridge = TransferBridge(self)
        self.queue = TransferQueue(
            self.rfs_for,
            self.logbook,
            _ListenerAdapter(self.bridge),
            data_dir() / "transfer_queue.json",
            concurrency=self.settings.concurrency,
        )
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_devices)
        self._init_adb()
        if start_monitor:
            self.apply_refresh_settings()
            QTimer.singleShot(0, self.refresh_devices)

    # ----------------------------------------------------------------- logging
    def _forward_log(self, event: LogEvent) -> None:
        self.log_event.emit(event)  # queued to GUI thread when emitted from workers

    # ----------------------------------------------------------------- adb
    def _init_adb(self) -> None:
        try:
            self.adb = AdbClient(self.settings.adb_path or None, self.settings.server_host, self.settings.server_port,
                                 default_timeout=self.settings.command_timeout)
        except AdbNotFoundError as exc:
            self.adb = None
            self.logbook.error(str(exc), op_type="adb")
            self.adb_error.emit(str(exc))
            return
        self.logbook.info(f"Using ADB executable: {self.adb.executable}", op_type="adb")
        adb = self.adb

        def version() -> str:
            return adb.version().summary

        def done(v: str) -> None:
            self.adb_version = v
            self.logbook.info(f"ADB client version: {v}", op_type="adb")

        self.tasks.submit(version, on_done=done, on_error=lambda e: self.logbook.warning(f"ADB version check failed: {e}"))

    def reload_adb(self) -> None:
        self.caps.clear()
        self._init_adb()
        self.refresh_devices()

    def apply_refresh_settings(self) -> None:
        if self.settings.auto_refresh:
            self.timer.start(int(self.settings.refresh_interval * 1000))
        else:
            self.timer.stop()

    def save_settings(self) -> None:
        self.settings.sanitize()
        self.store.save(self.settings)
        self.logbook.level = Level.parse(self.settings.log_level)
        self.logbook.retention_days = self.settings.log_retention_days
        self.logbook.max_bytes = self.settings.log_max_mb * 1024 * 1024
        self.queue.set_concurrency(self.settings.concurrency)
        self.apply_refresh_settings()
        self.settings_changed.emit()

    # ----------------------------------------------------------------- devices
    @Slot()
    def refresh_devices(self) -> None:
        if self.adb is None or self._refreshing:
            return
        self._refreshing = True
        adb = self.adb
        self.tasks.submit(adb.devices, on_done=self._on_devices, on_error=self._on_devices_error)

    def _on_devices_error(self, exc: BaseException) -> None:
        self._refreshing = False
        msg = f"Device discovery failed: {exc}"
        if not getattr(self, "_last_discovery_error", None) == msg:
            self.logbook.error(msg, op_type="discovery")
            self._last_discovery_error = msg
        self.adb_error.emit(msg)

    def _on_devices(self, devices: list[DeviceInfo]) -> None:
        self._refreshing = False
        self._last_discovery_error = None
        diff = diff_devices(self.devices, devices)
        self.devices = devices
        for d in diff.added:
            self.logbook.info(f"Device detected: {d.serial} ({d.state.value}, {d.transport})", device=d.serial,
                              op_type="discovery", extra={"adb_attributes": d.attributes})
            if d.state == DeviceState.UNAUTHORIZED:
                self.logbook.warning("Device is unauthorized: confirm the RSA key prompt on the device", device=d.serial,
                                     op_type="discovery")
            elif d.state == DeviceState.NO_PERMISSIONS:
                self.logbook.warning(f"No permission to access USB device: {d.state_detail} — check udev rules",
                                     device=d.serial, op_type="discovery")
        for d in diff.removed:
            self.logbook.warning(f"Device disconnected: {d.serial}", device=d.serial, op_type="discovery")
            self.caps.pop(d.serial, None)
            self.device_lost.emit(d.serial)
        for old, new in diff.changed:
            if old.state != new.state:
                level = Level.INFO if new.usable else Level.WARNING
                self.logbook.log(level, f"Device state changed: {old.state.value} → {new.state.value}", device=new.serial,
                                 op_type="discovery")
                if not new.usable:
                    self.caps.pop(new.serial, None)
                    self.device_lost.emit(new.serial)
        if not diff.empty:
            self.devices_changed.emit(devices)
        for d in devices:
            if d.usable and d.serial not in self.caps and d.serial not in self._probing:
                self.probe(d)

    def probe(self, device: DeviceInfo) -> None:
        if self.adb is None:
            return
        self._probing.add(device.serial)
        adb = self.adb
        self.logbook.info(f"Device connected: {device.serial}", device=device.serial, op_type="connect")

        def done(caps: DeviceCapabilities) -> None:
            self._probing.discard(device.serial)
            if not any(d.serial == device.serial and d.usable for d in self.devices):
                return
            self.caps[device.serial] = caps
            self.device_ready.emit(device.serial, caps)

        def fail(exc: BaseException) -> None:
            self._probing.discard(device.serial)
            self.logbook.error(f"Capability probe failed: {exc}", device=device.serial, op_type="probe")

        self.tasks.submit(probe_capabilities, adb, device.serial, logbook=self.logbook,
                          adb_reported_model=device.reported_model, on_done=done, on_error=fail)

    def device(self, serial: str | None) -> DeviceInfo | None:
        return next((d for d in self.devices if d.serial == serial), None)

    def rfs_for(self, serial: str) -> RemoteFS:
        """Return a RemoteFS for ``serial`` (callable from any thread)."""
        if self.adb is None:
            raise OpenADBError("ADB executable not available")
        caps = self.caps.get(serial)
        if caps is None:
            caps = probe_capabilities(self.adb, serial, logbook=self.logbook)
            if not caps.shell_ok:
                raise OpenADBError(f"Device {serial} has no working shell")
            self.caps[serial] = caps
        return RemoteFS(self.adb, serial, caps, protected_extra=self.settings.extra_protected_paths,
                        timeout=self.settings.command_timeout)

    def display_name(self, serial: str) -> str:
        prof = self.settings.device_profiles.get(serial)
        if prof and prof.alias:
            return prof.alias
        caps = self.caps.get(serial)
        if caps and caps.knulli:
            return f"Knulli handheld ({serial})"
        if caps and caps.android_model:
            return f"{caps.android_model} ({serial})"
        if caps and caps.hostname:
            return f"{caps.hostname} ({serial})"
        return serial

    def knulli_enabled(self, serial: str | None) -> bool:
        if not serial:
            return False
        prof = self.settings.device_profiles.get(serial)
        mode = prof.knulli_mode if prof else "auto"
        if mode == "on":
            return True
        if mode == "off":
            return False
        caps = self.caps.get(serial)
        return bool(caps and (caps.knulli or caps.batocera_like))

    # ----------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        self.timer.stop()
        self.bridge.closing = True
        self.queue.shutdown()
        self.tasks.shutdown()
        self.logbook.info("Application closed", op_type="app")
        self.logbook.close()

    def snapshot_for_diagnostics(self) -> dict[str, Any]:
        return {
            "settings": self.settings.to_dict(),
            "capabilities": {k: v.to_dict() for k, v in self.caps.items()},
            "devices": [{"serial": d.serial, "state": d.state.value, "transport": d.transport,
                         "attributes": d.attributes} for d in self.devices],
            "adb_version": self.adb_version,
        }
