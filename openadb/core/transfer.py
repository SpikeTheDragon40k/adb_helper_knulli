"""Transfer queue and engine built on ``adb push``/``adb pull``.

Directory transfers are expanded into individual files so that every file gets
its own conflict handling, progress, verification and log entry. Cancellation
kills the running ADB client; the destination file of an interrupted transfer
is then checked and reported as a possible partial file (it is never deleted
automatically). Byte-level resumption is *not* implemented: retrying a job
transfers its files again from the start.
"""

from __future__ import annotations

import json
import os
import posixpath
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from openadb.core.adb import looks_like_device_gone
from openadb.core.errors import (
    DeviceUnavailableError,
    OpenADBError,
    OperationCancelled,
    UnsafePathError,
)
from openadb.core.local_fs import free_space, walk_local
from openadb.core.logbook import Level, LogBook
from openadb.core.paths import (
    remote_join_relative,
    safe_local_join,
    unique_name,
)
from openadb.core.process import CancelToken
from openadb.core.progress import (
    AdbProgressParser,
    ProgressSnapshot,
    ProgressSource,
    RateEstimator,
    format_bytes,
)
from openadb.core.remote_fs import KIND_DIR, KIND_FILE, KIND_LINK, RemoteFS
from openadb.core.verify import VerifyMode, VerifyResult, verify_pair


class JobStatus(str, Enum):
    PENDING = "Pending"
    PREPARING = "Preparing"
    TRANSFERRING = "Transferring"
    VERIFYING = "Verifying"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    INTERRUPTED = "Interrupted"


ACTIVE_STATES = {JobStatus.PREPARING, JobStatus.TRANSFERRING, JobStatus.VERIFYING}
FINISHED_STATES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}


class ItemStatus(str, Enum):
    PENDING = "pending"
    SKIPPED = "skipped"
    TRANSFERRING = "transferring"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"
    VERIFY_FAILED = "verify failed"


class Direction(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"


@dataclass
class TransferItem:
    rel: str
    source: str
    destination: str
    kind: str  # "file" or "dir"
    size: int = 0
    status: ItemStatus = ItemStatus.PENDING
    error: str = ""
    verify: VerifyResult | None = None
    conflict: str = ""  # kind of existing destination, if any
    action: str = ""  # conflict action taken
    backup_path: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel": self.rel, "source": self.source, "destination": self.destination, "kind": self.kind,
            "size": self.size, "status": self.status.value, "error": self.error, "action": self.action,
            "backup_path": self.backup_path, "duration": round(self.duration, 3),
            "verify": self.verify.to_dict() if self.verify else None,
        }


@dataclass
class TransferJob:
    serial: str
    direction: Direction
    sources: list[str]
    dest_dir: str
    overwrite: str = "ask"
    verify: VerifyMode = VerifyMode.NONE
    move: bool = False
    label: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: JobStatus = JobStatus.PENDING
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    partial_paths: list[str] = field(default_factory=list)
    items: list[TransferItem] = field(default_factory=list)
    files_total: int = 0
    files_done: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    verify_ok: int = 0
    verify_failed: int = 0
    verify_unknown: int = 0

    @property
    def title(self) -> str:
        if self.label:
            return self.label
        if self.direction == Direction.UPLOAD:
            names = [os.path.basename(s.rstrip("/\\")) or s for s in self.sources]
        else:
            names = [posixpath.basename(s.rstrip("/")) or s for s in self.sources]
        first = names[0] if names else "?"
        more = f" (+{len(names) - 1} more)" if len(names) > 1 else ""
        verb = "Move" if self.move else ("Upload" if self.direction == Direction.UPLOAD else "Download")
        return f"{verb} {first}{more}"

    @property
    def finished_state(self) -> bool:
        return self.status in FINISHED_STATES

    def summary_text(self) -> str:
        parts = [f"{self.files_done}/{self.files_total} files", format_bytes(self.bytes_done) + " / " + format_bytes(self.bytes_total)]
        if self.files_skipped:
            parts.append(f"{self.files_skipped} skipped")
        if self.files_failed:
            parts.append(f"{self.files_failed} failed")
        if self.verify != VerifyMode.NONE and (self.verify_ok or self.verify_failed or self.verify_unknown):
            parts.append(f"verified {self.verify_ok} ok, {self.verify_failed} failed, {self.verify_unknown} unverifiable")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "serial": self.serial, "direction": self.direction.value, "sources": self.sources,
            "dest_dir": self.dest_dir, "overwrite": self.overwrite, "verify": self.verify.value, "move": self.move,
            "label": self.label, "status": self.status.value, "created": self.created, "started": self.started,
            "finished": self.finished, "error": self.error, "warnings": self.warnings[-50:],
            "partial_paths": self.partial_paths, "files_total": self.files_total, "files_done": self.files_done,
            "files_failed": self.files_failed, "files_skipped": self.files_skipped, "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done, "verify_ok": self.verify_ok, "verify_failed": self.verify_failed,
            "verify_unknown": self.verify_unknown,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TransferJob":
        job = cls(
            serial=d["serial"], direction=Direction(d["direction"]), sources=list(d["sources"]), dest_dir=d["dest_dir"],
            overwrite=d.get("overwrite", "ask"), verify=VerifyMode(d.get("verify", "none")), move=bool(d.get("move")),
            label=d.get("label", ""), id=d.get("id") or uuid.uuid4().hex[:10],
        )
        job.status = JobStatus(d.get("status", "Pending"))
        for key in ("created", "started", "finished", "error", "warnings", "partial_paths", "files_total", "files_done",
                    "files_failed", "files_skipped", "bytes_total", "bytes_done", "verify_ok", "verify_failed", "verify_unknown"):
            if key in d and d[key] is not None:
                setattr(job, key, d[key])
        return job


