from openadb.core.adb import AdbClient
from openadb.core.capabilities import DeviceClass, probe_capabilities

from conftest import SERIAL


def test_rg35xx_is_generic_linux_without_getprop(caps, logbook):
    assert caps.shell_ok
    assert caps.os_name == "Linux"
    assert caps.arch == "aarch64"
    assert not caps.has_getprop
    assert caps.device_class is DeviceClass.GENERIC_LINUX
    assert caps.knulli
    assert caps.transport == "raw" and caps.binary_safe
    assert caps.has("stat") and caps.has("find") and caps.has("ls")
    assert "/userdata/roms" in caps.existing_dirs
    messages = [e.message for e in logbook.events()]
    assert "OS detected: Linux aarch64" in messages
    assert "Android getprop unavailable" in messages
    assert any(m.startswith("Generic Linux ADB mode enabled") for m in messages)
    # ADB's fake model is never used for identification
    assert caps.adb_reported_model == "Nexus 4"


def test_pty_style_shell_uses_hex_transport(mock_env, adb):
    mock_env.add_device("PTY1", pty_mode=True)
    caps = probe_capabilities(adb, "PTY1")
    assert not caps.binary_safe
    assert caps.transport == "hex"
    assert caps.device_class is DeviceClass.GENERIC_LINUX


def test_minimal_device_without_stat(mock_env, adb):
    mock_env.add_device("MIN1", exclude_tools=("stat", "find", "sha256sum", "md5sum", "df"))
    caps = probe_capabilities(adb, "MIN1")
    assert caps.device_class is DeviceClass.MINIMAL
    assert caps.listing_strategy == "test"


def test_broken_shell_is_unknown(mock_env, adb):
    mock_env.add_device("BRK1", shell_broken=True)
    caps = probe_capabilities(adb, "BRK1")
    assert not caps.shell_ok
    assert caps.device_class is DeviceClass.UNKNOWN


def test_devices_from_mock(adb, rg35xx, mock_env):
    mock_env.add_device("192.168.1.50:5555")
    mock_env.add_device("OFF1", state="offline")
    mock_env.set_state("OFF1", "offline")
    devs = {d.serial: d for d in adb.devices()}
    assert devs[SERIAL].usable and devs[SERIAL].reported_model == "Nexus 4"
    assert devs["192.168.1.50:5555"].transport == "tcp"
    assert not devs["OFF1"].usable


def test_connect_disconnect(mock_env, adb):
    mock_env.add_device("10.0.0.7:5555", connectable_only=True)
    ok, msg = adb.connect("10.0.0.7:5555")
    assert ok, msg
    assert any(d.serial == "10.0.0.7:5555" for d in adb.devices())
    ok, msg = adb.connect("10.9.9.9:5555")
    assert not ok and "failed" in msg
    ok, _ = adb.disconnect("10.0.0.7:5555")
    assert ok
    assert not any(d.serial == "10.0.0.7:5555" for d in adb.devices())


def test_every_device_command_uses_serial(mock_env, adb, caps):
    calls = [c for c in mock_env.calls() if c and c[0] not in ("devices", "version", "start-server")]
    assert calls
    assert all(c[0] == "-s" and c[1] == SERIAL for c in calls)


def test_missing_adb_executable(tmp_path):
    import pytest

    from openadb.core.errors import AdbNotFoundError

    with pytest.raises(AdbNotFoundError):
        AdbClient(str(tmp_path / "nope" / "adb"), )
