"""Transfer progress: parsing ADB client output and computing rates.

The ADB client prints progress differently across versions:

* modern clients (when stdout is a terminal): ``\\r[ 42%] /remote/path``
* old clients with ``-p``: ``Transferring: 1234/5678 (21%)``
* all clients when finished: ``SRC: 1 file pushed, 0 skipped. 35.1 MB/s (123 bytes in 0.004s)``

The parser splits on both ``\\r`` and ``\\n`` and tolerates chunks that cut
lines in half. It never invents values: percentages are only reported when
ADB printed them, byte counts only when ADB printed bytes or when they are
measured from the destination file size (see :class:`ProgressSource`).
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
PCT_RE = re.compile(r"^\[\s*(\d{1,3})%\]\s*(.*)$")
TRANSFERRING_RE = re.compile(r"Transferring:\s*(\d+)\s*/\s*(\d+)\s*\((\d{1,3})%\)")
SUMMARY_RE = re.compile(
    r"(\d+) files? (pushed|pulled)(?:,\s*(\d+) skipped)?\.?"
    r"(?:\s*([\d.]+) MB/s \((\d+) bytes in ([\d.]+)s\))?"
)
OLD_SUMMARY_RE = re.compile(r"([\d.]+) KB/s \((\d+) bytes in ([\d.]+)s\)")


class ProgressSource(str, Enum):
    ADB_PERCENT = "adb percentage"
    ADB_BYTES = "adb byte count"
    DESTINATION_SIZE = "measured destination size"
    COMPLETED = "completed"
    NONE = "indeterminate"


@dataclass
class ProgressUpdate:
    source: ProgressSource
    percent: float | None = None
    bytes_done: int | None = None
    total: int | None = None
    current: str | None = None


@dataclass
class TransferSummary:
    files: int
    skipped: int
    bytes: int | None
    seconds: float | None


class AdbProgressParser:
    def __init__(self) -> None:
        self._pending = ""
        self.summary: TransferSummary | None = None
        self.last: ProgressUpdate | None = None
        self.lines: list[str] = []

    def feed(self, text: str) -> list[ProgressUpdate]:
        data = self._pending + text
        parts = re.split(r"[\r\n]", data)
        self._pending = parts.pop()  # incomplete trailing fragment
        updates: list[ProgressUpdate] = []
        for part in parts:
            upd = self._parse_line(part)
            if upd:
                updates.append(upd)
        # A bare "[ 42%] path" fragment without a terminator is complete enough to report.
        frag = ANSI_RE.sub("", self._pending).strip()
        m = PCT_RE.match(frag)
        if m and frag.endswith(m.group(2)) and m.group(2):
            pct = min(100.0, float(m.group(1)))
            if self.last is None or self.last.percent != pct:
                upd = ProgressUpdate(ProgressSource.ADB_PERCENT, percent=pct, current=m.group(2).strip())
                updates.append(upd)
                self.last = upd
        return updates

    def finish(self) -> list[ProgressUpdate]:
        if not self._pending:
            return []
        data, self._pending = self._pending, ""
        upd = self._parse_line(data)
        return [upd] if upd else []

    def _parse_line(self, raw: str) -> ProgressUpdate | None:
        line = ANSI_RE.sub("", raw).strip()
        if not line:
            return None
        m = TRANSFERRING_RE.search(line)
        if m:
            done, total, pct = int(m.group(1)), int(m.group(2)), float(m.group(3))
            upd = ProgressUpdate(ProgressSource.ADB_BYTES, percent=min(100.0, pct), bytes_done=done, total=total)
            self.last = upd
            return upd
        m = PCT_RE.match(line)
        if m:
            pct = min(100.0, float(m.group(1)))
            upd = ProgressUpdate(ProgressSource.ADB_PERCENT, percent=pct, current=m.group(2).strip() or None)
            self.last = upd
            return upd
        self.lines.append(line)
        m = SUMMARY_RE.search(line)
        if m:
            nbytes = int(m.group(5)) if m.group(5) else None
            secs = float(m.group(6)) if m.group(6) else None
            self.summary = TransferSummary(int(m.group(1)), int(m.group(3) or 0), nbytes, secs)
            return ProgressUpdate(ProgressSource.COMPLETED, percent=100.0, bytes_done=nbytes, total=nbytes)
        m = OLD_SUMMARY_RE.search(line)
        if m:
            nbytes = int(m.group(2))
            self.summary = TransferSummary(1, 0, nbytes, float(m.group(3)))
            return ProgressUpdate(ProgressSource.COMPLETED, percent=100.0, bytes_done=nbytes, total=nbytes)
        return None


class RateEstimator:
    """Sliding-window throughput estimator based on real measurements only."""

    def __init__(self, window: float = 5.0) -> None:
        self.window = window
        self._samples: deque[tuple[float, int]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def add(self, total_bytes_done: int, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self._samples and total_bytes_done < self._samples[-1][1]:
            self._samples.clear()
        self._samples.append((now, total_bytes_done))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.window:
            self._samples.popleft()

    def rate(self) -> float | None:
        if len(self._samples) < 2:
            return None
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        if t1 - t0 < 0.5:
            return None
        return (b1 - b0) / (t1 - t0)

    def eta(self, remaining: int) -> float | None:
        r = self.rate()
        if r is None or r <= 0:
            return None
        return remaining / r


@dataclass
class ProgressSnapshot:
    """Everything the GUI needs to render progress for one running job."""

    job_id: str
    phase: str
    current_file: str = ""
    source: str = ""
    destination: str = ""
    file_bytes_done: int | None = None
    file_total: int | None = None
    file_percent: float | None = None
    file_source: ProgressSource = ProgressSource.NONE
    batch_bytes_done: int = 0
    batch_total: int | None = None
    batch_files_done: int = 0
    batch_files_total: int = 0
    speed: float | None = None
    eta: float | None = None
    elapsed: float = 0.0
    message: str = ""

    @property
    def file_indeterminate(self) -> bool:
        return self.file_percent is None

    @property
    def batch_percent(self) -> float | None:
        if self.batch_total:
            return min(100.0, 100.0 * self.batch_bytes_done / self.batch_total)
        if self.batch_total == 0 and self.batch_files_total:
            return 100.0 * self.batch_files_done / self.batch_files_total
        return None


def format_bytes(n: int | float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