class ConflictAction(str, Enum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    RENAME = "rename"
    BACKUP = "backup"
    CANCEL = "cancel"


@dataclass
class ConflictQuery:
    job: TransferJob
    item: TransferItem
    existing_kind: str
    existing_size: int | None = None


class TransferListener(Protocol):
    def job_changed(self, job: TransferJob) -> None: ...

    def progress(self, snapshot: ProgressSnapshot) -> None: ...

    def resolve_conflict(self, query: ConflictQuery) -> tuple[ConflictAction, bool]: ...


class _NullListener:
    def job_changed(self, job: TransferJob) -> None:
        pass

    def progress(self, snapshot: ProgressSnapshot) -> None:
        pass

    def resolve_conflict(self, query: ConflictQuery) -> tuple[ConflictAction, bool]:
        return ConflictAction.SKIP, False


# ============================================================================ runner


class JobRunner:
    PROGRESS_INTERVAL = 0.1
    POLL_INTERVAL = 1.0

    def __init__(self, job: TransferJob, rfs: RemoteFS, logbook: LogBook, listener: TransferListener,
                 cancel: CancelToken, use_pty: bool = True) -> None:
        self.job = job
        self.rfs = rfs
        self.adb = rfs.adb
        self.log = logbook
        self.listener = listener
        self.cancel = cancel
        self.use_pty = use_pty
        self.op_type = job.direction.value
        self.rate = RateEstimator()
        self._last_emit = 0.0
        self._start = time.monotonic()
        self._existing: dict[str, tuple[str, int | None]] = {}

    # ----------------------------------------------------------------- helpers
    def _logf(self, level: Level, msg: str, **kw: Any) -> None:
        self.log.log(level, msg, device=self.job.serial, op_id=self.job.id, op_type=self.op_type, **kw)

    def _changed(self) -> None:
        self.listener.job_changed(self.job)

    def _check_cancel(self) -> None:
        if self.cancel.cancelled:
            raise OperationCancelled("Transfer cancelled")

    def _emit(self, snap: ProgressSnapshot, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < self.PROGRESS_INTERVAL:
            return
        self._last_emit = now
        snap.elapsed = now - self._start
        self.listener.progress(snap)

    def _snapshot(self, phase: str, **kw: Any) -> ProgressSnapshot:
        return ProgressSnapshot(
            job_id=self.job.id, phase=phase, batch_bytes_done=self.job.bytes_done, batch_total=self.job.bytes_total,
            batch_files_done=self.job.files_done + self.job.files_failed + self.job.files_skipped,
            batch_files_total=self.job.files_total, **kw,
        )

    # ----------------------------------------------------------------- main
    def run(self) -> None:
        job = self.job
        job.status = JobStatus.PREPARING
        job.started = time.time()
        job.error = ""
        self._changed()
        verb = "Upload" if job.direction == Direction.UPLOAD else "Download"
        self._logf(Level.INFO, f"{verb} job started: {job.title}", source=", ".join(job.sources)[:2000],
                   destination=job.dest_dir, extra={"overwrite": job.overwrite, "verify": job.verify.value, "move": job.move})
        try:
            if job.direction == Direction.UPLOAD:
                self._prepare_upload()
            else:
                self._prepare_download()
            self._resolve_conflicts()
            self._check_free_space()
            job.status = JobStatus.TRANSFERRING
            self._changed()
            self._create_dirs()
            self._transfer_files()
            mode = job.verify
            if job.move and mode == VerifyMode.NONE:
                mode = VerifyMode.SIZE
            if mode != VerifyMode.NONE:
                job.status = JobStatus.VERIFYING
                self._changed()
                self._verify(mode)
            if job.move:
                self._delete_sources()
            failed = [i for i in job.items if i.status in (ItemStatus.FAILED, ItemStatus.PARTIAL, ItemStatus.VERIFY_FAILED)]
            if failed:
                job.status = JobStatus.FAILED
                job.error = f"{len(failed)} of {job.files_total} item(s) failed"
            else:
                job.status = JobStatus.COMPLETED
        except OperationCancelled:
            job.status = JobStatus.CANCELLED
            job.error = "Cancelled by user"
            for item in job.items:
                if item.status in (ItemStatus.PENDING, ItemStatus.TRANSFERRING):
                    item.status = ItemStatus.CANCELLED
            self._logf(Level.WARNING, f"{verb} cancelled: {job.title}", result="cancelled")
        except DeviceUnavailableError as exc:
            job.status = JobStatus.FAILED
            job.error = f"Device disconnected: {exc}"
            self._logf(Level.ERROR, f"{verb} aborted: device disconnected", error=str(exc), result="failed")
        except (OpenADBError, OSError) as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            self._logf(Level.ERROR, f"{verb} failed: {exc}", error=str(exc), result="failed")
        except Exception as exc:  # noqa: BLE001 - never kill the queue thread
            job.status = JobStatus.FAILED
            job.error = f"Internal error: {exc!r}"
            self._logf(Level.ERROR, f"{verb} failed with internal error", error=repr(exc), result="failed")
        finally:
            job.finished = time.time()
            duration = job.finished - (job.started or job.finished)
            level = Level.INFO if job.status == JobStatus.COMPLETED else Level.WARNING
            self._logf(level, f"{verb} job {job.status.value.lower()}: {job.title} — {job.summary_text()}",
                       duration=duration, result=job.status.value.lower(), error=job.error or None,
                       destination=job.dest_dir)
            for p in job.partial_paths:
                self._logf(Level.WARNING, "Possible partial file left at destination", destination=p)
            self._emit(self._snapshot(job.status.value, message=job.summary_text()), force=True)
            self._changed()

    # ----------------------------------------------------------------- preparation
    def _add_item(self, item: TransferItem) -> None:
        self.job.items.append(item)
        if item.kind == "file":
            self.job.files_total += 1
            self.job.bytes_total += item.size

    def _prepare_upload(self) -> None:
        job = self.job
        job.items.clear()
        job.files_total = job.bytes_total = 0
        for src in job.sources:
            self._check_cancel()
            if not os.path.lexists(src):
                raise OpenADBError(f"Source does not exist: {src}")
            for li in walk_local(src, self.cancel):
                dest = remote_join_relative(job.dest_dir, li.rel)
                if li.kind == "file":
                    self._add_item(TransferItem(li.rel, li.path, dest, "file", li.size))
                elif li.kind == "dir":
                    self._add_item(TransferItem(li.rel, li.path, dest, "dir"))
                else:
                    msg = f"Skipped {li.kind}: {li.path}" + (" (symbolic links are not followed)" if li.kind == "symlink" else "")
                    job.warnings.append(msg)
                    self._logf(Level.WARNING, msg, source=li.path)
            self._emit(self._snapshot("Scanning", message=f"Scanned {job.files_total} files ({format_bytes(job.bytes_total)})"))
        # Existing destination state, one listing per top-level target
        for top in {i.destination for i in job.items if "/" not in i.rel}:
            self._check_cancel()
            entry = self.rfs.stat(top, cancel=self.cancel)
            if entry is None:
                continue
            self._existing[top] = (entry.kind, entry.size)
            if entry.kind == KIND_DIR:
                for e in self.rfs.walk(top, cancel=self.cancel):
                    self._existing[e.path] = (e.kind, e.size)
        self._logf(Level.INFO, f"Prepared {job.files_total} file(s), {format_bytes(job.bytes_total)}",
                   extra={"conflicts": sum(1 for i in job.items if i.destination in self._existing)})

    def _prepare_download(self) -> None:
        job = self.job
        job.items.clear()
        job.files_total = job.bytes_total = 0
        for src in job.sources:
            self._check_cancel()
            entry = self.rfs.stat(src, cancel=self.cancel)
            if entry is None:
                raise OpenADBError(f"Remote source does not exist: {src}")
            base = posixpath.basename(src.rstrip("/")) or "root"
            entries = [(base, entry)]
            if entry.kind == KIND_DIR:
                prefix = src.rstrip("/") + "/"
                entries += [(base + "/" + e.path[len(prefix):], e) for e in self.rfs.walk(src, cancel=self.cancel)]
            for rel, e in entries:
                if e.unsafe_name:
                    item = TransferItem(rel, e.path, "", "file", e.size or 0, ItemStatus.FAILED,
                                        "name cannot be transferred safely over this device's shell")
                    self._add_item(item)
                    job.files_failed += 1
                    continue
                try:
                    dest = safe_local_join(job.dest_dir, rel)
                except UnsafePathError as exc:
                    item = TransferItem(rel, e.path, "", "file", e.size or 0, ItemStatus.FAILED, str(exc))
                    self._add_item(item)
                    job.files_failed += 1
                    continue
                if e.kind == KIND_FILE:
                    self._add_item(TransferItem(rel, e.path, dest, "file", e.size or 0))
                elif e.kind == KIND_DIR:
                    self._add_item(TransferItem(rel, e.path, dest, "dir"))
                else:
                    msg = f"Skipped {'symbolic link' if e.kind == KIND_LINK else e.type_label}: {e.path}"
                    job.warnings.append(msg)
                    self._logf(Level.WARNING, msg, source=e.path)
            self._emit(self._snapshot("Scanning", message=f"Scanned {job.files_total} files ({format_bytes(job.bytes_total)})"))
        for item in job.items:
            if item.destination and os.path.lexists(item.destination):
                if os.path.islink(item.destination):
                    kind = KIND_LINK
                elif os.path.isdir(item.destination):
                    kind = KIND_DIR
                else:
                    kind = KIND_FILE
                size = os.path.getsize(item.destination) if kind == KIND_FILE else None
                self._existing[item.destination] = (kind, size)

    def _check_free_space(self) -> None:
        job = self.job
        needed = sum(i.size for i in job.items if i.kind == "file" and i.status == ItemStatus.PENDING)
        if job.direction == Direction.UPLOAD:
            probe = job.dest_dir
            space = None
            while True:
                try:
                    space = self.rfs.disk_space(probe, cancel=self.cancel)
                except OpenADBError:
                    space = None
                if space is not None or probe == "/":
                    break
                probe = posixpath.dirname(probe) or "/"
            available = space.available if space else None
        else:
            available = free_space(job.dest_dir)
        if available is None:
            msg = "Free space could not be determined; continuing without the check"
            job.warnings.append(msg)
            self._logf(Level.WARNING, msg)
            return
        self._logf(Level.INFO, f"Free space at destination: {format_bytes(available)}; required: {format_bytes(needed)}")
        if needed > available:
            raise OpenADBError(f"Not enough free space: {format_bytes(needed)} required, {format_bytes(available)} available")

    def _resolve_conflicts(self) -> None:
        job = self.job
        policy = job.overwrite
        sticky: ConflictAction | None = None
        planned: dict[str, set[str]] = {}
        skip_prefixes: list[str] = []
        sep = "/" if job.direction == Direction.UPLOAD else os.sep

        for item in job.items:
            if item.status != ItemStatus.PENDING:
                continue
            if any(item.destination.startswith(p) for p in skip_prefixes):
                item.status = ItemStatus.SKIPPED
                item.action = "skip (parent skipped)"
                if item.kind == "file":
                    job.files_skipped += 1
                continue
            existing = self._existing.get(item.destination)
            if existing is None:
                continue
            kind, size = existing
            item.conflict = kind
            if item.kind == "dir":
                if kind == KIND_DIR:
                    continue  # merge into existing folder
                item.status = ItemStatus.FAILED
                item.error = f"destination exists and is not a folder ({kind})"
                skip_prefixes.append(item.destination.rstrip(sep) + sep)
                self._logf(Level.ERROR, "Type conflict at destination", destination=item.destination, error=item.error)
                continue
            if kind == KIND_DIR:
                item.status = ItemStatus.FAILED
                item.error = "destination exists and is a folder"
                job.files_failed += 1
                self._logf(Level.ERROR, "Type conflict at destination", destination=item.destination, error=item.error)
                continue
            if sticky is not None:
                action = sticky
            elif policy == "ask":
                action, apply_all = self.listener.resolve_conflict(ConflictQuery(job, item, kind, size))
                if apply_all:
                    sticky = action
            else:
                action = ConflictAction(policy)
            item.action = action.value
            if action == ConflictAction.CANCEL:
                raise OperationCancelled("Cancelled at conflict prompt")
            if action == ConflictAction.SKIP:
                item.status = ItemStatus.SKIPPED
                job.files_skipped += 1
                job.bytes_total -= item.size
                self._logf(Level.INFO, "Skipped existing file", destination=item.destination, result="skipped")
            elif action == ConflictAction.RENAME:
                parent, name = (posixpath.split(item.destination) if job.direction == Direction.UPLOAD
                                else os.path.split(item.destination))
                taken = planned.setdefault(parent, set())
                existing_names = {posixpath.basename(p) if job.direction == Direction.UPLOAD else os.path.basename(p)
                                  for p in self._existing if (posixpath.dirname(p) if job.direction == Direction.UPLOAD else os.path.dirname(p)) == parent}
                new_name = unique_name(name, taken | existing_names)
                taken.add(new_name)
                old = item.destination
                item.destination = (posixpath.join(parent, new_name) if job.direction == Direction.UPLOAD
                                    else os.path.join(parent, new_name))
                self._logf(Level.INFO, "Incoming file renamed to avoid overwrite", source=old, destination=item.destination)
            elif action in (ConflictAction.OVERWRITE, ConflictAction.BACKUP):
                self._logf(Level.WARNING, f"Existing file will be {'backed up and ' if action == ConflictAction.BACKUP else ''}replaced",
                           destination=item.destination)
        self._changed()

    def _create_dirs(self) -> None:
        job = self.job
        dirs = [i for i in job.items if i.kind == "dir" and i.status == ItemStatus.PENDING]
        if job.direction == Direction.UPLOAD:
            targets = [job.dest_dir] + [i.destination for i in dirs if self._existing.get(i.destination, ("",))[0] != KIND_DIR]
            for res in self.rfs.mkdirs(targets, cancel=self.cancel):
                if not res.ok:
                    self._logf(Level.ERROR, "Could not create remote folder", destination=res.path, error=res.message)
                    for i in dirs:
                        if i.destination == res.path:
                            i.status, i.error = ItemStatus.FAILED, res.message
            for i in dirs:
                if i.status == ItemStatus.PENDING:
                    i.status = ItemStatus.DONE
        else:
            os.makedirs(job.dest_dir, exist_ok=True)
            for i in dirs:
                try:
                    os.makedirs(i.destination, exist_ok=True)
                    i.status = ItemStatus.DONE
                except OSError as exc:
                    i.status, i.error = ItemStatus.FAILED, str(exc)
                    self._logf(Level.ERROR, "Could not create local folder", destination=i.destination, error=str(exc))

    # ----------------------------------------------------------------- transfer
    def _make_backup(self, item: TransferItem) -> None:
        suffix = ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        if self.job.direction == Direction.UPLOAD:
            parent, name = posixpath.split(item.destination)
            backup = posixpath.join(parent, f"{name}{suffix}")
            res = self.rfs.rename(item.destination, backup, cancel=self.cancel)
            if not res.ok:
                raise OpenADBError(f"Backup of {item.destination} failed: {res.message}")
        else:
            backup = item.destination + suffix
            if os.path.lexists(backup):
                raise OpenADBError(f"Backup target already exists: {backup}")
            os.rename(item.destination, backup)
        item.backup_path = backup
        self._logf(Level.INFO, "Backup created before overwrite", source=item.destination, destination=backup)

    def _transfer_files(self) -> None:
        job = self.job
        files = [i for i in job.items if i.kind == "file" and i.status == ItemStatus.PENDING]
        failed_parents = [i.destination.rstrip("/" if job.direction == Direction.UPLOAD else os.sep) +
                          ("/" if job.direction == Direction.UPLOAD else os.sep)
                          for i in job.items if i.kind == "dir" and i.status == ItemStatus.FAILED]
        for item in files:
            self._check_cancel()
            if any(item.destination.startswith(p) for p in failed_parents):
                item.status, item.error = ItemStatus.FAILED, "parent folder could not be created"
                job.files_failed += 1
                continue
            if item.action == ConflictAction.BACKUP.value:
                try:
                    self._make_backup(item)
                except (OpenADBError, OSError) as exc:
                    item.status, item.error = ItemStatus.FAILED, str(exc)
                    job.files_failed += 1
                    self._logf(Level.ERROR, "Backup failed; file not transferred", destination=item.destination, error=str(exc))
                    continue
            self._transfer_one(item)
            self._changed()

    def _transfer_one(self, item: TransferItem) -> None:
        job = self.job
        upload = job.direction == Direction.UPLOAD
        item.status = ItemStatus.TRANSFERRING
        name = posixpath.basename(item.destination) if upload else os.path.basename(item.destination)
        verb = "Upload" if upload else "Download"
        self._logf(Level.INFO, f"{verb} started: {name}", source=item.source, destination=item.destination,
                   extra={"size": item.size})
        parser = AdbProgressParser()
        state: dict[str, Any] = {"bytes": None, "pct": None, "src": ProgressSource.NONE, "last_adb": None}
        base_done = job.bytes_done
        t0 = time.monotonic()
        milestones = {25, 50, 75} if item.size >= 16 * 1024 * 1024 else set()

        def publish(force: bool = False) -> None:
            cur = state["bytes"]
            batch = base_done + (cur or 0)
            self.rate.add(batch)
            remaining = None if job.bytes_total is None else max(0, job.bytes_total - batch)
            snap = self._snapshot(
                job.status.value, current_file=name, source=item.source, destination=item.destination,
                file_bytes_done=cur, file_total=item.size, file_percent=state["pct"], file_source=state["src"],
                speed=self.rate.rate(), eta=self.rate.eta(remaining) if remaining is not None else None,
            )
            snap.batch_bytes_done = batch
            self._emit(snap, force)
            pct = state["pct"]
            if pct is not None:
                for m in sorted(milestones):
                    if pct >= m:
                        milestones.discard(m)
                        self._logf(Level.INFO, f"Transferred: {format_bytes(cur)} / {format_bytes(item.size)} ({m}%+)",
                                   source=item.source, extra={"progress_source": state["src"].value})

        def on_output(text: str) -> None:
            for upd in parser.feed(text):
                if upd.source == ProgressSource.COMPLETED:
                    continue
                state["last_adb"] = time.monotonic()
                if upd.bytes_done is not None:
                    state["bytes"], state["src"] = upd.bytes_done, ProgressSource.ADB_BYTES
                    state["pct"] = upd.percent
                elif upd.percent is not None:
                    state["pct"] = upd.percent
                    state["src"] = ProgressSource.ADB_PERCENT
                    # bytes derived from ADB's own percentage of a known file size
                    state["bytes"] = int(item.size * upd.percent / 100) if item.size else None
                publish()

        stop_poll = threading.Event()

        def poller() -> None:
            # Fallback when ADB prints no progress: measure the destination size.
            if stop_poll.wait(1.5):
                return
            while not stop_poll.is_set():
                if state["last_adb"] is None:
                    size = None
                    try:
                        if upload:
                            e = self.rfs.stat(item.destination)
                            size = e.size if e else None
                        elif os.path.exists(item.destination):
                            size = os.path.getsize(item.destination)
                    except (OpenADBError, OSError):
                        size = None
                    if size is not None and not stop_poll.is_set():
                        size = min(size, item.size) if item.size else size
                        state["bytes"], state["src"] = size, ProgressSource.DESTINATION_SIZE
                        state["pct"] = (100.0 * size / item.size) if item.size else None
                        publish()
                if stop_poll.wait(self.POLL_INTERVAL):
                    return

        publish(force=True)
        poll_thread = threading.Thread(target=poller, daemon=True)
        poll_thread.start()
        try:
            if upload:
                res = self.adb.push(job.serial, item.source, item.destination, on_output, self.cancel, self.use_pty)
            else:
                res = self.adb.pull(job.serial, item.source, item.destination, on_output, self.cancel, self.use_pty)
        finally:
            stop_poll.set()
            poll_thread.join(3)
        parser.finish()
        item.duration = time.monotonic() - t0
        output = (res.stderr_text + "\n" + "\n".join(parser.lines)).strip()
        if res.ok:
            item.status = ItemStatus.DONE
            job.files_done += 1
            job.bytes_done = base_done + item.size
            state["bytes"], state["pct"], state["src"] = item.size, 100.0, ProgressSource.COMPLETED
            publish(force=True)
            speed = item.size / item.duration if item.duration > 0 else None
            self._logf(Level.INFO, f"{verb} completed: {name}", source=item.source, destination=item.destination,
                       duration=item.duration, result="success",
                       extra={"bytes": item.size, "speed": f"{format_bytes(speed)}/s" if speed else None,
                              "adb_summary": parser.summary.__dict__ if parser.summary else None})
            return

        # Failure / cancellation
        cancelled = res.cancelled or self.cancel.cancelled
        partial = self._destination_exists(item.destination)  # None = unknown (device unreachable)
        if partial is None:
            job.warnings.append(f"Could not check for a partial file at {item.destination} (device unreachable)")
        if partial or partial is None:
            item.status = ItemStatus.PARTIAL
            job.partial_paths.append(item.destination)
        else:
            item.status = ItemStatus.CANCELLED if cancelled else ItemStatus.FAILED
        item.error = "cancelled" if cancelled else (output or f"adb exited with {res.returncode}")
        if not cancelled:
            job.files_failed += 1
        self._logf(Level.WARNING if cancelled else Level.ERROR,
                   f"{verb} {'cancelled' if cancelled else 'failed'}: {name}", source=item.source,
                   destination=item.destination, duration=item.duration,
                   result="cancelled" if cancelled else "failed", error=None if cancelled else item.error,
                   extra={"exit_code": res.returncode, "partial_destination": partial})
        if cancelled:
            raise OperationCancelled("Transfer cancelled")
        if looks_like_device_gone(output):
            raise DeviceUnavailableError(output)

    def _destination_exists(self, path: str) -> bool | None:
        if self.job.direction == Direction.DOWNLOAD:
            return os.path.lexists(path)
        try:
            return self.rfs.exists(path)
        except OpenADBError:
            return None

    # ----------------------------------------------------------------- verify / move
    def _verify(self, mode: VerifyMode) -> None:
        job = self.job
        done = [i for i in job.items if i.kind == "file" and i.status == ItemStatus.DONE]
        for n, item in enumerate(done, 1):
            self._check_cancel()
            local, remote = (item.source, item.destination) if job.direction == Direction.UPLOAD else (item.destination, item.source)
            name = os.path.basename(local)
            self._emit(self._snapshot("Verifying", current_file=name, source=item.source, destination=item.destination,
                                      message=f"Verifying {n}/{len(done)}"), force=True)
            t0 = time.monotonic()
            try:
                result = verify_pair(self.rfs, local, remote, mode, self.cancel)
            except OperationCancelled:
                raise
            except (OpenADBError, OSError) as exc:
                result = VerifyResult(None, mode.value, f"verification error: {exc}")
            item.verify = result
            if result.ok is True:
                job.verify_ok += 1
                level = Level.INFO
            elif result.ok is False:
                job.verify_failed += 1
                item.status = ItemStatus.VERIFY_FAILED
                item.error = f"verification failed: {result.detail}"
                level = Level.ERROR
            else:
                job.verify_unknown += 1
                level = Level.WARNING
            self._logf(level, f"Verification {'passed' if result.ok else 'FAILED' if result.ok is False else 'inconclusive'}: {name}",
                       source=item.source, destination=item.destination, duration=time.monotonic() - t0,
                       result={True: "verified", False: "mismatch", None: "unverified"}[result.ok],
                       extra={"method": result.method, "detail": result.detail, "local": result.local_value,
                              "remote": result.remote_value})

    def _delete_sources(self) -> None:
        """For move jobs: remove sources only for files transferred *and* verified."""
        job = self.job
        movable = [i for i in job.items if i.kind == "file" and i.status == ItemStatus.DONE and i.verify and i.verify.ok]
        not_moved = [i for i in job.items if i.kind == "file" and i not in movable]
        if not_moved:
            job.warnings.append(f"{len(not_moved)} source file(s) kept because they were not transferred and verified")
        dirs = sorted((i for i in job.items if i.kind == "dir"), key=lambda i: i.rel.count("/"), reverse=True)
        if job.direction == Direction.UPLOAD:
            from openadb.core.paths import check_local_mutable

            for item in movable:
                try:
                    check_local_mutable(item.source)
                    os.unlink(item.source)
                    self._logf(Level.INFO, "Source deleted after verified move", source=item.source, result="success")
                except OSError as exc:
                    self._logf(Level.ERROR, "Could not delete moved source", source=item.source, error=str(exc))
            for d in dirs:
                try:
                    check_local_mutable(d.source)
                    os.rmdir(d.source)
                except OSError:
                    pass
        else:
            if movable:
                for r in self.rfs.delete([i.source for i in movable], recursive=False, cancel=self.cancel):
                    level = Level.INFO if r.ok else Level.ERROR
                    self._logf(level, "Remote source deleted after verified move" if r.ok else "Could not delete moved remote source",
                               source=r.path, result="success" if r.ok else "failed", error=r.message or None,
                               extra={"exit_code": r.returncode})
            if dirs:
                try:
                    self.rfs.delete([d.source for d in dirs], recursive=False, cancel=self.cancel)
                except OpenADBError:
                    pass


# ============================================================================ queue


class TransferQueue:
    """Ordered job queue with a scheduler thread and configurable concurrency."""

    def __init__(
        self,
        rfs_provider: Callable[[str], RemoteFS],
        logbook: LogBook,
        listener: TransferListener | None = None,
        persist_path: Path | None = None,
        concurrency: int = 1,
        use_pty: bool = True,
    ) -> None:
        self.rfs_provider = rfs_provider
        self.log = logbook
        self.listener: TransferListener = listener or _NullListener()
        self.persist_path = persist_path
        self.concurrency = max(1, concurrency)
        self.use_pty = use_pty
        self.jobs: list[TransferJob] = []
        self._running: dict[str, tuple[threading.Thread, CancelToken]] = {}
        self._cv = threading.Condition()
        self._paused = False
        self._stopping = False
        self._load()
        self._thread = threading.Thread(target=self._loop, name="transfer-scheduler", daemon=True)
        self._thread.start()

    # ----------------------------------------------------------------- persistence
    def _load(self) -> None:
        if not self.persist_path or not self.persist_path.exists():
            return
        try:
            data = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for d in data.get("jobs", []):
            try:
                job = TransferJob.from_dict(d)
            except (KeyError, ValueError):
                continue
            if job.status in ACTIVE_STATES:
                job.status = JobStatus.INTERRUPTED
                job.error = "Application closed while the transfer was running"
                self.log.warning("Transfer was interrupted by application exit", device=job.serial, op_id=job.id,
                                 op_type=job.direction.value, extra={"title": job.title})
            self.jobs.append(job)
        self._paused = bool(data.get("paused", False))

    def _persist(self) -> None:
        if not self.persist_path:
            return
        with self._cv:
            data = {"version": 1, "paused": self._paused, "jobs": [j.to_dict() for j in self.jobs]}
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.persist_path)
        except OSError:
            pass

    # ----------------------------------------------------------------- API
    @property
    def paused(self) -> bool:
        return self._paused

    def get(self, job_id: str) -> TransferJob | None:
        with self._cv:
            return next((j for j in self.jobs if j.id == job_id), None)

    def add(self, job: TransferJob) -> TransferJob:
        with self._cv:
            self.jobs.append(job)
            self._cv.notify_all()
        self.log.info(f"Transfer queued: {job.title}", device=job.serial, op_id=job.id, op_type=job.direction.value,
                      source=", ".join(job.sources)[:2000], destination=job.dest_dir)
        self._persist()
        self.listener.job_changed(job)
        return job

    def cancel(self, job_id: str) -> None:
        with self._cv:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if job is None:
                return
            running = self._running.get(job_id)
            if running is None and job.status == JobStatus.PENDING:
                job.status = JobStatus.CANCELLED
                job.error = "Cancelled before start"
        if running is not None:
            self.log.warning("Transfer cancellation requested", device=job.serial, op_id=job.id, op_type=job.direction.value)
            running[1].cancel()
        else:
            self.log.info("Pending transfer cancelled", device=job.serial, op_id=job.id, op_type=job.direction.value)
            self._persist()
            self.listener.job_changed(job)

    def cancel_all(self) -> None:
        for job in list(self.jobs):
            if job.status == JobStatus.PENDING or job.id in self._running:
                self.cancel(job.id)

    def retry(self, job_id: str) -> bool:
        with self._cv:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if job is None or job.status not in (JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED):
                return False
            for attr in ("files_total", "files_done", "files_failed", "files_skipped", "bytes_total", "bytes_done",
                         "verify_ok", "verify_failed", "verify_unknown"):
                setattr(job, attr, 0)
            job.items, job.warnings, job.partial_paths = [], [], []
            job.status, job.error, job.started, job.finished = JobStatus.PENDING, "", None, None
            self._cv.notify_all()
        self.log.info("Transfer retry queued", device=job.serial, op_id=job.id, op_type=job.direction.value,
                      extra={"title": job.title})
        self._persist()
        self.listener.job_changed(job)
        return True

    def remove(self, job_id: str) -> bool:
        with self._cv:
            if job_id in self._running:
                return False
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j.id != job_id]
            removed = len(self.jobs) != before
        self._persist()
        return removed

    def clear_finished(self, include_failed: bool = False) -> int:
        states = {JobStatus.COMPLETED} | ({JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED} if include_failed else set())
        with self._cv:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j.status not in states]
            n = before - len(self.jobs)
        self._persist()
        return n

    def move(self, job_id: str, delta: int) -> bool:
        """Reorder a pending job among pending jobs."""
        with self._cv:
            pending = [j for j in self.jobs if j.status == JobStatus.PENDING]
            job = next((j for j in pending if j.id == job_id), None)
            if job is None:
                return False
            idx = pending.index(job)
            new_idx = max(0, min(len(pending) - 1, idx + delta))
            if new_idx == idx:
                return False
            other = pending[new_idx]
            self.jobs.remove(job)
            pos = self.jobs.index(other)
            self.jobs.insert(pos if delta < 0 else pos + 1, job)
        self._persist()
        return True

    def pause(self) -> None:
        with self._cv:
            self._paused = True
        self.log.info("Transfer queue paused (running transfers finish first)", op_type="queue")
        self._persist()

    def resume(self) -> None:
        with self._cv:
            self._paused = False
            self._cv.notify_all()
        self.log.info("Transfer queue resumed", op_type="queue")
        self._persist()

    def set_concurrency(self, n: int) -> None:
        with self._cv:
            self.concurrency = max(1, min(4, n))
            self._cv.notify_all()

    def running_ids(self) -> list[str]:
        with self._cv:
            return list(self._running)

    def has_active(self) -> bool:
        with self._cv:
            return bool(self._running) or any(j.status == JobStatus.PENDING for j in self.jobs)

    def overall(self) -> tuple[int, int, int]:
        """Return (finished, total, active) job counts."""
        with self._cv:
            total = len(self.jobs)
            finished = sum(1 for j in self.jobs if j.finished_state)
            return finished, total, len(self._running)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cv:
                busy = bool(self._running) or (not self._paused and any(j.status == JobStatus.PENDING for j in self.jobs))
            if not busy:
                return True
            time.sleep(0.05)
        return False

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._cv:
            self._stopping = True
            running = list(self._running.values())
            self._cv.notify_all()
            running_ids = set(self._running)
        for _thread, token in running:
            token.cancel()
        for thread, _token in running:
            thread.join(timeout)
        with self._cv:
            for job in self.jobs:
                if job.status in ACTIVE_STATES or job.id in running_ids:
                    job.status = JobStatus.INTERRUPTED
                    job.error = "Application closed while the transfer was running"
        self._persist()

    # ----------------------------------------------------------------- scheduler
    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._stopping and (
                    self._paused
                    or len(self._running) >= self.concurrency
                    or not any(j.status == JobStatus.PENDING for j in self.jobs)
                ):
                    self._cv.wait(0.5)
                if self._stopping:
                    return
                job = next(j for j in self.jobs if j.status == JobStatus.PENDING)
                job.status = JobStatus.PREPARING
                token = CancelToken()
                thread = threading.Thread(target=self._run_job, args=(job, token), name=f"transfer-{job.id}", daemon=True)
                self._running[job.id] = (thread, token)
            thread.start()

    def _run_job(self, job: TransferJob, token: CancelToken) -> None:
        try:
            try:
                rfs = self.rfs_provider(job.serial)
            except Exception as exc:  # noqa: BLE001
                job.status = JobStatus.FAILED
                job.error = f"Device unavailable: {exc}"
                job.finished = time.time()
                self.log.error("Transfer could not start: device unavailable", device=job.serial, op_id=job.id,
                               op_type=job.direction.value, error=str(exc))
                self.listener.job_changed(job)
                return
            JobRunner(job, rfs, self.log, self.listener, token, self.use_pty).run()
        finally:
            with self._cv:
                self._running.pop(job.id, None)
                self._cv.notify_all()
            self._persist()
