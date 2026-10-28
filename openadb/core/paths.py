"""Path handling: application directories, remote POSIX paths, quoting and protection rules."""

from __future__ import annotations

import os
import posixpath
import sys
from pathlib import Path

from openadb.core.errors import ProtectedPathError, UnsafePathError

APP_DIR_NAME = "openadb"

# --------------------------------------------------------------------------- app dirs


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def config_dir() -> Path:
    override = _env_path("OPENADB_CONFIG_DIR")
    if override:
        return override
    if sys.platform.startswith("win"):
        base = _env_path("APPDATA") or Path.home() / "AppData" / "Roaming"
        return base / "OpenADB"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenADB"
    return (_env_path("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_DIR_NAME


def data_dir() -> Path:
    override = _env_path("OPENADB_DATA_DIR")
    if override:
        return override
    if sys.platform.startswith("win"):
        base = _env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return base / "OpenADB"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenADB"
    return (_env_path("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_DIR_NAME


def log_dir() -> Path:
    return data_dir() / "logs"


# --------------------------------------------------------------------------- remote paths

#: Locations that must never be deleted, renamed or moved through the GUI.
DEFAULT_PROTECTED_REMOTE: frozenset[str] = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/data",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib64",
        "/media",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/sdcard",
        "/storage",
        "/sys",
        "/system",
        "/tmp",
        "/usr",
        "/var",
        "/vendor",
        "/userdata",
        "/userdata/roms",
        "/userdata/bios",
        "/userdata/saves",
        "/userdata/system",
        "/storage/emulated",
        "/storage/emulated/0",
        "/sdcard/Android",
        "/data/data",
        "/data/media",
        "/data/media/0",
    }
)


def sq(value: str) -> str:
    """Quote ``value`` for a POSIX shell using single quotes.

    The result is safe against word splitting, globbing, variable expansion
    and command substitution. NUL bytes cannot be represented and are refused.
    """
    if "\x00" in value:
        raise UnsafePathError("NUL bytes cannot be passed to a remote shell")
    return "'" + value.replace("'", "'\\''") + "'"


def normalize_remote(path: str) -> str:
    """Normalise an absolute remote POSIX path, rejecting anything ambiguous."""
    if not isinstance(path, str) or not path:
        raise UnsafePathError("Remote path is empty")
    if "\x00" in path:
        raise UnsafePathError("Remote path contains a NUL byte")
    if not path.startswith("/"):
        raise UnsafePathError(f"Remote path must be absolute: {path!r}")
    norm = posixpath.normpath(path)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    return norm


def validate_component(name: str) -> str:
    """Validate a single file name (no separators, not '.' or '..')."""
    if not name:
        raise UnsafePathError("File name is empty")
    if "/" in name:
        raise UnsafePathError(f"File name must not contain '/': {name!r}")
    if "\x00" in name:
        raise UnsafePathError("File name contains a NUL byte")
    if name in (".", ".."):
        raise UnsafePathError(f"Invalid file name: {name!r}")
    return name


def remote_join(base: str, *names: str) -> str:
    result = normalize_remote(base)
    for name in names:
        validate_component(name)
        result = result.rstrip("/") + "/" + name
    return result


def remote_join_relative(base: str, relative: str) -> str:
    """Join a relative POSIX path (``a/b/c``) onto ``base`` without allowing traversal."""
    parts = [p for p in relative.split("/") if p not in ("", ".")]
    return remote_join(base, *parts) if parts else normalize_remote(base)


def remote_parent(path: str) -> str:
    return posixpath.dirname(normalize_remote(path)) or "/"


def remote_basename(path: str) -> str:
    return posixpath.basename(normalize_remote(path))


def is_remote_within(path: str, ancestor: str) -> bool:
    path, ancestor = normalize_remote(path), normalize_remote(ancestor)
    if ancestor == "/":
        return True
    return path == ancestor or path.startswith(ancestor.rstrip("/") + "/")


def is_protected_remote(path: str, extra: tuple[str, ...] | list[str] = ()) -> bool:
    norm = normalize_remote(path)
    if norm in DEFAULT_PROTECTED_REMOTE:
        return True
    if norm.count("/") == 1:  # every top-level directory
        return True
    return any(norm == normalize_remote(p) for p in extra if p)


def check_remote_mutable(path: str, extra: tuple[str, ...] | list[str] = ()) -> str:
    """Raise :class:`ProtectedPathError` if ``path`` may not be deleted/renamed/moved."""
    norm = normalize_remote(path)
    if is_protected_remote(norm, extra):
        raise ProtectedPathError(f"Refusing to modify protected location: {norm}")
    return norm


# --------------------------------------------------------------------------- local paths


def is_protected_local(path: str | os.PathLike[str]) -> bool:
    """Roots, top-level system directories and the home directory itself are protected."""
    p = Path(os.path.abspath(path))
    if p == Path(p.anchor) or p.parent == Path(p.anchor):
        return True
    try:
        if p == Path.home().resolve() or p == Path.home():
            return True
    except OSError:
        pass
    return False


def check_local_mutable(path: str | os.PathLike[str]) -> str:
    absolute = os.path.abspath(path)
    if is_protected_local(absolute):
        raise ProtectedPathError(f"Refusing to modify protected location: {absolute}")
    return absolute


def safe_local_join(base: str | os.PathLike[str], relative_posix: str) -> str:
    """Join a remote-derived relative path onto a local directory, preventing traversal."""
    base_abs = os.path.abspath(base)
    parts = [p for p in relative_posix.split("/") if p not in ("", ".")]
    for part in parts:
        validate_component(part)
        if sys.platform.startswith("win") and any(c in part for c in '<>:"\\|?*'):
            raise UnsafePathError(f"File name not representable on Windows: {part!r}")
    joined = os.path.abspath(os.path.join(base_abs, *parts))
    if os.path.commonpath([joined, base_abs]) != base_abs:
        raise UnsafePathError(f"Path escapes destination directory: {relative_posix!r}")
    return joined


def split_name_ext(name: str) -> tuple[str, str]:
    """Split ``archive.tar.gz`` style names conservatively (only last extension)."""
    if name.startswith(".") and name.count(".") == 1:
        return name, ""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return name, ""
    return stem, "." + ext


def unique_name(name: str, taken: "set[str] | None" = None, exists=None) -> str:  # type: ignore[no-untyped-def]
    """Return ``name`` or ``name (N).ext`` not present in ``taken``/``exists``."""
    taken = taken or set()

    def used(candidate: str) -> bool:
        return candidate in taken or (exists is not None and exists(candidate))

    if not used(name):
        return name
    stem, ext = split_name_ext(name)
    for n in range(1, 10000):
        candidate = f"{stem} ({n}){ext}"
        if not used(candidate):
            return candidate
    raise UnsafePathError(f"Could not find a free name for {name!r}")
