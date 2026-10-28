"""User-level remote file operations with structured logging.

These wrap :class:`RemoteFS` mutations so that every action produces log
entries with device, operation id, paths, exit status, duration and errors.
The GUI only calls into this module (from worker threads).
"""

from __future__ import annotations

import posixpath
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from openadb.core.errors import OpenADBError, ProtectedPathError
from openadb.core.logbook import Level, LogBook, new_op_id
from openadb.core.paths import (
    check_remote_mutable,
    normalize_remote,
    remote_join,
    remote_join_relative,
    unique_name,
    validate_component,
)
from openadb.core.process import CancelToken
from openadb.core.remote_fs import OpResult, RemoteFS


@dataclass
class BatchOutcome:
    op_id: str
    results: list[OpResult] = field(default_factory=list)
    cancelled: bool = False
    duration: float = 0.0

    @property
    def succeeded(self) -> list[OpResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[OpResult]:
        return [r for r in self.results if not r.ok]

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and not self.failed and not self.cancelled


def protected_among(paths: list[str], extra: tuple[str, ...] = ()) -> list[str]:
    bad = []
    for p in paths:
        try:
            check_remote_mutable(p, extra)
        except ProtectedPathError:
            bad.append(normalize_remote(p))
    return bad


def delete_remote(
    rfs: RemoteFS,
    paths: list[str],
    *,
    recursive: bool,
    confirmed: bool,
    logbook: LogBook,
    cancel: CancelToken | None = None,
    on_progress: Callable[[int, int, OpResult], None] | None = None,
) -> BatchOutcome:
    """Permanently delete remote paths. ``confirmed`` must be true (explicit user confirmation)."""
    op_id = new_op_id("del")
    outcome = BatchOutcome(op_id)
    serial = rfs.serial
    if not confirmed:
        logbook.error("Deletion refused: not confirmed by the user", device=serial, op_id=op_id, op_type="delete",
                      extra={"paths": paths, "confirmed": False})
        raise OpenADBError("Deletion requires explicit confirmation")
    protected = protected_among(paths, rfs.protected_extra)
    if protected:
        logbook.error("Deletion refused: protected location", device=serial, op_id=op_id, op_type="delete",
                      extra={"protected": protected, "confirmed": True})
        raise ProtectedPathError("Refusing to delete protected location(s): " + ", ".join(protected))
    logbook.info(f"Delete started: {len(paths)} item(s){' (recursive)' if recursive else ''}", device=serial,
                 op_id=op_id, op_type="delete", extra={"confirmed": True, "recursive": recursive, "count": len(paths)})
    start = time.monotonic()
    total = len(paths)

    def on_result(r: OpResult) -> None:
        outcome.results.append(r)
        logbook.log(
            Level.INFO if r.ok else Level.ERROR,
            f"Deleted {r.path}" if r.ok else f"Delete failed: {r.path}",
            device=serial, op_id=op_id, op_type="delete", source=r.path, duration=r.duration,
            result="success" if r.ok else "failed", error=r.message or None,
            extra={"exit_code": r.returncode, "confirmed": True, "recursive": recursive},
        )
        if on_progress:
            on_progress(len(outcome.results), total, r)

    try:
        rfs.delete(paths, recursive=recursive, cancel=cancel, on_result=on_result)
    except OpenADBError as exc:
        if cancel is not None and cancel.cancelled:
            outcome.cancelled = True
        else:
            logbook.error("Delete aborted", device=serial, op_id=op_id, op_type="delete", error=str(exc))
            done = {r.path for r in outcome.results}
            for p in paths:
                if normalize_remote(p) not in done:
                    outcome.results.append(OpResult(normalize_remote(p), False, None, str(exc)))
    if cancel is not None and cancel.cancelled and len(outcome.results) < total:
        outcome.cancelled = True
    outcome.duration = time.monotonic() - start
    n_ok, n_fail = len(outcome.succeeded), len(outcome.failed)
    level = Level.INFO if outcome.all_ok else Level.WARNING
    status = "completed" if outcome.all_ok else ("cancelled" if outcome.cancelled else "completed with failures")
    logbook.log(level, f"Delete {status}: {n_ok} deleted, {n_fail} failed, {total - n_ok - n_fail} not attempted",
                device=serial, op_id=op_id, op_type="delete", duration=outcome.duration,
                result="success" if outcome.all_ok else ("cancelled" if outcome.cancelled else "partial failure"),
                extra={"confirmed": True, "recursive": recursive})
    return outcome


def mkdir_remote(rfs: RemoteFS, parent: str, name: str, logbook: LogBook) -> OpResult:
    """Create ``parent/name``; ``name`` may contain '/' for nested folders."""
    parts = [p for p in name.split("/") if p]
    for p in parts:
        validate_component(p)
    target = remote_join_relative(parent, "/".join(parts))
    res = rfs.mkdir(target, parents=True)
    logbook.log(Level.INFO if res.ok else Level.ERROR, f"Create folder {'succeeded' if res.ok else 'failed'}: {target}",
                device=rfs.serial, op_id=new_op_id("mkd"), op_type="mkdir", destination=target, duration=res.duration,
                result="success" if res.ok else "failed", error=res.message or None, extra={"exit_code": res.returncode})
    return res


def rename_remote(rfs: RemoteFS, path: str, new_name: str, logbook: LogBook) -> OpResult:
    validate_component(new_name)
    src = normalize_remote(path)
    dst = remote_join(posixpath.dirname(src) or "/", new_name)
    try:
        res = rfs.rename(src, dst)
    except ProtectedPathError as exc:
        res = OpResult(src, False, None, str(exc))
    logbook.log(Level.INFO if res.ok else Level.ERROR, f"Rename {'succeeded' if res.ok else 'failed'}: {posixpath.basename(src)} → {new_name}",
                device=rfs.serial, op_id=new_op_id("ren"), op_type="rename", source=src, destination=dst,
                duration=res.duration, result="success" if res.ok else "failed", error=res.message or None,
                extra={"exit_code": res.returncode})
    return res


def paste_remote(
    rfs: RemoteFS,
    sources: list[str],
    dest_dir: str,
    *,
    move: bool,
    logbook: LogBook,
    on_conflict: Callable[[str, str], str] | None = None,
    cancel: CancelToken | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> BatchOutcome:
    """Copy or move remote items into ``dest_dir`` on the same device.

    ``on_conflict(src, dst)`` returns ``"skip"`` or ``"rename"`` when the target
    exists; the default is ``"rename"`` for copies into the same folder and
    ``"skip"`` otherwise. Existing targets are never overwritten.
    """
    op_type = "move" if move else "copy"
    op_id = new_op_id(op_type[:3])
    outcome = BatchOutcome(op_id)
    dest_dir = normalize_remote(dest_dir)
    start = time.monotonic()
    logbook.info(f"Remote {op_type} started: {len(sources)} item(s) → {dest_dir}", device=rfs.serial, op_id=op_id,
                 op_type=op_type, destination=dest_dir)
    existing = {e.name for e in rfs.list_dir(dest_dir, cancel=cancel)}
    for n, src in enumerate(sources, 1):
        if cancel is not None and cancel.cancelled:
            outcome.cancelled = True
            break
        src = normalize_remote(src)
        name = posixpath.basename(src)
        if on_progress:
            on_progress(n, len(sources), name)
        if move and posixpath.dirname(src) == dest_dir:
            outcome.results.append(OpResult(src, True, 0, "already in destination"))
            continue
        target_name = name
        if name in existing:
            if posixpath.dirname(src) == dest_dir and not move:
                decision = "rename"
            else:
                decision = on_conflict(src, remote_join(dest_dir, name)) if on_conflict else "skip"
            if decision == "skip":
                res = OpResult(src, False, None, "skipped: destination exists")
                outcome.results.append(res)
                logbook.info(f"Skipped existing item: {name}", device=rfs.serial, op_id=op_id, op_type=op_type,
                             source=src, destination=remote_join(dest_dir, name), result="skipped")
                continue
            target_name = unique_name(name, existing)
        dst = remote_join(dest_dir, target_name)
        try:
            res = rfs.rename(src, dst, cancel=cancel) if move else rfs.copy(src, dst, cancel=cancel)
        except (ProtectedPathError, OpenADBError) as exc:
            res = OpResult(src, False, None, str(exc))
        if res.ok:
            existing.add(target_name)
        outcome.results.append(res)
        logbook.log(Level.INFO if res.ok else Level.ERROR,
                    f"{'Moved' if move else 'Copied'} {name}" if res.ok else f"{op_type.title()} failed: {name}",
                    device=rfs.serial, op_id=op_id, op_type=op_type, source=src, destination=dst,
                    duration=res.duration, result="success" if res.ok else "failed", error=res.message or None,
                    extra={"exit_code": res.returncode})
    outcome.duration = time.monotonic() - start
    real_fail = [r for r in outcome.failed if not r.message.startswith("skipped")]
    logbook.log(Level.INFO if not real_fail else Level.WARNING,
                f"Remote {op_type} finished: {len(outcome.succeeded)} ok, {len(real_fail)} failed, "
                f"{len(outcome.failed) - len(real_fail)} skipped",
                device=rfs.serial, op_id=op_id, op_type=op_type, duration=outcome.duration,
                result="success" if not real_fail else "partial failure")
    return outcome
