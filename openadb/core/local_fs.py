"""Local filesystem helpers (walking, hashing, safe copy/move/delete)."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat as statmod
from collections.abc import Callable
from dataclasses import dataclass

from openadb.core.errors import OperationCancelled, ProtectedPathError, UnsafePathError
from openadb.core.paths import check_local_mutable, unique_name, validate_component
from openadb.core.process import CancelToken


@dataclass
class LocalItem:
    path: str  # absolute
    rel: str  # POSIX-style relative path from the walk root's parent (includes root name)
    kind: str  # "file", "dir", "symlink", "other"
    size: int = 0


def walk_local(root: str, cancel: CancelToken | None = None) -> list[LocalItem]:
    """List ``root`` and everything under it without following symlinks."""
    root = os.path.abspath(root)
    base_name = os.path.basename(root.rstrip(os.sep)) or root
    items: list[LocalItem] = []

    def kind_of(st: os.stat_result) -> str:
        if statmod.S_ISLNK(st.st_mode):
            return "symlink"
        if statmod.S_ISDIR(st.st_mode):
            return "dir"
        if statmod.S_ISREG(st.st_mode):
            return "file"
        return "other"

    st = os.lstat(root)
    k = kind_of(st)
    items.append(LocalItem(root, base_name, k, st.st_size if k == "file" else 0))
    if k != "dir":
        return items
    stack = [(root, base_name)]
    while stack:
        if cancel is not None and cancel.cancelled:
            raise OperationCancelled("Scan cancelled")
        current, rel = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            items.append(LocalItem(current, rel, "unreadable", 0))
            del exc
            continue
        subdirs = []
        for entry in entries:
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            ek = kind_of(est)
            erel = f"{rel}/{entry.name}"
            items.append(LocalItem(entry.path, erel, ek, est.st_size if ek == "file" else 0))
            if ek == "dir":
                subdirs.append((entry.path, erel))
        stack.extend(reversed(subdirs))
    return items


def hash_file(path: str, algorithm: str = "sha256", cancel: CancelToken | None = None,
              on_progress: Callable[[int], None] | None = None) -> str:
    h = hashlib.new(algorithm)
    done = 0
    with open(path, "rb") as fh:
        while True:
            if cancel is not None and cancel.cancelled:
                raise OperationCancelled("Hashing cancelled")
            buf = fh.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
            done += len(buf)
            if on_progress:
                on_progress(done)
    return h.hexdigest()


def free_space(path: str) -> int | None:
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def local_unique_path(directory: str, name: str) -> str:
    validate_component(name)
    return os.path.join(directory, unique_name(name, exists=lambda n: os.path.lexists(os.path.join(directory, n))))


def make_dir(parent: str, name: str) -> str:
    """Create ``parent/name``; ``name`` may contain '/' to create nested folders."""
    parts = [p for p in name.replace("\\", "/").split("/") if p]
    if not parts:
        raise UnsafePathError("Folder name is empty")
    for p in parts:
        validate_component(p)
    target = os.path.join(parent, *parts)
    if os.path.lexists(target):
        raise FileExistsError(f"Already exists: {target}")
    os.makedirs(target)
    return target


def rename_local(path: str, new_name: str) -> str:
    check_local_mutable(path)
    validate_component(new_name)
    target = os.path.join(os.path.dirname(os.path.abspath(path)), new_name)
    if os.path.lexists(target) and os.path.normcase(target) != os.path.normcase(os.path.abspath(path)):
        raise FileExistsError(f"Already exists: {target}")
    os.rename(path, target)
    return target


def delete_local(path: str) -> None:
    """Permanently delete a file, symlink or directory tree (symlinks are never followed)."""
    absolute = check_local_mutable(path)
    st = os.lstat(absolute)
    if statmod.S_ISDIR(st.st_mode):
        shutil.rmtree(absolute)
    else:
        os.unlink(absolute)


def copy_local(src: str, dst: str) -> None:
    """Copy file or tree from ``src`` to exact ``dst`` (must not exist); symlinks copied as links."""
    if os.path.lexists(dst):
        raise FileExistsError(f"Already exists: {dst}")
    src_abs = os.path.abspath(src)
    if os.path.abspath(dst).startswith(src_abs.rstrip(os.sep) + os.sep):
        raise UnsafePathError("Cannot copy a folder into itself")
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True)
    elif os.path.islink(src):
        os.symlink(os.readlink(src), dst)
    else:
        shutil.copy2(src, dst)


def move_local(src: str, dst: str) -> str:
    """Move ``src`` to exact ``dst``. Uses rename on the same filesystem, else copy-then-delete.

    Returns ``"rename"`` or ``"copy+delete"``.
    """
    check_local_mutable(src)
    if os.path.lexists(dst):
        raise FileExistsError(f"Already exists: {dst}")
    src_abs = os.path.abspath(src)
    if os.path.abspath(dst).startswith(src_abs.rstrip(os.sep) + os.sep):
        raise UnsafePathError("Cannot move a folder into itself")
    try:
        os.rename(src, dst)
        return "rename"
    except OSError as exc:
        import errno

        if exc.errno != errno.EXDEV:
            raise
    copy_local(src, dst)
    delete_local(src)
    return "copy+delete"


__all__ = [
    "LocalItem",
    "walk_local",
    "hash_file",
    "free_space",
    "local_unique_path",
    "make_dir",
    "rename_local",
    "delete_local",
    "copy_local",
    "move_local",
    "ProtectedPathError",
]
