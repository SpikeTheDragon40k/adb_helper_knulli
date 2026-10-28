"""ADB client wrapper.

Every device-specific command is issued with an explicit ``-s SERIAL``. Remote
shell scripts are passed as a single argument (never through a local shell)
and wrapped so that their exit status is recoverable even on ADB daemons that
do not implement the ``shell_v2`` protocol.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from openadb.core.devices import DeviceInfo, parse_devices_output
from openadb.core.errors import (
    AdbCommandError,
    AdbNotFoundError,
    CommandTimeoutError,
    DeviceUnavailableError,
    OperationCancelled,
    ParseError,
)
from openadb.core.process import CancelToken, ProcessResult, run_process, run_streaming

RC_MARKER = b"__OPENADB_RC="
ERR_MARKER = b"--OPENADB-ERR--"

_DEVICE_GONE_PATTERNS = (
    re.compile(r"device '?[^']*'? not found", re.I),
    re.compile(r"no devices/emulators found", re.I),
    re.compile(r"device offline", re.I),
    re.compile(r"device unauthorized", re.I),
    re.compile(r"device still authorizing", re.I),
    re.compile(r"closed\s*$", re.I),
    re.compile(r"protocol fault", re.I),
    re.compile(r"connection reset", re.I),
    re.compile(r"no permissions", re.I),
)


def looks_like_device_gone(text: str) -> bool:
    return any(p.search(text) for p in _DEVICE_GONE_PATTERNS)


@dataclass(frozen=True)
class AdbVersion:
    raw: str
    protocol: str | None
    release: str | None

    @property
    def summary(self) -> str:
        if self.protocol:
            return f"ADB {self.protocol}" + (f" ({self.release})" if self.release else "")
        lines = self.raw.strip().splitlines()
        return lines[0] if lines else "unknown"


def parse_version(text: str) -> AdbVersion:
    proto = re.search(r"Android Debug Bridge version\s+(\S+)", text)
    release = re.search(r"^Version\s+(\S+)", text, re.M)
    return AdbVersion(raw=text, protocol=proto.group(1) if proto else None, release=release.group(1) if release else None)


@dataclass
class ShellResult:
    """Outcome of a wrapped remote script."""

    stdout: bytes
    stderr: str
    returncode: int | None
    duration: float
    marker_found: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def resolve_adb_executable(configured: str | None) -> str:
    """Return a usable ADB executable path or raise :class:`AdbNotFoundError`."""
    candidates: list[str] = []
    if configured:
        candidates.append(os.path.expanduser(configured))
    candidates.append("adb")
    for cand in candidates:
        if os.path.sep in cand or (os.path.altsep and os.path.altsep in cand):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    raise AdbNotFoundError(
        "ADB executable not found. Install android-tools (Arch: 'sudo pacman -S android-tools') "
        "or set the path in Settings."
    )


class AdbClient:
    """Thin, thread-safe wrapper around the ``adb`` command-line client."""

    def __init__(
        self,
        executable: str | None = None,
        server_host: str | None = None,
        server_port: int | None = None,
        default_timeout: float = 30.0,
    ) -> None:
        self.executable = resolve_adb_executable(executable)
        self.server_host = server_host or None
        self.server_port = server_port or None
        self.default_timeout = default_timeout

    # ------------------------------------------------------------------ basics
    def base_argv(self, serial: str | None = None) -> list[str]:
        argv = [self.executable]
        if self.server_host and self.server_host not in ("127.0.0.1", "localhost"):
            argv += ["-H", self.server_host]
        if self.server_port and int(self.server_port) != 5037:
            argv += ["-P", str(int(self.server_port))]
        if serial:
            argv += ["-s", serial]
        return argv

    def run(
        self,
        args: Sequence[str],
        serial: str | None = None,
        timeout: float | None = None,
        cancel: CancelToken | None = None,
    ) -> ProcessResult:
        argv = self.base_argv(serial) + list(args)
        return run_process(argv, timeout=self.default_timeout if timeout is None else timeout, cancel=cancel)

    def version(self) -> AdbVersion:
        res = self.run(["version"], timeout=15)
        return parse_version(res.stdout_text)

    def start_server(self) -> ProcessResult:
        return self.run(["start-server"], timeout=30)

    def devices(self, timeout: float = 20.0) -> list[DeviceInfo]:
        res = self.run(["devices", "-l"], timeout=timeout)
        if res.timed_out:
            raise CommandTimeoutError("'adb devices -l' timed out")
        if res.returncode != 0:
            raise AdbCommandError(f"'adb devices -l' failed: {res.stderr_text.strip()}", res.returncode, res.stderr_text)
        return parse_devices_output(res.stdout_text)

    def connect(self, target: str, timeout: float = 20.0) -> tuple[bool, str]:
        res = self.run(["connect", target], timeout=timeout)
        text = (res.stdout_text + res.stderr_text).strip()
        ok = res.returncode == 0 and ("connected to" in text.lower()) and "cannot" not in text.lower() and "failed" not in text.lower()
        return ok, text or ("timed out" if res.timed_out else "no output")

    def disconnect(self, target: str, timeout: float = 15.0) -> tuple[bool, str]:
        res = self.run(["disconnect", target], timeout=timeout)
        text = (res.stdout_text + res.stderr_text).strip()
        return res.returncode == 0 and "error" not in text.lower(), text

    def features(self, serial: str) -> set[str]:
        res = self.run(["features"], serial=serial, timeout=10)
        if res.returncode != 0:
            return set()
        return {f.strip() for f in res.stdout_text.replace("\n", ",").split(",") if f.strip()}

    # ------------------------------------------------------------------ shell
    def shell_raw(
        self, serial: str, command: str, timeout: float | None = None, cancel: CancelToken | None = None
    ) -> ProcessResult:
        """Run ``command`` (one string, interpreted by the remote ``sh``)."""
        return self.run(["shell", command], serial=serial, timeout=timeout, cancel=cancel)

    def run_script(
        self,
        serial: str,
        script: str,
        *,
        timeout: float | None = None,
        cancel: CancelToken | None = None,
        transport: str = "raw",
    ) -> ShellResult:
        """Run a POSIX shell script remotely and recover its exit status.

        ``transport`` selects how output travels back:

        * ``raw``     — bytes as produced (binary-safe ADB shells)
        * ``hex``     — hex-encoded with ``od`` (PTY-mangled shells)
        * ``limited`` — raw, but CRLF translation is undone (lossy for odd names)
        """
        if transport == "hex":
            wrapped = (
                "exec 3>&1\n"
                "__oe=$( { { (\n" + script + "\n); printf '\\n__OPENADB_RC=%d\\n' \"$?\"; } "
                "| od -An -v -tx1 >&3; } 2>&1 )\n"
                "printf '\\n--OPENADB-ERR--\\n'\n"
                "printf '%s' \"$__oe\" | od -An -v -tx1\n"
            )
        else:
            wrapped = "(\n" + script + "\n)\nprintf '\\n__OPENADB_RC=%d\\n' \"$?\"\n"
        res = self.shell_raw(serial, wrapped, timeout=timeout, cancel=cancel)
        return self._finish_script(serial, res, transport)

    def _finish_script(self, serial: str, res: ProcessResult, transport: str) -> ShellResult:
        if res.cancelled:
            raise OperationCancelled("Operation cancelled")
        if res.timed_out:
            raise CommandTimeoutError(f"Remote command on {serial} timed out after {res.duration:.1f}s")
        stdout = res.stdout
        stderr = res.stderr_text
        if transport == "hex":
            text = stdout.decode("ascii", "replace")
            data_part, sep, err_part = text.partition(ERR_MARKER.decode())
            if not sep:
                self._raise_if_gone(serial, res)
                raise ParseError("Hex transport output is incomplete")
            try:
                stdout = bytes.fromhex("".join(data_part.split()))
                extra_err = bytes.fromhex("".join(err_part.split())).decode("utf-8", "replace")
            except ValueError as exc:
                raise ParseError(f"Hex transport output is corrupted: {exc}") from exc
            stderr = (stderr + extra_err).strip()
        elif transport == "limited":
            stdout = stdout.replace(b"\r\n", b"\n")
        idx = stdout.rfind(b"\n" + RC_MARKER)
        if idx < 0:
            self._raise_if_gone(serial, res)
            return ShellResult(stdout, stderr, res.returncode, res.duration, False)
        tail = stdout[idx + 1 + len(RC_MARKER):]
        digits = re.match(rb"\s*(-?\d+)", tail)
        rc = int(digits.group(1)) if digits else res.returncode
        return ShellResult(stdout[:idx], stderr, rc, res.duration, True)

    def _raise_if_gone(self, serial: str, res: ProcessResult) -> None:
        text = res.stderr_text + res.stdout_text[-400:]
        if res.returncode not in (0, None) and looks_like_device_gone(text):
            raise DeviceUnavailableError(f"Device {serial} is not available: {text.strip()}")

    # ------------------------------------------------------------------ sync
    def _sync(
        self,
        verb: str,
        serial: str,
        src: str,
        dst: str,
        on_output: Callable[[str], None] | None,
        cancel: CancelToken | None,
        use_pty: bool,
        timeout: float | None,
    ) -> ProcessResult:
        argv = self.base_argv(serial) + [verb, src, dst]
        return run_streaming(argv, on_output or (lambda _t: None), cancel=cancel, timeout=timeout, use_pty=use_pty)

    def push(
        self,
        serial: str,
        local: str,
        remote: str,
        on_output: Callable[[str], None] | None = None,
        cancel: CancelToken | None = None,
        use_pty: bool = True,
        timeout: float | None = None,
    ) -> ProcessResult:
        return self._sync("push", serial, local, remote, on_output, cancel, use_pty, timeout)

    def pull(
        self,
        serial: str,
        remote: str,
        local: str,
        on_output: Callable[[str], None] | None = None,
        cancel: CancelToken | None = None,
        use_pty: bool = True,
        timeout: float | None = None,
    ) -> ProcessResult:
        return self._sync("pull", serial, remote, local, on_output, cancel, use_pty, timeout)
