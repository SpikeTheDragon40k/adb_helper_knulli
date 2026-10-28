"""GUI tests (pytest-qt, offscreen) against the simulated RG35XX SP."""

import os
import time

import pytest

pytest.importorskip("pytestqt")

from PySide6.QtCore import QMimeData, QPoint, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtWidgets import QDialog, QMessageBox  # noqa: E402

from openadb.core.config import ConfigStore, Settings  # noqa: E402
from openadb.core.transfer import JobStatus  # noqa: E402

from conftest import SERIAL  # noqa: E402

GBA = "/userdata/roms/gba"


@pytest.fixture
def window(qtbot, mock_env, rg35xx, tmp_path, monkeypatch):
    from openadb.gui.context import AppContext
    from openadb.gui.main_window import MainWindow

    # Never block on modal message boxes in tests; record them instead.
    shown = []

    def fake_box(*args, **kwargs):
        shown.append(args[1:3])
        return QMessageBox.StandardButton.Ok

    for name in ("critical", "warning", "information"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(fake_box))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append((self.windowTitle(), self.text())) or 0)
    local = tmp_path / "local"
    local.mkdir()
    settings = Settings(adb_path=mock_env.adb_path, default_local_dir=str(local), refresh_interval=1.0)
    ctx = AppContext(settings, ConfigStore(tmp_path / "cfg" / "settings.json"))
    w = MainWindow(ctx)
    w.shown_messages = shown
    w.local_dir = local
    w.show()
    qtbot.waitUntil(lambda: w.serial == SERIAL and w.remote.cwd == "/userdata", timeout=15000)
    yield w
    ctx.queue.cancel_all()
    ctx.queue.wait_idle(20)
    w.close()


def remote_names(w):
    return {e.name for e in w.remote.model.entries}


def test_discovers_rg35xx_and_browses_gba(window, qtbot):
    caps = window.ctx.caps[SERIAL]
    assert caps.device_class.value == "Generic Linux ADB"
    assert not caps.has_getprop
    assert "Knulli" in window.device_combo.currentText()
    assert {"roms", "bios", "saves", "system"} <= remote_names(window)
    window.remote.navigate(GBA)
    qtbot.waitUntil(lambda: window.remote.cwd == GBA, timeout=10000)
    assert "Pokemon FireRed.gba" in remote_names(window)
    assert "ポケモン ルビー.gba" in remote_names(window)
    assert window.remote.path_bar.path() == GBA
    window.remote.go_up()
    qtbot.waitUntil(lambda: window.remote.cwd == "/userdata/roms", timeout=10000)
    window.remote.go_back()
    qtbot.waitUntil(lambda: window.remote.cwd == GBA, timeout=10000)


def test_upload_download_and_gui_stays_responsive(window, qtbot, mock_env, rg35xx):
    mock_env.set_option(SERIAL, "rate", 1_000_000)
    src = window.local_dir / "Big ROM (USA).gba"
    src.write_bytes(os.urandom(2_000_000))
    window.remote.navigate(GBA)
    qtbot.waitUntil(lambda: window.remote.cwd == GBA, timeout=10000)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(20)
    job = window.enqueue_upload([str(src)], GBA)
    qtbot.waitUntil(lambda: job.status == JobStatus.COMPLETED, timeout=30000)
    timer.stop()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.5, f"GUI thread blocked for {max(gaps):.2f}s"
    assert (rg35xx.host(GBA) / "Big ROM (USA).gba").read_bytes() == src.read_bytes()
    qtbot.waitUntil(lambda: "Big ROM (USA).gba" in remote_names(window), timeout=10000)
    assert window.transfers.model.rowCount() == 1
    # download a folder back
    mock_env.set_option(SERIAL, "rate", None)
    out = window.local_dir / "dl"
    out.mkdir()
    job2 = window.enqueue_download(["/userdata/system"], str(out))
    qtbot.waitUntil(lambda: job2.status == JobStatus.COMPLETED, timeout=30000)
    assert (out / "system" / "logs" / "es_log.txt").read_text() == "log\n"


