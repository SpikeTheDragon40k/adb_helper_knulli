"""Local diagnostic bundle (ZIP). Never uploaded anywhere by the application."""

from __future__ import annotations

import json
import platform
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from openadb import __version__
from openadb.core.logbook import LogBook, redact


def _redact_settings(d: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(d))
    # Host names of network devices are not secrets, but keep the bundle minimal.
    out.pop("window_geometry", None)
    out.pop("window_state", None)
    return out


def create_bundle(
    target: str | Path,
    logbook: LogBook,
    *,
    settings: dict[str, Any] | None = None,
    capabilities: dict[str, dict[str, Any]] | None = None,
    devices: list[dict[str, Any]] | None = None,
    adb_version: str = "",
    queue_file: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    target = Path(target)
    info = {
        "app_version": __version__,
        "created": datetime.now().astimezone().isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "adb_version": adb_version,
    }
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion

        info["pyside6"] = pyside_version
        info["qt"] = qVersion()
    except Exception:  # noqa: BLE001
        pass
    if extra:
        info.update(extra)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("system.json", json.dumps(info, indent=2))
        if settings is not None:
            zf.writestr("settings.json", json.dumps(_redact_settings(settings), indent=2))
        if capabilities:
            zf.writestr("capabilities.json", json.dumps(capabilities, indent=2, default=str))
        if devices is not None:
            zf.writestr("devices.json", json.dumps(devices, indent=2, default=str))
        if queue_file and queue_file.exists():
            zf.write(queue_file, "transfer_queue.json")
        for path in logbook.log_files():
            try:
                zf.writestr(f"logs/{path.name}", redact(path.read_text(encoding="utf-8")) or "")
            except OSError:
                continue
        lines = "\n".join(e.format_text() for e in logbook.events())
        zf.writestr("logs/session.txt", lines)
    return target
