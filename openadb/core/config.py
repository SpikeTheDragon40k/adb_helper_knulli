"""Persistent user preferences (JSON in the platform configuration directory)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from openadb.core.knulli import RomSystem, default_rom_systems
from openadb.core.paths import config_dir

CONFIG_VERSION = 1

OVERWRITE_POLICIES = ("ask", "skip", "overwrite", "rename", "backup")
THEMES = ("system", "light", "dark")


@dataclass
class DeviceProfile:
    serial: str
    alias: str = ""
    default_remote_dir: str = ""
    knulli_mode: str = "auto"  # auto | on | off
    bookmarks: list[str] = field(default_factory=list)
    rom_root: str = "/userdata/roms"
    bios_dir: str = "/userdata/bios"
    last_host: str = ""  # for TCP devices

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeviceProfile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Settings:
    adb_path: str = ""
    server_host: str = "127.0.0.1"
    server_port: int = 5037
    refresh_interval: float = 3.0
    auto_refresh: bool = True
    default_local_dir: str = ""
    default_remote_dir: str = "/userdata"
    concurrency: int = 1
    verify_mode: str = "size"
    high_integrity_roms: bool = True
    overwrite_policy: str = "ask"
    log_level: str = "INFO"
    log_retention_days: int = 30
    log_max_mb: int = 5
    theme: str = "system"
    language: str = "system"
    command_timeout: float = 60.0
    console_timeout: float = 30.0
    show_hidden: bool = True
    confirm_console_destructive: bool = True
    extra_protected_paths: list[str] = field(default_factory=list)
    recent_connections: list[str] = field(default_factory=list)
    device_profiles: dict[str, DeviceProfile] = field(default_factory=dict)
    rom_systems: list[RomSystem] = field(default_factory=default_rom_systems)
    window_geometry: str = ""
    window_state: str = ""

    # ------------------------------------------------------------------ helpers
    def profile(self, serial: str) -> DeviceProfile:
        if serial not in self.device_profiles:
            self.device_profiles[serial] = DeviceProfile(serial=serial)
        return self.device_profiles[serial]

    def remote_home(self, serial: str | None) -> str:
        if serial and serial in self.device_profiles and self.device_profiles[serial].default_remote_dir:
            return self.device_profiles[serial].default_remote_dir
        return self.default_remote_dir or "/"

    def local_home(self) -> str:
        d = os.path.expanduser(self.default_local_dir) if self.default_local_dir else ""
        return d if d and os.path.isdir(d) else str(Path.home())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["device_profiles"] = {k: asdict(v) for k, v in self.device_profiles.items()}
        d["rom_systems"] = [s.to_dict() for s in self.rom_systems]
        d["version"] = CONFIG_VERSION
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Settings":
        s = cls()
        known = {f.name for f in fields(cls)}
        for key, value in d.items():
            if key not in known or key in ("device_profiles", "rom_systems"):
                continue
            default = getattr(s, key)
            if isinstance(default, bool):
                value = bool(value)
            elif isinstance(default, int) and not isinstance(default, bool):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(default, float):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(default, list) and not isinstance(value, list):
                continue
            elif isinstance(default, str) and not isinstance(value, str):
                continue
            setattr(s, key, value)
        for serial, prof in (d.get("device_profiles") or {}).items():
            if isinstance(prof, dict):
                prof = dict(prof)
                prof["serial"] = serial
                s.device_profiles[serial] = DeviceProfile.from_dict(prof)
        systems = d.get("rom_systems")
        if isinstance(systems, list) and systems:
            parsed = []
            for item in systems:
                try:
                    parsed.append(RomSystem.from_dict(item))
                except (KeyError, TypeError):
                    continue
            if parsed:
                s.rom_systems = parsed
        s.sanitize()
        return s

    def sanitize(self) -> None:
        if self.overwrite_policy not in OVERWRITE_POLICIES:
            self.overwrite_policy = "ask"
        if self.theme not in THEMES:
            self.theme = "system"
        if self.verify_mode not in ("none", "size", "hash", "hash_or_download"):
            self.verify_mode = "size"
        self.concurrency = max(1, min(4, int(self.concurrency)))
        self.refresh_interval = max(1.0, min(600.0, float(self.refresh_interval)))
        self.server_port = max(1, min(65535, int(self.server_port)))
        if not self.default_remote_dir.startswith("/"):
            self.default_remote_dir = "/userdata"


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config_dir() / "settings.json")

    def load(self) -> Settings:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return Settings()
        except (OSError, ValueError):
            # Keep the broken file for inspection; start with defaults.
            try:
                self.path.rename(self.path.with_suffix(".broken.json"))
            except OSError:
                pass
            return Settings()
        return Settings.from_dict(data if isinstance(data, dict) else {})

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(settings.to_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)