def test_remote_delete_flow_with_confirmation(window, qtbot, rg35xx, monkeypatch):
    from openadb.gui.dialogs import DeleteConfirmDialog

    folder = rg35xx.host(GBA) / "To Delete"
    folder.mkdir()
    (folder / "inner.gba").write_bytes(b"x")
    window.remote.navigate(GBA)
    qtbot.waitUntil(lambda: window.remote.cwd == GBA, timeout=10000)
    window.remote.select_names(["To Delete", "gamelist.xml"])
    assert {e.name for e in window.remote.selected_entries()} == {"To Delete", "gamelist.xml"}
    seen = {}

    def accept(self):
        seen["recursive_needed"] = self.recursive_box is not None
        seen["enabled_before"] = self.delete_btn.isEnabled()
        self.recursive_box.setChecked(True)
        seen["enabled_after"] = self.delete_btn.isEnabled()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(DeleteConfirmDialog, "exec", accept)
    window.remote.delete_selected()
    qtbot.waitUntil(lambda: not folder.exists() and not (rg35xx.host(GBA) / "gamelist.xml").exists(), timeout=10000)
    assert seen == {"recursive_needed": True, "enabled_before": False, "enabled_after": True}
    qtbot.waitUntil(lambda: "To Delete" not in remote_names(window), timeout=10000)
    events = [e for e in window.ctx.logbook.events() if e.op_type == "delete"]
    item_events = [e for e in events if e.source]
    assert {e.source for e in item_events} == {GBA + "/To Delete", GBA + "/gamelist.xml"}
    assert all(e.extra.get("confirmed") is True and e.extra.get("exit_code") == 0 for e in item_events)
    assert any("Delete completed" in e.message for e in events)


def test_remote_delete_cancelled_keeps_files(window, qtbot, rg35xx, monkeypatch):
    from openadb.gui.dialogs import DeleteConfirmDialog

    window.remote.navigate(GBA)
    qtbot.waitUntil(lambda: window.remote.cwd == GBA, timeout=10000)
    window.remote.select_names(["Pokemon FireRed.gba"])
    called = []
    monkeypatch.setattr(DeleteConfirmDialog, "exec", lambda self: called.append(1) or QDialog.DialogCode.Rejected)
    window.remote.delete_selected()
    qtbot.waitUntil(lambda: bool(called), timeout=10000)
    qtbot.wait(300)
    assert (rg35xx.host(GBA) / "Pokemon FireRed.gba").exists()


def test_protected_delete_refused(window, qtbot, rg35xx, monkeypatch):
    from openadb.gui.dialogs import DeleteConfirmDialog

    window.remote.navigate("/userdata")
    qtbot.waitUntil(lambda: window.remote.cwd == "/userdata", timeout=10000)
    window.remote.select_names(["roms"])
    monkeypatch.setattr(DeleteConfirmDialog, "exec", lambda self: pytest.fail("must not ask"))
    window.remote.delete_selected()
    assert any("Delete refused" in str(m) for m in window.shown_messages)
    assert rg35xx.host("/userdata/roms/gba").exists()


def test_rename_and_mkdir_and_cut_paste(window, qtbot, rg35xx, monkeypatch):
    from openadb.gui.dialogs import NameDialog

    window.remote.navigate(GBA)
    qtbot.waitUntil(lambda: window.remote.cwd == GBA, timeout=10000)
    answers = iter(["Renamed (1).gba", "hacks/new"])
    monkeypatch.setattr(NameDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(NameDialog, "value", lambda self: next(answers))
    window.remote.select_names(["It's a Test's ROM.gba"])
    window.remote.rename_selected()
    qtbot.waitUntil(lambda: (rg35xx.host(GBA) / "Renamed (1).gba").exists(), timeout=10000)
    window.remote.new_folder()
    qtbot.waitUntil(lambda: (rg35xx.host(GBA) / "hacks" / "new").is_dir(), timeout=10000)
    qtbot.waitUntil(lambda: "hacks" in remote_names(window), timeout=10000)
    window.remote.select_names(["Renamed (1).gba"])
    window.remote.copy_selected(cut=True)
    window.remote.paste(GBA + "/hacks")
    qtbot.waitUntil(lambda: (rg35xx.host(GBA) / "hacks" / "Renamed (1).gba").exists(), timeout=10000)
    assert not (rg35xx.host(GBA) / "Renamed (1).gba").exists()


def test_drop_local_files_on_remote_uploads(window, qtbot, rg35xx):
    src = window.local_dir / "dropped.gba"
    src.write_bytes(b"abc")
    window.remote.navigate(GBA)
    qtbot.waitUntil(lambda: window.remote.cwd == GBA, timeout=10000)
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(src))])
    assert window.remote._accept_drop(md)
    assert window.remote._handle_drop(md, QPoint(-1, -1), Qt.DropAction.CopyAction)
    qtbot.waitUntil(lambda: (rg35xx.host(GBA) / "dropped.gba").exists(), timeout=15000)


