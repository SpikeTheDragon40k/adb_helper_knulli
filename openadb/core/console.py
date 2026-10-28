"""Integrated console helpers: command validation and destructive-command detection.

Two modes exist:

* **Remote shell** — the text is executed by ``sh`` on the selected device.
* **ADB commands** — the text is split with :func:`shlex.split` (no local
  shell, no expansion) and only an allow-listed set of ADB subcommands is run.

There is deliberately no way to run arbitrary local programs.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from openadb.core.paths import sq

# Subcommands that may be run in "ADB commands" mode.
ALLOWED_ADB_COMMANDS = {
    "devices", "version", "connect", "disconnect", "get-state", "get-serialno", "get-devpath", "features",
    "host-features", "shell", "push", "pull", "ls", "reboot", "forward", "reverse", "tcpip", "usb",
    "start-server", "kill-server", "reconnect", "logcat", "root", "unroot", "remount", "wait-for-device",
    "mdns", "pair", "help",
}
# Subcommands that act on a particular device and therefore receive ``-s SERIAL``.
DEVICE_SCOPED = {
    "get-state", "get-serialno", "get-devpath", "features", "shell", "push", "pull", "ls", "reboot", "forward",
    "reverse", "tcpip", "usb", "logcat", "root", "unroot", "remount", "wait-for-device",
}
DESTRUCTIVE_ADB = {
    "reboot": "reboots the device",
    "root": "restarts adbd with root privileges",
    "unroot": "restarts adbd",
    "remount": "remounts system partitions read-write",
    "tcpip": "restarts adbd in TCP mode",
    "usb": "restarts adbd in USB mode",
    "kill-server": "stops the ADB server (all devices disconnect)",
    "push": "may overwrite files on the device",
    "pull": "may overwrite local files",
}

_DESTRUCTIVE_SHELL = (
    (re.compile(r"(^|[\s;&|(`])rm(\s|$)"), "deletes files (rm)"),
    (re.compile(r"(^|[\s;&|(`])rmdir(\s|$)"), "removes directories (rmdir)"),
    (re.compile(r"(^|[\s;&|(`])mv(\s|$)"), "moves/overwrites files (mv)"),
    (re.compile(r"(^|[\s;&|(`])dd(\s|$)"), "raw disk writes (dd)"),
    (re.compile(r"(^|[\s;&|(`])(mkfs(\.\w+)?|mke2fs|mkswap|format)(\s|$)"), "formats a filesystem"),
    (re.compile(r"(^|[\s;&|(`])(fdisk|parted|sfdisk|gdisk|wipefs|blkdiscard)(\s|$)"), "modifies partitions"),
    (re.compile(r"(^|[\s;&|(`])shred(\s|$)"), "destroys file contents (shred)"),
    (re.compile(r"(^|[\s;&|(`])truncate(\s|$)"), "truncates files"),
    (re.compile(r"(^|[\s;&|(`])(chmod|chown|chgrp)(\s|$)"), "changes permissions/ownership"),
    (re.compile(r"(^|[\s;&|(`])(reboot|poweroff|halt|shutdown|init\s+[06])(\s|$)"), "reboots or powers off the device"),
    (re.compile(r"(^|[\s;&|(`])(mount|umount)(\s|$)"), "changes mounts"),
    (re.compile(r"(^|[\s;&|(`])(kill|killall|pkill)(\s|$)"), "terminates processes"),
    (re.compile(r"(^|[\s;&|(`])(cp)(\s|$)"), "may overwrite files (cp)"),
    (re.compile(r"(^|[\s;&|(`])(sed\s+-i|tee)(\s|$)"), "modifies files in place"),
    (re.compile(r"(^|[^<>&2])>(?!&)\s*[^\s&]"), "overwrites a file with output redirection (>)"),
)


def destructive_reasons(command: str) -> list[str]:
    """Return human-readable reasons why a remote shell command looks destructive."""
    reasons = []
    for pattern, reason in _DESTRUCTIVE_SHELL:
        if pattern.search(command):
            reasons.append(reason)
    # "2>/dev/null" and ">/dev/null" are harmless
    if reasons and all(r.startswith("overwrites a file") for r in reasons):
        stripped = re.sub(r"\d?>\s*/dev/null", "", command)
        if not _DESTRUCTIVE_SHELL[-1][0].search(stripped):
            return []
    return reasons


@dataclass
class AdbCommand:
    args: list[str]
    device_scoped: bool
    warnings: list[str]


class ConsoleCommandError(ValueError):
    pass


def parse_adb_command(text: str) -> AdbCommand:
    """Parse ``adb``-mode input into an argv (without the executable)."""
    try:
        args = shlex.split(text, posix=True)
    except ValueError as exc:
        raise ConsoleCommandError(f"Cannot parse command: {exc}") from exc
    if args and args[0] in ("adb", "adb.exe") or (args and args[0].endswith("/adb")):
        args = args[1:]
    if not args:
        raise ConsoleCommandError("Empty command")
    if args[0] in ("-s", "-t", "-H", "-P", "-L", "-a", "-d", "-e"):
        raise ConsoleCommandError("Global options are managed by the application; the selected device is used automatically")
    sub = args[0]
    if sub not in ALLOWED_ADB_COMMANDS:
        raise ConsoleCommandError(
            f"'{sub}' is not an allowed ADB command here. Allowed: " + ", ".join(sorted(ALLOWED_ADB_COMMANDS))
        )
    warnings = []
    if sub in DESTRUCTIVE_ADB:
        warnings.append(DESTRUCTIVE_ADB[sub])
    if sub == "shell" and len(args) > 1:
        warnings += destructive_reasons(" ".join(args[1:]))
    if sub == "logcat" and "-d" not in args:
        warnings.append("logcat streams until cancelled or timed out (use 'logcat -d' to dump)")
    return AdbCommand(args=args, device_scoped=sub in DEVICE_SCOPED, warnings=warnings)


PWD_MARKER = "__OPENADB_PWD="


def build_console_script(command: str, cwd: str | None) -> str:
    """Wrap a user command so the working directory persists between commands."""
    prefix = f"cd -- {sq(cwd)} 2>/dev/null\n" if cwd else ""
    return (
        prefix
        + command
        + "\n__openadb_rc=$?\nprintf '\\n" + PWD_MARKER + "%s\\n' \"$(pwd)\"\nexit $__openadb_rc\n"
    )


def split_console_output(stdout: str) -> tuple[str, str | None]:
    """Remove the trailing working-directory marker. Returns ``(output, cwd)``."""
    idx = stdout.rfind("\n" + PWD_MARKER)
    if idx < 0:
        if stdout.startswith(PWD_MARKER):
            idx = -1
        else:
            return stdout, None
    tail = stdout[idx + 1 + len(PWD_MARKER):]
    cwd = tail.splitlines()[0].strip() if tail else None
    return stdout[: max(idx, 0)], cwd or None
