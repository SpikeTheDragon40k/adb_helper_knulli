"""Helpers to build simulated ADB devices for the test suite.

The default fixture emulates an Anbernic RG35XX SP running Knulli:

* serial ``0123456789`` reported by ADB as ``model:Nexus_4`` (wrong on purpose)
* ``uname -a`` → ``Linux KNULLI 4.9.170 aarch64 GNU/Linux``
* no ``getprop``
* data under ``/userdata``
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

MOCK_ADB = Path(__file__).with_name("adb")

HOST_TOOLS = (
    "sh", "ls", "find", "stat", "df", "du", "sha256sum", "md5sum", "readlink", "od", "mv", "rm", "rmdir",
    "mkdir", "cp", "cat", "tr", "wc", "head", "id", "sleep", "touch", "sed", "grep", "sort", "pwd", "echo",
    "printf", "dd", "ln", "true", "false",
)

UNAME_SCRIPT = """#!/bin/sh
case "$1" in
  -s) echo Linux ;;
  -m) echo aarch64 ;;
  -r) echo 4.9.170 ;;
  -n) echo KNULLI ;;
  -a|"") echo "$MOCK_UNAME_A" ;;
  *) echo Linux ;;
esac
"""

RG35XX_ATTRS = "usb:3-2 product:occam model:Nexus_4 device:mako transport_id:1"


@dataclass
class MockDevice:
    serial: str
    root: Path
    state: str = "device"
    attrs: str = RG35XX_ATTRS
    uname_a: str = "Linux KNULLI 4.9.170 aarch64 GNU/Linux"
    options: dict = field(default_factory=dict)
    exclude_tools: tuple[str, ...] = ()

    def to_json(self) -> dict:
        d = {"serial": self.serial, "state": self.state, "attrs": self.attrs, "root": str(self.root), "uname_a": self.uname_a}
        d.update(self.options)
        return d

    def host(self, remote: str) -> Path:
        return self.root / remote.lstrip("/")


class MockAdbEnv:
    """A set of mock devices plus the state file read by the mock ``adb``."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.state_file = base / "mock_state.json"
        self.call_log = base / "adb_calls.jsonl"
        self.devices: list[MockDevice] = []
        self.connectable: dict[str, MockDevice] = {}
        self.adb_path = str(MOCK_ADB)
        os.chmod(MOCK_ADB, os.stat(MOCK_ADB).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["OPENADB_MOCK_STATE"] = str(self.state_file)
        os.environ["OPENADB_MOCK_LOG"] = str(self.call_log)

    def make_root(self, name: str, exclude_tools: tuple[str, ...] = ()) -> Path:
        root = self.base / name
        (root / ".mockbin").mkdir(parents=True, exist_ok=True)
        for tool in HOST_TOOLS:
            if tool in exclude_tools:
                continue
            found = shutil.which(tool)
            if found:
                link = root / ".mockbin" / tool
                if not link.exists():
                    link.symlink_to(found)
        uname = root / ".mockbin" / "uname"
        uname.write_text(UNAME_SCRIPT)
        uname.chmod(0o755)
        return root

    def add_device(self, serial: str = "0123456789", *, knulli: bool = True, connectable_only: bool = False,
                   exclude_tools: tuple[str, ...] = (), **options: object) -> MockDevice:
        root = self.make_root(f"dev_{serial.replace(':', '_').replace('.', '_')}", exclude_tools)
        if knulli:
            populate_knulli(root)
        dev = MockDevice(serial=serial, root=root, options=dict(options), exclude_tools=exclude_tools)
        if serial.count(":") == 1:
            dev.attrs = "product:occam model:Nexus_4 device:mako transport_id:2"
        if connectable_only:
            self.connectable[serial] = dev
        else:
            self.devices.append(dev)
        self.save()
        return dev

    def set_option(self, serial: str, key: str, value: object) -> None:
        for d in self.devices:
            if d.serial == serial:
                if value is None:
                    d.options.pop(key, None)
                else:
                    d.options[key] = value
        self.save()

    def set_state(self, serial: str, state: str) -> None:
        for d in self.devices:
            if d.serial == serial:
                d.state = state
        self.save()

    def remove_device(self, serial: str) -> None:
        self.devices = [d for d in self.devices if d.serial != serial]
        self.save()

    def reload(self) -> None:
        """Pick up changes the mock adb made (connect/disconnect/state)."""
        data = json.loads(self.state_file.read_text())
        by_serial = {d.serial: d for d in self.devices + list(self.connectable.values())}
        self.devices = []
        for d in data["devices"]:
            dev = by_serial.get(d["serial"])
            if dev is None:
                continue
            dev.state = d["state"]
            self.devices.append(dev)

    def save(self) -> None:
        state = {
            "devices": [d.to_json() for d in self.devices],
            "connectable": {k: v.to_json() for k, v in self.connectable.items()},
        }
        self.state_file.write_text(json.dumps(state))

    def calls(self) -> list[list[str]]:
        if not self.call_log.exists():
            return []
        return [json.loads(line) for line in self.call_log.read_text().splitlines() if line.strip()]


def populate_knulli(root: Path) -> None:
    for d in ("userdata/roms/gba", "userdata/roms/snes", "userdata/roms/nds", "userdata/roms/psx",
              "userdata/bios", "userdata/saves/gba", "userdata/system/logs", "userdata/screenshots",
              "usr/share/batocera"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "usr/share/batocera/batocera.version").write_text("40-knulli-scarab 2024/11/01\n")
    (root / "etc").mkdir(exist_ok=True)
    (root / "etc/os-release").write_text('NAME="KNULLI"\nPRETTY_NAME="KNULLI Scarab"\nID=knulli\n')
    gba = root / "userdata/roms/gba"
    (gba / "Pokemon FireRed.gba").write_bytes(os.urandom(4096))
    (gba / "Mario & Luigi (USA) [!].gba").write_bytes(b"\x01" * 2048)
    (gba / "It's a Test's ROM.gba").write_bytes(b"\x02" * 100)
    (gba / "ポケモン ルビー.gba").write_bytes(b"\x03" * 333)
    (gba / ".hidden.gba").write_bytes(b"x")
    (gba / "gamelist.xml").write_text("<gameList/>\n")
    (root / "userdata/system/logs/es_log.txt").write_text("log\n")
    (root / "userdata/bios/gba_bios.bin").write_bytes(b"\x00" * 16384)
