"""Structured activity log.

Every significant operation is recorded as a :class:`LogEvent`. Events are kept
in memory for the live viewer and appended to a rotating JSONL file in the
application data directory. Nothing is ever uploaded anywhere.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any

_std_logger = logging.getLogger("openadb")


class Level(IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40

    @property
    def label(self) -> str:
        return {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR"}[int(self)]

    @classmethod
    def parse(cls, value: str | int) -> "Level":
        if isinstance(value, int):
            return cls(value)
        v = value.strip().upper()
        if v == "WARN":
            v = "WARNING"
        return cls[v]


_REDACTIONS = (
    (re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|auth(?:orization)?|bearer)\b(\s*[=:]\s*|\s+)(\S+)"), r"\1\2***"),
    (re.compile(r"(?i)(https?://[^:/\s]+:)([^@/\s]+)(@)"), r"\1***\3"),
    (re.compile(r"(?i)\b(pairing code|pair)\s+(\S+:\d+)\s+(\d{6})\b"), r"\1 \2 ******"),
)


def redact(text: str | None) -> str | None:
    if not text:
        return text
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def new_op_id(prefix: str = "") -> str:
    short = uuid.uuid4().hex[:8]
    return f"{prefix}-{short}" if prefix else short


@dataclass
class LogEvent:
    ts: datetime
    level: Level
    message: str
    device: str | None = None
    op_id: str | None = None
    op_type: str | None = None
    source: str | None = None
    destination: str | None = None
    duration: float | None = None
    result: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat(timespec="milliseconds")
        d["level"] = self.level.label
        return {k: v for k, v in d.items() if v not in (None, {}, "")}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LogEvent":
        return cls(
            ts=datetime.fromisoformat(d["ts"]),
            level=Level.parse(d.get("level", "INFO")),
            message=d.get("message", ""),
            device=d.get("device"),
            op_id=d.get("op_id"),
            op_type=d.get("op_type"),
            source=d.get("source"),
            destination=d.get("destination"),
            duration=d.get("duration"),
            result=d.get("result"),
            error=d.get("error"),
            extra=d.get("extra") or {},
            seq=d.get("seq", 0),
        )

    def format_text(self, with_date: bool = True) -> str:
        stamp = self.ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if with_date else self.ts.strftime("%H:%M:%S.%f")[:-3]
        tz = self.ts.strftime("%z")
        parts = [f"[{stamp} {tz}]", f"[{self.level.label}]"]
        if self.device:
            parts.append(f"[{self.device}]")
        if self.op_type or self.op_id:
            parts.append(f"[{self.op_type or 'op'}{'#' + self.op_id if self.op_id else ''}]")
        parts.append(self.message)
        details = []
        if self.source:
            details.append(f"src={self.source!r}")
        if self.destination:
            details.append(f"dst={self.destination!r}")
        if self.duration is not None:
            details.append(f"duration={self.duration:.3f}s")
        if self.result:
            details.append(f"result={self.result}")
        if self.error:
            details.append(f"error={self.error!r}")
        for k, v in self.extra.items():
            details.append(f"{k}={v!r}")
        if details:
            parts.append("| " + " ".join(details))
        return " ".join(parts)


Listener = Callable[[LogEvent], None]


class LogBook:
    """Thread-safe structured log with live subscribers, rotation and retention."""

    FILE_PREFIX = "activity"

    def __init__(
        self,
        directory: Path | None,
        *,
        level: Level = Level.INFO,
        max_bytes: int = 5 * 1024 * 1024,
        backups: int = 10,
        retention_days: int = 30,
        memory_limit: int = 20000,
    ) -> None:
        self.directory = directory
        self.level = level
        self.max_bytes = max_bytes
        self.backups = backups
        self.retention_days = retention_days
        self._events: deque[LogEvent] = deque(maxlen=memory_limit)
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()
        self._seq = 0
        self._fh = None
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            self.apply_retention()

    # ----------------------------------------------------------------- files
    @property
    def current_file(self) -> Path | None:
        return self.directory / f"{self.FILE_PREFIX}.jsonl" if self.directory else None

    def log_files(self) -> list[Path]:
        if not self.directory or not self.directory.exists():
            return []
        files = sorted(self.directory.glob(f"{self.FILE_PREFIX}*.jsonl"), key=lambda p: p.stat().st_mtime)
        return files

    def _open(self) -> None:
        if self._fh is None and self.current_file is not None:
            self._fh = open(self.current_file, "a", encoding="utf-8")  # noqa: SIM115

    def _rotate_if_needed(self) -> None:
        path = self.current_file
        if path is None or self._fh is None:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self.max_bytes:
            return
        self._fh.close()
        self._fh = None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path.rename(path.with_name(f"{self.FILE_PREFIX}-{stamp}.jsonl"))
        rotated = [p for p in self.log_files() if p.name != path.name]
        for old in rotated[: max(0, len(rotated) - self.backups)]:
            try:
                old.unlink()
            except OSError:
                pass

    def apply_retention(self) -> int:
        """Delete rotated log files older than ``retention_days``. Returns the number removed."""
        if not self.directory or self.retention_days <= 0:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for p in self.log_files():
            if p.name == f"{self.FILE_PREFIX}.jsonl":
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        return removed

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None

    # ----------------------------------------------------------------- logging
    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def log(self, level: Level, message: str, **fields: Any) -> LogEvent:
        extra = fields.pop("extra", None) or {}
        known = {k: fields.pop(k) for k in list(fields) if k in LogEvent.__dataclass_fields__}
        extra.update(fields)
        event = LogEvent(
            ts=datetime.now(timezone.utc).astimezone(),
            level=level,
            message=redact(message) or "",
            **known,
            extra={k: (redact(v) if isinstance(v, str) else v) for k, v in extra.items()},
        )
        if event.error:
            event.error = redact(event.error)
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            if level >= self.level:
                self._events.append(event)
                if self.directory is not None:
                    try:
                        self._open()
                        assert self._fh is not None
                        self._fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                        self._fh.flush()
                        self._rotate_if_needed()
                    except OSError as exc:
                        _std_logger.error("Failed to write log file: %s", exc)
            listeners = list(self._listeners) if level >= self.level else []
        _std_logger.log(int(level), event.format_text())
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                _std_logger.exception("Log listener failed")
        return event

    def debug(self, message: str, **f: Any) -> LogEvent:
        return self.log(Level.DEBUG, message, **f)

    def info(self, message: str, **f: Any) -> LogEvent:
        return self.log(Level.INFO, message, **f)

    def warning(self, message: str, **f: Any) -> LogEvent:
        return self.log(Level.WARNING, message, **f)

    def error(self, message: str, **f: Any) -> LogEvent:
        return self.log(Level.ERROR, message, **f)

    def events(self) -> list[LogEvent]:
        with self._lock:
            return list(self._events)

    @contextmanager
    def operation(self, op_type: str, message: str, *, device: str | None = None, **fields: Any) -> Iterator["Operation"]:
        op = Operation(self, op_type, device, new_op_id(op_type[:3]), fields)
        self.info(f"{message} started", device=device, op_id=op.op_id, op_type=op_type, **fields)
        start = time.monotonic()
        try:
            yield op
        except Exception as exc:
            op.duration = time.monotonic() - start
            self.error(
                f"{message} failed",
                device=device,
                op_id=op.op_id,
                op_type=op_type,
                duration=op.duration,
                result="failed",
                error=str(exc),
                **fields,
            )
            raise
        op.duration = time.monotonic() - start
        level = Level.WARNING if op.result not in (None, "success") else Level.INFO
        self.log(
            level,
            f"{message} {'completed' if op.result in (None, 'success') else op.result}",
            device=device,
            op_id=op.op_id,
            op_type=op_type,
            duration=op.duration,
            result=op.result or "success",
            **{**fields, **op.extra},
        )

    # ----------------------------------------------------------------- export
    @staticmethod
    def export_txt(path: str | os.PathLike[str], events: Iterable[LogEvent]) -> int:
        n = 0
        with open(path, "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(ev.format_text() + "\n")
                n += 1
        return n

    @staticmethod
    def export_jsonl(path: str | os.PathLike[str], events: Iterable[LogEvent]) -> int:
        n = 0
        with open(path, "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
                n += 1
        return n

    @staticmethod
    def read_jsonl(path: str | os.PathLike[str]) -> list[LogEvent]:
        out = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(LogEvent.from_dict(json.loads(line)))
                    except (ValueError, KeyError):
                        continue
        return out

    def load_history(self, max_age: timedelta = timedelta(days=1)) -> list[LogEvent]:
        """Load recent events from disk (used to seed the viewer at start-up)."""
        cutoff = datetime.now(timezone.utc) - max_age
        events: list[LogEvent] = []
        for path in self.log_files():
            try:
                events.extend(e for e in self.read_jsonl(path) if e.ts >= cutoff)
            except OSError:
                continue
        return events


@dataclass
class Operation:
    book: LogBook
    op_type: str
    device: str | None
    op_id: str
    fields: dict[str, Any]
    result: str | None = None
    duration: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def log(self, level: Level, message: str, **f: Any) -> LogEvent:
        return self.book.log(level, message, device=self.device, op_id=self.op_id, op_type=self.op_type, **f)

    def info(self, message: str, **f: Any) -> LogEvent:
        return self.log(Level.INFO, message, **f)

    def warning(self, message: str, **f: Any) -> LogEvent:
        return self.log(Level.WARNING, message, **f)

    def error(self, message: str, **f: Any) -> LogEvent:
        return self.log(Level.ERROR, message, **f)
