"""Parsing of ``adb devices -l`` and device-state diffing.

ADB-reported metadata (model, product, device) is kept for display only; it is
frequently inaccurate on non-Android systems and is never used to infer the
real hardware or operating system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

_TCP_SERIAL = re.compile(r"^(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.\-]+):\d{1,5}$")
_KV = re.compile(r"(\S+?):(\S+)")


class DeviceState(str, Enum):
    DEVICE = "device"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    AUTHORIZING = "authorizing"
    NO_PERMISSIONS = "no permissions"
    RECOVERY = "recovery"
    SIDELOAD = "sideload"
    BOOTLOADER = "bootloader"
    CONNECTING = "connecting"
    HOST = "host"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, text: str) -> "DeviceState":
        for member in cls:
            if member.value == text:
                return member
        return cls.UNKNOWN


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    state: DeviceState
    transport: str  # "usb", "tcp" or "unknown"
    attributes: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    state_detail: str = ""

    @property
    def usable(self) -> bool:
        return self.state in (DeviceState.DEVICE, DeviceState.RECOVERY)

    @property
    def reported_model(self) -> str:
        return self.attributes.get("model", "").replace("_", " ")

    @property
    def usb_path(self) -> str:
        return self.attributes.get("usb", "")

    def describe(self) -> str:
        model = self.reported_model
        bits = [self.serial, self.state.value]
        if model:
            bits.append(f"ADB-reported model: {model}")
        if self.usb_path:
            bits.append(f"usb:{self.usb_path}")
        return " · ".join(bits)


def classify_transport(serial: str, attributes: dict[str, str]) -> str:
    if "usb" in attributes:
        return "usb"
    if _TCP_SERIAL.match(serial) or "._adb-tls-connect._tcp" in serial or "._adb._tcp" in serial:
        return "tcp"
    if serial.startswith("emulator-"):
        return "emulator"
    return "unknown"


def parse_devices_output(text: str) -> list[DeviceInfo]:
    """Parse ``adb devices -l`` output into :class:`DeviceInfo` records."""
    devices: list[DeviceInfo] = []
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("List of devices"):
            started = True
            continue
        if line.startswith("*") or line.lower().startswith("adb server") or line.lower().startswith("adb:"):
            continue
        if not started and "\t" not in raw and len(line.split()) < 2:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        serial, rest = parts
        detail = ""
        if rest.startswith("no permissions"):
            state = DeviceState.NO_PERMISSIONS
            # "no permissions (reason); see [url] usb:3-2 transport_id:1"
            m = re.match(r"no permissions(.*?)(?=\s+\S+:\S+(\s|$)|$)", rest)
            detail = (m.group(1).strip() if m else "").strip()
            attrs_text = rest[m.end():] if m else ""
        else:
            state_text, _, attrs_text = rest.partition(" ")
            state = DeviceState.parse(state_text)
        attrs = {k: v for k, v in _KV.findall(attrs_text) if "http" not in k and not k.startswith("[")}
        devices.append(
            DeviceInfo(
                serial=serial,
                state=state,
                transport=classify_transport(serial, attrs),
                attributes=attrs,
                state_detail=detail,
            )
        )
    return devices


@dataclass
class DeviceDiff:
    added: list[DeviceInfo]
    removed: list[DeviceInfo]
    changed: list[tuple[DeviceInfo, DeviceInfo]]  # (old, new)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff_devices(old: list[DeviceInfo], new: list[DeviceInfo]) -> DeviceDiff:
    old_map = {d.serial: d for d in old}
    new_map = {d.serial: d for d in new}
    added = [d for s, d in new_map.items() if s not in old_map]
    removed = [d for s, d in old_map.items() if s not in new_map]
    changed = [
        (old_map[s], d)
        for s, d in new_map.items()
        if s in old_map and (old_map[s].state != d.state or old_map[s].attributes != d.attributes)
    ]
    return DeviceDiff(added, removed, changed)
