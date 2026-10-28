"""Non-destructive device capability detection.

The probe never relies on Android framework services. It checks, one by one,
whether a shell works, which POSIX utilities exist, whether the shell channel
is binary-safe, and whether the device looks like a Knulli/Batocera system.
Classification is purely informational: file management works for every
device that has a working shell and the ADB sync protocol.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from openadb.core.adb import AdbClient
from openadb.core.errors import OpenADBError
from openadb.core.logbook import LogBook
from openadb.core.process import CancelToken

PROBED_COMMANDS = (
    "ls", "find", "stat", "df", "du", "sha256sum", "md5sum", "sha1sum", "readlink", "od",
    "mv", "rm", "rmdir", "mkdir", "cp", "cat", "tr", "wc", "head", "getprop", "curl", "wget",
    "busybox", "toybox",
)

BINARY_PROBE_CMD = "printf 'A\\000B\\nC\\rD'"
BINARY_PROBE_EXPECT = b"A\x00B\nC\rD"


class DeviceClass(str, Enum):
    ANDROID = "Android-compatible"
    GENERIC_LINUX = "Generic Linux ADB"
    MINIMAL = "Minimal ADB"
    UNKNOWN = "Unknown"


@dataclass
class DeviceCapabilities:
    serial: str
    shell_ok: bool = False
    os_name: str = ""
    arch: str = ""
    kernel: str = ""
    hostname: str = ""
    uname_a: str = ""
    commands: dict[str, str] = field(default_factory=dict)
    stat_format_ok: bool = False
    binary_safe: bool = False
    transport: str = "raw"
    has_getprop: bool = False
    android_model: str = ""
    android_release: str = ""
    os_release: dict[str, str] = field(default_factory=dict)
    distro_version: str = ""
    existing_dirs: list[str] = field(default_factory=list)
    knulli: bool = False
    batocera_like: bool = False
    uid: str = ""
    device_class: DeviceClass = DeviceClass.UNKNOWN
    notes: list[str] = field(default_factory=list)
    probe_duration: float = 0.0
    adb_reported_model: str = ""

    def has(self, command: str) -> bool:
        return command in self.commands

    @property
    def listing_strategy(self) -> str:
        if self.has("stat") and self.stat_format_ok:
            return "stat"
        return "test"

    @property
    def hash_command(self) -> tuple[str, str] | None:
        if self.has("sha256sum"):
            return "sha256sum", "sha256"
        if self.has("md5sum"):
            return "md5sum", "md5"
        return None

    @property
    def os_pretty(self) -> str:
        pretty = self.os_release.get("PRETTY_NAME") or self.os_release.get("NAME")
        base = f"{self.os_name or '?'} {self.arch}".strip()
        return f"{pretty} ({base})" if pretty else base

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["device_class"] = self.device_class.value
        return d


def _parse_kv(text: str) -> tuple[dict[str, str], dict[str, str], list[str], dict[str, str]]:
    info: dict[str, str] = {}
    cmds: dict[str, str] = {}
    dirs: list[str] = []
    osr: dict[str, str] = {}
    for line in text.splitlines():
        line = line.rstrip("\r")
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key.startswith("cmd."):
            if value.strip():
                cmds[key[4:]] = value.strip()
        elif key == "dir":
            dirs.append(value)
        elif key == "osr":
            k, s2, v = value.partition("=")
            if s2:
                osr[k.strip()] = v.strip().strip('"').strip("'")
        else:
            info[key] = value.strip()
    return info, cmds, dirs, osr


def build_probe_script(knulli_dirs: tuple[str, ...]) -> str:
    cmds = " ".join(PROBED_COMMANDS)
    dirs = " ".join("'" + d + "'" for d in knulli_dirs)
    return f"""
printf 'os=%s\\n' "$(uname -s 2>/dev/null)"
printf 'arch=%s\\n' "$(uname -m 2>/dev/null)"
printf 'kernel=%s\\n' "$(uname -r 2>/dev/null)"
printf 'host=%s\\n' "$(uname -n 2>/dev/null)"
printf 'uname_a=%s\\n' "$(uname -a 2>/dev/null)"
printf 'uid=%s\\n' "$(id -u 2>/dev/null)"
for c in {cmds}; do
  p=$(command -v "$c" 2>/dev/null) && printf 'cmd.%s=%s\\n' "$c" "$p"
done
if stat -c '%f %s %Y' / >/dev/null 2>&1; then echo stat_c=1; fi
if [ -r /etc/os-release ]; then
  while IFS= read -r l || [ -n "$l" ]; do printf 'osr=%s\\n' "$l"; done < /etc/os-release
fi
for f in /usr/share/batocera/batocera.version /usr/share/knulli/version /etc/knulli-release; do
  if [ -r "$f" ]; then IFS= read -r v < "$f"; printf 'ver=%s\\n' "$v"; printf 'verfile=%s\\n' "$f"; fi