def test_drag_remote_to_local_downloads(window, qtbot):
    from openadb.gui.remote_model import encode_remote_mime

    md = encode_remote_mime(SERIAL, [GBA + "/Pokemon FireRed.gba"])
    assert window.local._accept_drop(md)
    window.local._handle_drop(md, QPoint(-1, -1), Qt.DropAction.CopyAction)
    target = window.local_dir / "Pokemon FireRed.gba"
    qtbot.waitUntil(lambda: target.exists() and target.stat().st_size == 4096, timeout=15000)


def test_device_disconnect_and_reconnect(window, qtbot, mock_env):
    window.remote.navigate(GBA)
    qtbot.waitUntil(lambda: window.remote.cwd == GBA, timeout=10000)
    mock_env.set_state(SERIAL, "offline")
    window.ctx.refresh_devices()
    qtbot.waitUntil(lambda: window.remote.stack.currentWidget() is window.remote.placeholder, timeout=10000)
    assert any("Device state changed" in e.message for e in window.ctx.logbook.events())
    mock_env.set_state(SERIAL, "device")
    window.ctx.refresh_devices()
    qtbot.waitUntil(lambda: window.remote.stack.currentWidget() is window.remote.view and window.remote.cwd == GBA,
                    timeout=15000)
    mock_env.remove_device(SERIAL)
    window.ctx.refresh_devices()
    qtbot.waitUntil(lambda: window.serial is None, timeout=10000)
    assert any(e.message.startswith("Device disconnected") for e in window.ctx.logbook.events())


def test_console_remote_shell(window, qtbot):
    window.console.input.setText("uname -a")
    window.console.run()
    qtbot.waitUntil(lambda: "exit 0" in window.console.output.toPlainText(), timeout=10000)
    assert "KNULLI" in window.console.output.toPlainText()
    window.console.input.setText("getprop ro.product.model")
    window.console.run()
    qtbot.waitUntil(lambda: "not found" in window.console.output.toPlainText(), timeout=10000)


def test_console_adb_mode_rejects_unlisted(window, qtbot):
    window.console.mode.setCurrentIndex(1)
    window.console.input.setText("install evil.apk")
    window.console.run()
    assert "not an allowed ADB command" in window.console.output.toPlainText()
    window.console.input.setText("devices -l")
    window.console.run()
    qtbot.waitUntil(lambda: "model:Nexus_4" in window.console.output.toPlainText(), timeout=10000)


def test_rom_wizard_builds_verified_job(window, qtbot, rg35xx):
    from openadb.gui.rom_wizard import RomUploadDialog

    rom = window.local_dir / "Golden Sun.gba"
    rom.write_bytes(os.urandom(1000))
    dlg = RomUploadDialog(window.ctx, SERIAL, "rom", [str(rom)])
    qtbot.addWidget(dlg)
    dlg._files_changed()
    assert dlg.selected_system().key == "gba"
    qtbot.waitUntil(lambda: dlg.check is not None, timeout=10000)
    assert dlg.destination() == GBA and dlg.check.exists and dlg.start_btn.isEnabled()
    job = dlg.build_job()
    assert job.verify.value == "hash" and job.dest_dir == GBA and job.overwrite == "skip"
    window.ctx.queue.add(job)
    qtbot.waitUntil(lambda: job.status == JobStatus.COMPLETED, timeout=20000)
    assert job.verify_ok == 1


def test_logs_view_filters(window, qtbot):
    window.ctx.logbook.error("special failure xyz", device=SERIAL, op_type="upload")
    logs = window.logs
    qtbot.waitUntil(lambda: any(e.message == "special failure xyz" for e in logs.model.events), timeout=5000)
    logs.search.setText("xyz")
    assert logs.proxy.rowCount() == 1
    logs.search.setText("")
    logs.level.setCurrentIndex(3)  # ERROR and above
    assert all(e.level.label == "ERROR" for e in logs.visible_events())
