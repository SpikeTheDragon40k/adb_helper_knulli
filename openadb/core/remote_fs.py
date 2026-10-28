"""Remote filesystem access through ``adb shell`` using only POSIX tools.

Directory listings never parse ``ls -l``. The listing script emits every file
name verbatim, NUL-terminated, followed by machine-readable ``stat -c`` output
which is matched sequentially against the known names. When the bulk ``stat``
output cannot be matched unambiguously the script is re-run in per-file mode.
Without ``stat`` the listing degrades to file types obtained via ``test``.

All paths are passed to the remote shell single-quoted (see :func:`sq`), so
there is no word splitting or wildcard expansion of user-provided paths.
"""

from __future__ import annotations

import posixpath
import re
import stat as statmod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openadb.core.adb import AdbClient, ShellResult
from openadb.core.capabilities import DeviceCapabilities
from openadb.core.errors import (
    ParseError,
    RemoteOperationError,
    UnsafePathError,
)
from openadb.core.paths import check_remote_mutable, normalize_remote, remote_join, sq, validate_component
from openadb.core.process import CancelToken

STAT_FIELDS = "%f %s %Y %h %u %g %U %G"
GLOBS = '"$p"/* "$p"/.[!.]* "$p"/..?*'
#: Approximate maximum size of one batched script (old adbd builds limit commands to 4 KiB).
SCRIPT_BUDGET = 3000

# exit codes used by our scripts
RC_NOT_FOUND = 44
RC_NOT_DIR = 45
RC_EXISTS = 46
RC_IS_DIR = 47
RC_NOT_EMPTY = 48
RC_CD_FAILED = 41

KIND_DIR = "dir"
KIND_FILE = "file"
KIND_LINK = "symlink"
KIND_OTHER = "other"


@dataclass
class RemoteEntry:
    path: str
    name: str
    kind: str
    size: int | None = None
    mtime: float | None = None
    mode: int | None = None
    nlink: int | None = None
    uid: int | None = None
    gid: int | None = None
    owner: str = ""
    group: str = ""
    link_target: str | None = None
    link_target_kind: str | None = None  # "dir", "file", or "broken"
    unsafe_name: bool = False
    special: str = ""

    @property
    def is_dir(self) -> bool:
        return self.kind == KIND_DIR

    @property
    def is_link(self) -> bool:
        return self.kind == KIND_LINK

    @property
    def navigable(self) -> bool:
        return self.is_dir or (self.is_link and self.link_target_kind == "dir")

    @property
    def permissions(self) -> str:
        if self.mode is None:
            return ""
        return statmod.filemode(self.mode)

    @property
    def mtime_dt(self) -> datetime | None:
        return datetime.fromtimestamp(self.mtime) if self.mtime is not None else None

    @property
    def type_label(self) -> str:
        if self.is_link:
            return "Link → folder" if self.link_target_kind == "dir" else "Link"
        if self.is_dir:
            return "Folder"
        if self.kind == KIND_OTHER:
            return self.special or "Special"
        ext = posixpath.splitext(self.name)[1]
        return f"{ext[1:].upper()} file" if ext else "File"

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class OpResult:
    path: str
    ok: bool
    returncode: int | None
    message: str = ""
    duration: float = 0.0


@dataclass
class DeleteInspection:
    path: str
    kind: str  # dir, file, symlink, missing, other
    non_empty: bool = False
    item_count: int | None = None


@dataclass
class DiskSpace:
    total: int
    used: int
    available: int
    mount: str = ""


@dataclass
class WalkResult:
    root: RemoteEntry
    entries: list[RemoteEntry] = field(default_factory=list)  # excluding root, depth-first


def _kind_from_mode(mode: int) -> tuple[str, str]:
    if statmod.S_ISDIR(mode):
        return KIND_DIR, ""
    if statmod.S_ISREG(mode):
        return KIND_FILE, ""
    if statmod.S_ISLNK(mode):
        return KIND_LINK, ""
    if statmod.S_ISCHR(mode):
        return KIND_OTHER, "Character device"
    if statmod.S_ISBLK(mode):
        return KIND_OTHER, "Block device"
    if statmod.S_ISFIFO(mode):
        return KIND_OTHER, "FIFO"
    if statmod.S_ISSOCK(mode):
        return KIND_OTHER, "Socket"
    return KIND_OTHER, "Unknown"