done
for d in {dirs}; do [ -d "$d" ] && printf 'dir=%s\\n' "$d"; done
exit 0
"""


def probe_capabilities(
    adb: AdbClient,
    serial: str,
    *,
    logbook: LogBook | None = None,
    cancel: CancelToken | None = None,
    adb_reported_model: str = "",
) -> DeviceCapabilities:
    from openadb.core.knulli import KNULLI_PROBE_DIRS

    caps = DeviceCapabilities(serial=serial, adb_reported_model=adb_reported_model)
    start = time.monotonic()

    def log(level: str, msg: str, **kw: Any) -> None:
        if logbook:
            getattr(logbook, level)(msg, device=serial, op_type="probe", **kw)

    # 1. Does a shell work at all?
    res = adb.shell_raw(serial, "echo __OPENADB_OK__", timeout=20, cancel=cancel)
    if b"__OPENADB_OK__" not in res.stdout:
        caps.notes.append("Shell unavailable: " + (res.stderr_text or res.stdout_text).strip())
        caps.device_class = DeviceClass.UNKNOWN
        caps.probe_duration = time.monotonic() - start
        log("error", "Capability probe: shell unavailable", error=caps.notes[-1])
        return caps
    caps.shell_ok = True

    # 2. Is the shell channel binary safe (no PTY CRLF translation)?
    res = adb.shell_raw(serial, BINARY_PROBE_CMD, timeout=20, cancel=cancel)
    caps.binary_safe = res.stdout == BINARY_PROBE_EXPECT

    # 3. OS info, utilities, Knulli markers
    try:
        sres = adb.run_script(serial, build_probe_script(KNULLI_PROBE_DIRS), timeout=30, cancel=cancel,
                              transport="raw" if caps.binary_safe else "limited")
        text = sres.stdout.decode("utf-8", "replace")
    except OpenADBError as exc:
        text = ""
        caps.notes.append(f"Probe script failed: {exc}")
    info, cmds, dirs, osr = _parse_kv(text)
    caps.os_name = info.get("os", "")
    caps.arch = info.get("arch", "")
    caps.kernel = info.get("kernel", "")
    caps.hostname = info.get("host", "")
    caps.uname_a = info.get("uname_a", "")
    caps.uid = info.get("uid", "")
    caps.commands = cmds
    caps.stat_format_ok = info.get("stat_c") == "1"
    caps.existing_dirs = dirs
    caps.os_release = osr
    caps.distro_version = info.get("ver", "")

    if caps.binary_safe:
        caps.transport = "raw"
    elif caps.has("od"):
        caps.transport = "hex"
        caps.notes.append("Shell channel is not binary-safe; using hex-encoded transport")
    else:
        caps.transport = "limited"
        caps.notes.append("Shell channel is not binary-safe and 'od' is missing; names with newlines are unsupported")

    log("info", f"OS detected: {caps.os_name or 'unknown'} {caps.arch}".strip(), extra={"uname": caps.uname_a})

    # 4. Android framework (optional)
    caps.has_getprop = caps.has("getprop")
    if caps.has_getprop:
        r = adb.shell_raw(serial, "getprop ro.product.model; getprop ro.build.version.release", timeout=15, cancel=cancel)
        lines = r.stdout_text.replace("\r", "").splitlines() + ["", ""]
        caps.android_model, caps.android_release = lines[0].strip(), lines[1].strip()
    else:
        log("warning", "Android getprop unavailable")

    # 5. Knulli / Batocera detection (never from the ADB model string)
    hay = " ".join([caps.hostname, caps.uname_a, caps.distro_version, " ".join(osr.values())]).lower()
    caps.knulli = "knulli" in hay
    caps.batocera_like = caps.knulli or "batocera" in hay or "/userdata/roms" in dirs

    # 6. Classification
    has_listing = caps.has("ls") or caps.has("stat") or caps.has("find")
    if caps.has_getprop and caps.android_model:
        caps.device_class = DeviceClass.ANDROID
    elif caps.os_name.lower() == "linux" and caps.has("stat") and caps.stat_format_ok:
        caps.device_class = DeviceClass.GENERIC_LINUX
    elif has_listing or caps.os_name:
        caps.device_class = DeviceClass.MINIMAL
    else:
        caps.device_class = DeviceClass.MINIMAL
    missing = [c for c in ("stat", "find", "df", "sha256sum", "md5sum", "readlink") if not caps.has(c)]
    if missing:
        caps.notes.append("Missing utilities: " + ", ".join(missing))
    caps.probe_duration = time.monotonic() - start
    log(
        "info",
        f"{caps.device_class.value} mode enabled" + (" (Knulli detected)" if caps.knulli else ""),
        duration=caps.probe_duration,
        result="success",
        extra={"transport": caps.transport, "commands": ",".join(sorted(caps.commands)), "missing": ",".join(missing)},
    )
    if caps.adb_reported_model and not caps.has_getprop:
        log("info", f"ADB-reported model '{caps.adb_reported_model}' ignored for identification (no Android framework)")
    return caps
