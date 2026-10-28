"""Subprocess execution with explicit argv, timeouts, cancellation and streaming output.

``shell=True`` is never used. Streaming mode can attach the child's stdout to a
pseudo terminal on POSIX systems, which is required for the ADB client to emit
its interactive progress output.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from openadb.core.errors import AdbNotFoundError

IS_WINDOWS = sys.platform.startswith("win")
PTY_AVAILABLE = not IS_WINDOWS
_POPEN_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


class CancelToken:
    """Thread-safe cancellation flag that can also notify registered callbacks."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb()
            except Exception:  # noqa: BLE001 - cancellation must not raise
                pass

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def add_callback(self, cb: Callable[[], None]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(cb)
                return
        cb()

    def remove_callback(self, cb: Callable[[], None]) -> None:
        with self._lock:
            if cb in self._callbacks:
                self._callbacks.remove(cb)

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass
class ProcessResult:
    argv: list[str]
    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    duration: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", "replace")


def _spawn(argv: Sequence[str], **kwargs: object) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(list(argv), creationflags=_POPEN_FLAGS, **kwargs)  # type: ignore[call-overload]
    except FileNotFoundError as exc:
        raise AdbNotFoundError(f"Executable not found: {argv[0]}") from exc
    except PermissionError as exc:
        raise AdbNotFoundError(f"Executable is not runnable: {argv[0]} ({exc})") from exc


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except OSError:
        pass


def run_process(
    argv: Sequence[str],
    *,
    timeout: float | None = None,
    cancel: CancelToken | None = None,
    env: dict[str, str] | None = None,
    stdin_data: bytes | None = None,
) -> ProcessResult:
    """Run ``argv`` to completion capturing stdout/stderr."""
    start = time.monotonic()
    proc = _spawn(
        argv,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    timed_out = cancelled = False
    kill_cb = lambda: _terminate(proc)  # noqa: E731
    if cancel is not None:
        cancel.add_callback(kill_cb)
    try:
        deadline = None if timeout is None else start + timeout
        input_data = stdin_data
        while True:
            try:
                out, err = proc.communicate(input=input_data, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                input_data = None
                if cancel is not None and cancel.cancelled:
                    cancelled = True
                    _terminate(proc)
                elif deadline is not None and time.monotonic() > deadline:
                    timed_out = True
                    _terminate(proc)
    finally:
        if cancel is not None:
            cancel.remove_callback(kill_cb)
    if cancel is not None and cancel.cancelled:
        cancelled = True
    return ProcessResult(
        argv=list(argv),
        returncode=proc.returncode,
        stdout=out or b"",
        stderr=err or b"",
        duration=time.monotonic() - start,
        timed_out=timed_out,
        cancelled=cancelled,
    )


def run_streaming(
    argv: Sequence[str],
    on_output: Callable[[str], None],
    *,
    cancel: CancelToken | None = None,
    timeout: float | None = None,
    use_pty: bool = True,
    env: dict[str, str] | None = None,
    capture_limit: int = 256 * 1024,
) -> ProcessResult:
    """Run ``argv`` delivering decoded stdout chunks to ``on_output`` as they arrive.

    When ``use_pty`` is true and the platform supports it, stdout is attached to
    a pseudo terminal so that tools which only print progress to terminals
    (such as ``adb push``/``adb pull``) do so. stderr is always a plain pipe.
    """
    start = time.monotonic()
    child_env = dict(os.environ if env is None else env)
    pty_used = bool(use_pty and PTY_AVAILABLE)
    stdout_buf = bytearray()
    stderr_buf = bytearray()

    if pty_used:
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        # A wide terminal prevents the ADB client from eliding long paths.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 400, 0, 0))
        attrs = termios.tcgetattr(slave)
        attrs[1] &= ~termios.ONLCR  # keep newlines untouched
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        child_env.setdefault("TERM", "xterm-256color")
        if child_env.get("TERM") == "dumb":
            child_env["TERM"] = "xterm-256color"
        try:
            proc = _spawn(argv, stdin=subprocess.DEVNULL, stdout=slave, stderr=subprocess.PIPE, env=child_env)
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
    else:
        master = -1
        proc = _spawn(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env)

    def pump_stderr() -> None:
        assert proc.stderr is not None
        for chunk in iter(lambda: proc.stderr.read1(4096) if proc.stderr else b"", b""):  # type: ignore[union-attr]
            if len(stderr_buf) < capture_limit:
                stderr_buf.extend(chunk)

    def deliver(chunk: bytes) -> None:
        if len(stdout_buf) < capture_limit:
            stdout_buf.extend(chunk)
        try:
            on_output(chunk.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - a broken listener must not kill the transfer
            pass

    err_thread = threading.Thread(target=pump_stderr, daemon=True)
    err_thread.start()

    timed_out = cancelled = False
    kill_cb = lambda: _terminate(proc)  # noqa: E731
    if cancel is not None:
        cancel.add_callback(kill_cb)
    deadline = None if timeout is None else start + timeout
    try:
        if pty_used:
            import select

            while True:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    deliver(chunk)
                elif proc.poll() is not None:
                    # Drain whatever is left.
                    while True:
                        r, _, _ = select.select([master], [], [], 0)
                        if not r:
                            break
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:
                            chunk = b""
                        if not chunk:
                            break
                        deliver(chunk)
                    break
                if cancel is not None and cancel.cancelled:
                    cancelled = True
                    _terminate(proc)
                if deadline is not None and time.monotonic() > deadline and proc.poll() is None:
                    timed_out = True
                    _terminate(proc)
        else:
            def pump_stdout() -> None:
                assert proc.stdout is not None
                for chunk in iter(lambda: proc.stdout.read1(4096) if proc.stdout else b"", b""):  # type: ignore[union-attr]
                    deliver(chunk)

            out_thread = threading.Thread(target=pump_stdout, daemon=True)
            out_thread.start()
            while out_thread.is_alive():
                out_thread.join(0.1)
                if cancel is not None and cancel.cancelled and proc.poll() is None:
                    cancelled = True
                    _terminate(proc)
                if deadline is not None and time.monotonic() > deadline and proc.poll() is None:
                    timed_out = True
                    _terminate(proc)
        proc.wait()
    finally:
        if cancel is not None:
            cancel.remove_callback(kill_cb)
        if pty_used:
            try:
                os.close(master)
            except OSError:
                pass
        err_thread.join(2.0)
    if cancel is not None and cancel.cancelled:
        cancelled = True
    return ProcessResult(
        argv=list(argv),
        returncode=proc.returncode,
        stdout=bytes(stdout_buf),
        stderr=bytes(stderr_buf),
        duration=time.monotonic() - start,
        timed_out=timed_out,
        cancelled=cancelled,
        extra={"pty": pty_used},
    )