def _dec(b: bytes) -> str:
    return b.decode("utf-8", "surrogateescape")


def _parse_stat_fields(fields: list[bytes]) -> dict[str, Any]:
    if len(fields) != 8:
        raise ParseError(f"Unexpected stat field count: {fields!r}")
    mode = int(fields[0], 16)
    kind, special = _kind_from_mode(mode)
    return {
        "mode": mode,
        "kind": kind,
        "special": special,
        "size": int(fields[1]),
        "mtime": float(fields[2]),
        "nlink": int(fields[3]),
        "uid": int(fields[4]),
        "gid": int(fields[5]),
        "owner": _dec(fields[6]),
        "group": _dec(fields[7]),
    }


def parse_bulk_stat(text: bytes, names: list[bytes]) -> list[list[bytes]] | None:
    """Match ``stat -c '<8 fields> %n'`` output sequentially against ``names``.

    Returns the field lists, or ``None`` when the output does not match exactly
    (for example, an entry disappeared between the glob and ``stat``).
    """
    pos = 0
    out: list[list[bytes]] = []
    for name in names:
        fields = []
        for _ in range(8):
            sp = text.find(b" ", pos)
            if sp < 0:
                return None
            fields.append(text[pos:sp])
            pos = sp + 1
        if text[pos:pos + len(name)] != name:
            return None
        pos += len(name)
        if text[pos:pos + 1] != b"\n":
            return None
        pos += 1
        out.append(fields)
    if text[pos:].strip():
        return None
    return out


def build_list_script(path: str, *, recursive: bool, mode: str) -> str:
    """Build the listing script. ``mode`` is ``bulk``, ``perfile`` or ``test``."""
    if mode == "bulk":
        stat_part = (
            "  printf 'S\\0'\n"
            f"  stat -c '{STAT_FIELDS} %n' -- {GLOBS} 2>/dev/null\n"
            "  printf '\\0'\n"
        )
    elif mode == "perfile":
        stat_part = (
            f"  for f in {GLOBS}; do\n"
            "    if [ -e \"$f\" ] || [ -L \"$f\" ]; then\n"
            "      printf 'P%s\\0' \"$f\"\n"
            f"      stat -c '{STAT_FIELDS}' -- \"$f\" 2>/dev/null\n"
            "      printf '\\0'\n"
            "    fi\n"
            "  done\n"
        )
    else:
        stat_part = (
            f"  for f in {GLOBS}; do\n"
            "    if [ -L \"$f\" ]; then t=L; elif [ -d \"$f\" ]; then t=D; elif [ -f \"$f\" ]; then t=F;"
            " elif [ -e \"$f\" ]; then t=O; else continue; fi\n"
            "    printf 'T%s\\0%s\\0' \"$f\" \"$t\"\n"
            "  done\n"
        )
    rec_part = ""
    if recursive:
        rec_part = (
            f"  for f in {GLOBS}; do\n"
            "    if [ -d \"$f\" ] && [ ! -L \"$f\" ]; then __lsd \"$f\"; fi\n"
            "  done\n"
        )
    return (
        "__lsd() {\n"
        "  p=${1%/}\n"
        "  printf 'D%s\\0' \"$1\"\n"
        f"  for f in {GLOBS}; do\n"
        "    if [ -e \"$f\" ] || [ -L \"$f\" ]; then printf 'N%s\\0' \"$f\"; fi\n"
        "  done\n"
        + stat_part
        + f"  for f in {GLOBS}; do\n"
        "    if [ -L \"$f\" ]; then\n"
        "      if [ -d \"$f\" ]; then t=dir; elif [ -e \"$f\" ]; then t=file; else t=broken; fi\n"
        "      printf 'L%s\\0%s\\0%s\\0' \"$f\" \"$t\" \"$(readlink -- \"$f\" 2>/dev/null)\"\n"
        "    fi\n"
        "  done\n"
        "  printf 'E\\0'\n"
        + rec_part
        + "}\n"
        f"P0={sq(path)}\n"
        f"if [ ! -e \"$P0\" ] && [ ! -L \"$P0\" ]; then echo 'No such file or directory' >&2; exit {RC_NOT_FOUND}; fi\n"
        f"if [ ! -d \"$P0\" ]; then echo 'Not a directory' >&2; exit {RC_NOT_DIR}; fi\n"
        f"cd -- \"$P0\" 2>/dev/null || {{ echo 'Permission denied' >&2; exit {RC_CD_FAILED}; }}\n"
        "__lsd \"$P0\"\n"
        "exit 0\n"
    )


