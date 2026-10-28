"""Opt-in, non-destructive tests against a real device.

    OPENADB_REAL_DEVICE=0123456789 python -m pytest tests/integration -v -s

Read-only by default. Set OPENADB_REAL_DEVICE_WRITE=1 to also push/pull a small
temporary file inside a new directory under /tmp on the device (removed afterwards).
"""

import os
import uuid

import pytest

from openadb.core.adb import AdbClient
from openadb.core.capabilities import probe_capabilities
from openadb.core.remote_fs import RemoteFS

SERIAL = os.environ.get("OPENADB_REAL_DEVICE")
pytestmark = [pytest.mark.device, pytest.mark.skipif(not SERIAL, reason="set OPENADB_REAL_DEVICE=<serial>")]


@pytest.fixture(scope="module")
def rfs():
    adb = AdbClient(os.environ.get("OPENADB_ADB"))
    caps = probe_capabilities(adb, SERIAL)
    assert caps.shell_ok, caps.notes
    print("\nclass:", caps.device_class.value, "| os:", caps.os_pretty, "| knulli:", caps.knulli,
          "| transport:", caps.transport, "| hash:", caps.hash_command, "| notes:", caps.notes)
    return RemoteFS(adb, SERIAL, caps)


def test_device_listed():
    assert any(d.serial == SERIAL and d.usable for d in AdbClient(os.environ.get("OPENADB_ADB")).devices())


def test_list_userdata(rfs):
    entries = rfs.list_dir("/userdata" if rfs.exists("/userdata") else "/")
    assert entries
    print([e.name for e in entries][:30])


def test_walk_roms_readonly(rfs):
    if not rfs.exists("/userdata/roms/gba"):
        pytest.skip("no /userdata/roms/gba")
    print(len(rfs.walk("/userdata/roms/gba")), "entries under /userdata/roms/gba")


def test_disk_space(rfs):
    assert rfs.disk_space("/userdata" if rfs.exists("/userdata") else "/") is not None


@pytest.mark.skipif(os.environ.get("OPENADB_REAL_DEVICE_WRITE") != "1", reason="set OPENADB_REAL_DEVICE_WRITE=1")
def test_push_pull_roundtrip(rfs, tmp_path):
    d = f"/tmp/openadb-test-{uuid.uuid4().hex[:8]}"
    src = tmp_path / "sample file ü.bin"
    src.write_bytes(os.urandom(100_000))
    assert rfs.mkdir(d).ok
    try:
        assert rfs.adb.push(SERIAL, str(src), d + "/" + src.name).ok
        assert rfs.stat(d + "/" + src.name).size == 100_000
        back = tmp_path / "back.bin"
        assert rfs.adb.pull(SERIAL, d + "/" + src.name, str(back)).ok
        assert back.read_bytes() == src.read_bytes()
    finally:
        rfs.delete([d + "/" + src.name], recursive=False)
        rfs.delete([d], recursive=False)
