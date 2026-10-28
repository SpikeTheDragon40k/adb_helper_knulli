"""Post-transfer integrity verification.

A successful ``adb push``/``adb pull`` exit status only says the client did
not report an error; it is not proof of integrity. These checks compare the
local and remote copies independently.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import Enum

from openadb.core.errors import OpenADBError
from openadb.core.local_fs import hash_file
from openadb.core.process import CancelToken
from openadb.core.remote_fs import RemoteFS


class VerifyMode(str, Enum):
    NONE = "none"
    SIZE = "size"
    HASH = "hash"  # remote hash tool, falls back to size if unavailable
    HASH_OR_DOWNLOAD = "hash_or_download"  # remote hash tool, else download and hash locally

    @property
    def label(self) -> str:
        return {
            "none": "No verification",
            "size": "Compare file sizes",
            "hash": "Compare hashes (SHA-256/MD5 on device; size if unavailable)",
            "hash_or_download": "Compare hashes (download & hash if the device has no hash tool)",
        }[self.value]


@dataclass
class VerifyResult:
    ok: bool | None  # None = could not verify
    method: str
    detail: str = ""
    local_value: str = ""
    remote_value: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "method": self.method, "detail": self.detail,
                "local": self.local_value, "remote": self.remote_value}


def verify_pair(
    rfs: RemoteFS,
    local_path: str,
    remote_path: str,
    mode: VerifyMode,
    cancel: CancelToken | None = None,
) -> VerifyResult:
    if mode == VerifyMode.NONE:
        return VerifyResult(None, "none", "verification disabled")
    try:
        local_size = os.path.getsize(local_path)
    except OSError as exc:
        return VerifyResult(False, "size", f"local file unreadable: {exc}")
    try:
        entry = rfs.stat(remote_path, cancel=cancel)
    except OpenADBError as exc:
        return VerifyResult(None, "size", f"remote stat failed: {exc}")
    if entry is None:
        return VerifyResult(False, "size", "remote file missing")
    if entry.size is None:
        size_result = None
    else:
        size_result = entry.size == local_size
        if not size_result:
            return VerifyResult(False, "size", "size mismatch", str(local_size), str(entry.size))
    if mode == VerifyMode.SIZE:
        if size_result is None:
            return VerifyResult(None, "size", "remote size unavailable (no stat on device)")
        return VerifyResult(True, "size", f"{local_size} bytes", str(local_size), str(entry.size))

    try:
        remote = rfs.file_hash(remote_path, cancel=cancel)
    except OpenADBError as exc:
        remote = None
        hash_error = str(exc)
    else:
        hash_error = ""
    if remote is not None:
        algo, remote_digest = remote
        local_digest = hash_file(local_path, algo, cancel=cancel)
        ok = local_digest == remote_digest
        return VerifyResult(ok, algo, "hashes match" if ok else "HASH MISMATCH", local_digest, remote_digest)

    if mode == VerifyMode.HASH_OR_DOWNLOAD:
        fd, tmp = tempfile.mkstemp(prefix="openadb-verify-")
        os.close(fd)
        try:
            res = rfs.adb.pull(rfs.serial, remote_path, tmp, cancel=cancel, use_pty=False)
            if not res.ok:
                return VerifyResult(None, "download+sha256", f"re-download failed: {res.stderr_text.strip()}")
            local_digest = hash_file(local_path, "sha256", cancel=cancel)
            remote_digest = hash_file(tmp, "sha256", cancel=cancel)
            ok = local_digest == remote_digest
            return VerifyResult(ok, "download+sha256", "hashes match" if ok else "HASH MISMATCH", local_digest, remote_digest)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    if size_result:
        detail = "no hash tool on device; size matches" + (f" ({hash_error})" if hash_error else "")
        return VerifyResult(True, "size (hash unavailable)", detail, str(local_size), str(entry.size))
    return VerifyResult(None, "none", "no hash tool and no size information on device")