class _ListingMismatch(Exception):
    pass


def parse_listing(data: bytes, *, limited: bool = False) -> dict[str, list[RemoteEntry]]:
    """Parse listing output into ``{directory: [entries]}``.

    Raises :class:`_ListingMismatch` when bulk stat output does not match.
    """
    tokens = data.split(b"\x00")
    result: dict[str, list[RemoteEntry]] = {}
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        if tok[:1] != b"D":
            raise ParseError(f"Unexpected token in listing: {tok[:40]!r}")
        directory = _dec(tok[1:])
        i += 1
        names: list[bytes] = []
        entries: dict[bytes, RemoteEntry] = {}
        order: list[bytes] = []
        while i < n and tokens[i][:1] == b"N":
            names.append(tokens[i][1:])
            i += 1

        def make(full: bytes, **kw: Any) -> RemoteEntry:
            name = full.rsplit(b"/", 1)[-1]
            unsafe = limited and (b"\n" in name or b"\r" in name)
            return RemoteEntry(path=_dec(full), name=_dec(name), unsafe_name=unsafe, **kw)

        if i < n and tokens[i] == b"S":
            stat_text = tokens[i + 1] if i + 1 < n else b""
            i += 2
            matched = parse_bulk_stat(stat_text, names)
            if matched is None:
                raise _ListingMismatch(directory)
            for full, fields in zip(names, matched):
                entries[full] = make(full, **_parse_stat_fields(fields))
                order.append(full)
        else:
            while i < n and tokens[i][:1] in (b"P", b"T"):
                tag, full = tokens[i][:1], tokens[i][1:]
                val = tokens[i + 1] if i + 1 < n else b""
                i += 2
                if tag == b"P":
                    fields = val.strip().split(b" ")
                    if len(fields) == 8:
                        entries[full] = make(full, **_parse_stat_fields(fields))
                    else:  # entry vanished or stat failed for it
                        entries[full] = make(full, kind=KIND_OTHER, special="Unreadable")
                else:
                    kind = {b"D": KIND_DIR, b"F": KIND_FILE, b"L": KIND_LINK}.get(val, KIND_OTHER)
                    entries[full] = make(full, kind=kind)
                order.append(full)
        while i < n and tokens[i][:1] == b"L":
            full = tokens[i][1:]
            target_kind = _dec(tokens[i + 1]) if i + 1 < n else "broken"
            target = tokens[i + 2] if i + 2 < n else b""
            i += 3
            entry = entries.get(full)
            if entry is not None:
                entry.kind = KIND_LINK
                entry.link_target = _dec(target)
                entry.link_target_kind = target_kind
        if i < n and tokens[i] == b"E":
            i += 1
        else:
            raise ParseError("Listing output truncated")
        # Keep only entries that were announced by the name loop (consistent snapshot).
        announced = set(names)
        result[directory] = [entries[k] for k in order if k in announced or not names]
    return result


class RemoteFS:
    """Filesystem operations on one device."""

    def __init__(
        self,
        adb: AdbClient,
        serial: str,
        caps: DeviceCapabilities | None = None,
        *,
        protected_extra: Iterable[str] = (),
        timeout: float = 60.0,
    ) -> None:
        self.adb = adb
        self.serial = serial
        self.caps = caps
        self.protected_extra = tuple(protected_extra)
        self.timeout = timeout

    # ----------------------------------------------------------------- helpers
    @property
    def transport(self) -> str:
        return self.caps.transport if self.caps else "raw"

    def _strategy(self) -> str:
        if self.caps is None or self.caps.listing_strategy == "stat":
            return "bulk"
        return "test"

    def script(self, script: str, *, timeout: float | None = None, cancel: CancelToken | None = None) -> ShellResult:
        return self.adb.run_script(
            self.serial, script, timeout=self.timeout if timeout is None else timeout, cancel=cancel, transport=self.transport
        )

    def _check_safe(self, path: str) -> str:
        norm = normalize_remote(path)
        if self.transport == "limited" and ("\n" in norm or "\r" in norm):
            raise UnsafePathError("This device's shell cannot safely handle names containing newlines")
        return norm

    @staticmethod
    def _error_text(res: ShellResult) -> str:
        text = res.stdout.decode("utf-8", "replace") + "\n" + res.stderr
        return "\n".join(line for line in text.strip().splitlines() if line.strip())[:2000]

    # ----------------------------------------------------------------- listing
    def _listing(self, path: str, recursive: bool, cancel: CancelToken | None, timeout: float | None) -> dict[str, list[RemoteEntry]]:
        path = self._check_safe(path)
        mode = self._strategy()
        for attempt in range(2):
            res = self.script(build_list_script(path, recursive=recursive, mode=mode), cancel=cancel, timeout=timeout)
            if res.returncode == RC_NOT_FOUND:
                raise RemoteOperationError(f"No such directory: {path}", res.returncode, self._error_text(res))
            if res.returncode == RC_NOT_DIR:
                raise RemoteOperationError(f"Not a directory: {path}", res.returncode, self._error_text(res))
            if res.returncode == RC_CD_FAILED:
                raise RemoteOperationError(f"Permission denied: {path}", res.returncode, self._error_text(res))
            if res.returncode != 0:
                raise RemoteOperationError(
                    f"Listing {path} failed (exit {res.returncode}): {self._error_text(res)}", res.returncode, self._error_text(res)
                )
            try:
                return parse_listing(res.stdout, limited=self.transport == "limited")
            except _ListingMismatch:
                if attempt == 0 and mode == "bulk":
                    mode = "perfile"
                    continue
                raise ParseError(f"Directory listing for {path} changed while reading; please refresh") from None
        raise ParseError("unreachable")

    def list_dir(self, path: str, cancel: CancelToken | None = None) -> list[RemoteEntry]:
        path = normalize_remote(path)
        listing = self._listing(path, False, cancel, None)
        return listing.get(path, next(iter(listing.values()), []))

    def walk(self, path: str, cancel: CancelToken | None = None, timeout: float | None = 3600) -> list[RemoteEntry]:
        """Recursively list ``path`` (not following symlinks). Entries in depth-first order."""
        path = normalize_remote(path)
        listing = self._listing(path, True, cancel, timeout)
        out: list[RemoteEntry] = []

        def visit(d: str) -> None:
            for e in listing.get(d, []):
                out.append(e)
                if e.is_dir and e.path in listing:
                    visit(e.path)

        visit(path)
        return out

    def stat(self, path: str, cancel: CancelToken | None = None) -> RemoteEntry | None:
        """Return metadata for ``path`` without following a final symlink, or ``None`` if missing."""
        path = self._check_safe(path)
        q = sq(path)
        if self._strategy() == "bulk":
            body = (
                f"  printf 'P%s\\0' {q}\n"
                f"  stat -c '{STAT_FIELDS}' -- {q} 2>/dev/null\n"
                "  printf '\\0'\n"
            )
        else:
            body = (
                f"  if [ -L {q} ]; then t=L; elif [ -d {q} ]; then t=D; elif [ -f {q} ]; then t=F; else t=O; fi\n"
                f"  printf 'T%s\\0%s\\0' {q} \"$t\"\n"
            )
        script = (
            f"if [ ! -e {q} ] && [ ! -L {q} ]; then exit {RC_NOT_FOUND}; fi\n"
            "printf 'D/\\0'\n"
            + body
            + f"if [ -L {q} ]; then\n"
            f"  if [ -d {q} ]; then t=dir; elif [ -e {q} ]; then t=file; else t=broken; fi\n"
            f"  printf 'L%s\\0%s\\0%s\\0' {q} \"$t\" \"$(readlink -- {q} 2>/dev/null)\"\n"
            "fi\n"
            "printf 'E\\0'\n"
        )
        res = self.script(script, cancel=cancel, timeout=30)
        if res.returncode == RC_NOT_FOUND:
            return None
        if res.returncode != 0:
            raise RemoteOperationError(f"stat {path} failed: {self._error_text(res)}", res.returncode, self._error_text(res))
        listing = parse_listing(res.stdout, limited=self.transport == "limited")
        entries = listing.get("/", [])
        if not entries:
            return None
        entry = entries[0]
        if path == "/":
            entry.name = "/"
        return entry

    def exists(self, path: str, cancel: CancelToken | None = None) -> bool:
        q = sq(self._check_safe(path))
        res = self.script(f"if [ -e {q} ] || [ -L {q} ]; then exit 0; else exit 1; fi", cancel=cancel, timeout=20)
        return res.returncode == 0

    def is_dir(self, path: str, cancel: CancelToken | None = None) -> bool:
        q = sq(self._check_safe(path))
        res = self.script(f"[ -d {q} ]", cancel=cancel, timeout=20)
        return res.returncode == 0

    def existing_paths(self, paths: list[str], cancel: CancelToken | None = None) -> dict[str, str]:
        """Return ``{path: kind}`` for those of ``paths`` that exist (kind: dir/file/symlink/other)."""
        found: dict[str, str] = {}
        for chunk in _chunks_by_size([self._check_safe(p) for p in paths], SCRIPT_BUDGET):
            lines = []
            for p in chunk:
                q = sq(p)
                lines.append(
                    f"if [ -L {q} ]; then printf 'X%s\\0L\\0' {q}; elif [ -d {q} ]; then printf 'X%s\\0D\\0' {q}; "
                    f"elif [ -e {q} ]; then printf 'X%s\\0F\\0' {q}; fi"
                )
            res = self.script("\n".join(lines) + "\nexit 0", cancel=cancel, timeout=60)
            toks = res.stdout.split(b"\x00")
            for j in range(0, len(toks) - 1, 2):
                if toks[j][:1] == b"X":
                    found[_dec(toks[j][1:])] = {b"L": KIND_LINK, b"D": KIND_DIR}.get(toks[j + 1], KIND_FILE)
        return found

    # ----------------------------------------------------------------- mutations
    def _simple(self, script: str, path: str, what: str, cancel: CancelToken | None = None, timeout: float | None = None) -> OpResult:
        res = self.script(script, cancel=cancel, timeout=timeout)
        msg = self._error_text(res) if res.returncode != 0 else ""
        if res.returncode == RC_EXISTS:
            msg = "Destination already exists"
        elif res.returncode == RC_NOT_FOUND:
            msg = "No such file or directory"
        elif res.returncode == RC_IS_DIR:
            msg = "Is a directory"
        elif res.returncode == RC_NOT_EMPTY:
            msg = "Directory is not empty"
        return OpResult(path, res.returncode == 0, res.returncode, msg or ("" if res.returncode == 0 else f"{what} failed"), res.duration)

    def mkdir(self, path: str, parents: bool = True, cancel: CancelToken | None = None) -> OpResult:
        path = self._check_safe(path)
        q = sq(path)
        if parents:
            script = f"if [ -e {q} ] && [ ! -d {q} ]; then exit {RC_EXISTS}; fi\nmkdir -p -- {q} 2>&1"
        else:
            script = f"if [ -e {q} ] || [ -L {q} ]; then exit {RC_EXISTS}; fi\nmkdir -- {q} 2>&1"
        return self._simple(script, path, "mkdir", cancel)

    def mkdirs(self, paths: list[str], cancel: CancelToken | None = None) -> list[OpResult]:
        """Create many directories (with parents) in batched remote invocations."""
        results: list[OpResult] = []
        for chunk in _chunks_by_size([self._check_safe(p) for p in paths], SCRIPT_BUDGET):
            body = "\n".join(
                f"e=$(mkdir -p -- {sq(p)} 2>&1); printf 'R%s\\0%s\\0%s\\0' {sq(p)} \"$?\" \"$e\"" for p in chunk
            )
            res = self.script(body + "\nexit 0", cancel=cancel, timeout=120)
            results.extend(_parse_batch_results(res.stdout, res.duration / max(1, len(chunk))))
        return results

    def rename(self, src: str, dst: str, cancel: CancelToken | None = None) -> OpResult:
        """Rename/move ``src`` to the exact path ``dst``. Never overwrites."""
        src = check_remote_mutable(self._check_safe(src), self.protected_extra)
        dst = self._check_safe(dst)
        if dst == src:
            return OpResult(src, True, 0, "unchanged")
        if dst.startswith(src.rstrip("/") + "/"):
            return OpResult(src, False, None, "Cannot move a folder into itself")
        qs, qd = sq(src), sq(dst)
        script = (
            f"if [ ! -e {qs} ] && [ ! -L {qs} ]; then exit {RC_NOT_FOUND}; fi\n"
            f"if [ -e {qd} ] || [ -L {qd} ]; then exit {RC_EXISTS}; fi\n"
            f"mv -- {qs} {qd} 2>&1"
        )
        return self._simple(script, src, "rename", cancel)

    def copy(self, src: str, dst: str, cancel: CancelToken | None = None) -> OpResult:
        """Copy ``src`` to the exact path ``dst`` on the device (recursive, preserving attributes)."""
        src = self._check_safe(src)
        dst = self._check_safe(dst)
        if dst == src or dst.startswith(src.rstrip("/") + "/"):
            return OpResult(src, False, None, "Cannot copy a folder into itself")
        qs, qd = sq(src), sq(dst)
        script = (
            f"if [ ! -e {qs} ] && [ ! -L {qs} ]; then exit {RC_NOT_FOUND}; fi\n"
            f"if [ -e {qd} ] || [ -L {qd} ]; then exit {RC_EXISTS}; fi\n"
            f"cp -a -- {qs} {qd} 2>&1 || cp -R -p -- {qs} {qd} 2>&1"
        )
        return self._simple(script, src, "copy", cancel, timeout=3600)

    def inspect_for_delete(self, paths: list[str], cancel: CancelToken | None = None, count: bool = True) -> list[DeleteInspection]:
        out: list[DeleteInspection] = []
        use_find = count and (self.caps is None or self.caps.has("find")) and (self.caps is None or self.caps.has("wc"))
        for chunk in _chunks_by_size([self._check_safe(p) for p in paths], SCRIPT_BUDGET):
            lines = []
            for p in chunk:
                q = sq(p)
                cnt = f"c=$(find {q} 2>/dev/null | wc -l)" if use_find else "c="
                lines.append(
                    f"n=0; c=; if [ -L {q} ]; then t=L; elif [ -d {q} ]; then t=D; p={q}; "
                    f"for f in {GLOBS}; do if [ -e \"$f\" ] || [ -L \"$f\" ]; then n=1; break; fi; done; "
                    f"if [ $n = 1 ]; then {cnt}; fi; "
                    f"elif [ -f {q} ]; then t=F; elif [ -e {q} ]; then t=O; else t=X; fi; "
                    f"printf 'I%s\\0%s\\0%s\\0%s\\0' {q} \"$t\" \"$n\" \"$c\""
                )
            res = self.script("\n".join(lines) + "\nexit 0", cancel=cancel, timeout=180)
            toks = res.stdout.split(b"\x00")
            for j in range(0, len(toks) - 3, 4):
                if toks[j][:1] != b"I":
                    continue
                kind = {b"L": KIND_LINK, b"D": KIND_DIR, b"F": KIND_FILE, b"O": KIND_OTHER, b"X": "missing"}.get(toks[j + 1], KIND_OTHER)
                cnt_text = toks[j + 3].strip()
                out.append(
                    DeleteInspection(
                        path=_dec(toks[j][1:]),
                        kind=kind,
                        non_empty=toks[j + 2] == b"1",
                        item_count=(max(0, int(cnt_text) - 1) if cnt_text.isdigit() else None),
                    )
                )
        return out

    def delete(
        self,
        paths: list[str],
        *,
        recursive: bool,
        cancel: CancelToken | None = None,
        on_result: Callable[[OpResult], None] | None = None,
    ) -> list[OpResult]:
        """Permanently delete ``paths``.

        Files and symlinks are unlinked (a symlink to a directory removes only
        the link). Directories are removed with ``rmdir`` unless ``recursive``
        is true, in which case ``rm -rf`` is used; ``rm -r`` never follows
        symbolic links. Protected locations raise before anything runs.
        """
        checked = [check_remote_mutable(self._check_safe(p), self.protected_extra) for p in paths]
        results: list[OpResult] = []
        for chunk in _chunks_by_size(checked, SCRIPT_BUDGET):
            if cancel is not None and cancel.cancelled:
                break
            lines = []
            for p in chunk:
                q = sq(p)
                if recursive:
                    action = f"e=$(rm -rf -- {q} 2>&1); r=$?; if [ $r = 0 ] && {{ [ -e {q} ] || [ -L {q} ]; }}; then r=1; e='still exists after rm'; fi"
                else:
                    action = (
                        f"if [ -d {q} ] && [ ! -L {q} ]; then e=$(rmdir -- {q} 2>&1); r=$?; "
                        f"else e=$(rm -f -- {q} 2>&1); r=$?; fi"
                    )
                lines.append(
                    f"if [ ! -e {q} ] && [ ! -L {q} ]; then r={RC_NOT_FOUND}; e='No such file or directory'; else {action}; fi; "
                    f"printf 'R%s\\0%s\\0%s\\0' {q} \"$r\" \"$e\""
                )
            res = self.script("\n".join(lines) + "\nexit 0", cancel=cancel, timeout=3600 if recursive else 300)
            chunk_results = _parse_batch_results(res.stdout, res.duration / max(1, len(chunk)))
            seen = {r.path for r in chunk_results}
            for p in chunk:
                if p not in seen:
                    chunk_results.append(OpResult(p, False, res.returncode, "No result reported by device"))
            for r in chunk_results:
                if not r.ok and r.returncode != RC_NOT_FOUND and "Directory not empty" in r.message:
                    r.message = "Directory is not empty (recursive deletion was not confirmed)"
                results.append(r)
                if on_result:
                    on_result(r)
        return results

    def delete_one(self, path: str, recursive: bool, cancel: CancelToken | None = None) -> OpResult:
        return self.delete([path], recursive=recursive, cancel=cancel)[0]

    def backup_name(self, path: str, suffix: str) -> str:
        parent, name = posixpath.split(normalize_remote(path))
        return remote_join(parent or "/", validate_component(f"{name}{suffix}"))

    # ----------------------------------------------------------------- info
    def disk_space(self, path: str, cancel: CancelToken | None = None) -> DiskSpace | None:
        q = sq(self._check_safe(path))
        res = self.script(f"df -kP {q} 2>/dev/null || df -k {q} 2>/dev/null", cancel=cancel, timeout=20)
        if res.returncode != 0:
            return None
        return parse_df(res.stdout.decode("utf-8", "replace"))

    def file_hash(self, path: str, cancel: CancelToken | None = None) -> tuple[str, str] | None:
        """Return ``(algorithm, hexdigest)`` computed on the device, or ``None`` if unsupported."""
        if self.caps is None or self.caps.hash_command is None:
            return None
        cmd, algo = self.caps.hash_command
        res = self.script(f"{cmd} -- {sq(self._check_safe(path))}", cancel=cancel, timeout=3600)
        if res.returncode != 0:
            raise RemoteOperationError(f"{cmd} failed: {self._error_text(res)}", res.returncode, self._error_text(res))
        m = re.match(rb"\s*\\?([0-9a-fA-F]{32,128})\b", res.stdout)
        if not m:
            raise ParseError(f"Unexpected {cmd} output")
        return algo, m.group(1).decode().lower()

    def dir_usage(self, path: str, cancel: CancelToken | None = None) -> int | None:
        res = self.script(f"du -sk -- {sq(self._check_safe(path))} 2>/dev/null", cancel=cancel, timeout=600)
        m = re.match(rb"\s*(\d+)", res.stdout)
        return int(m.group(1)) * 1024 if m else None


def parse_df(text: str) -> DiskSpace | None:
    lines = [ln for ln in text.replace("\r", "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    tokens = " ".join(lines[1:]).split()
    for idx, tok in enumerate(tokens):
        if tok.endswith("%") and tok[:-1].isdigit() and idx >= 3:
            try:
                total, used, avail = (int(tokens[idx - 3]), int(tokens[idx - 2]), int(tokens[idx - 1]))
            except ValueError:
                continue
            mount = " ".join(tokens[idx + 1:])
            return DiskSpace(total * 1024, used * 1024, avail * 1024, mount)
    return None


def _parse_batch_results(data: bytes, per_item_duration: float) -> list[OpResult]:
    toks = data.split(b"\x00")
    out = []
    for j in range(0, len(toks) - 2, 3):
        if toks[j][:1] != b"R":
            continue
        rc_text = toks[j + 1].strip()
        rc = int(rc_text) if rc_text.lstrip(b"-").isdigit() else None
        msg = toks[j + 2].decode("utf-8", "replace").strip()
        if rc == RC_NOT_FOUND and not msg:
            msg = "No such file or directory"
        out.append(OpResult(_dec(toks[j][1:]), rc == 0, rc, msg, per_item_duration))
    return out


def _chunks_by_size(paths: list[str], limit: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for p in paths:
        cost = len(p.encode("utf-8", "surrogateescape")) * 5 + 250
        if cur and size + cost > limit:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += cost
    if cur:
        chunks.append(cur)
    return chunks
