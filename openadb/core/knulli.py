"""Knulli/Batocera handheld profile: shortcuts and editable ROM system mappings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

KNULLI_PROBE_DIRS = (
    "/userdata",
    "/userdata/roms",
    "/userdata/bios",
    "/userdata/saves",
    "/userdata/system",
    "/userdata/system/logs",
    "/userdata/screenshots",
)

KNULLI_SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("User data", "/userdata"),
    ("ROMs", "/userdata/roms"),
    ("BIOS", "/userdata/bios"),
    ("Saves", "/userdata/saves"),
    ("System", "/userdata/system"),
    ("Logs", "/userdata/system/logs"),
    ("Screenshots", "/userdata/screenshots"),
)


@dataclass
class RomSystem:
    key: str
    name: str
    folder: str  # relative to the ROM root
    extensions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"key": self.key, "name": self.name, "folder": self.folder, "extensions": list(self.extensions)}

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "RomSystem":
        exts = d.get("extensions") or []
        return cls(
            key=str(d["key"]),
            name=str(d.get("name", d["key"])),
            folder=str(d.get("folder", d["key"])),
            extensions=[str(e).lower() for e in exts] if isinstance(exts, list) else [],
        )


# Defaults follow Batocera/Knulli folder naming. They are suggestions only and can be edited in Settings.
DEFAULT_ROM_SYSTEMS: tuple[RomSystem, ...] = (
    RomSystem("gba", "Game Boy Advance", "gba", [".gba"]),
    RomSystem("gb", "Game Boy", "gb", [".gb"]),
    RomSystem("gbc", "Game Boy Color", "gbc", [".gbc"]),
    RomSystem("nes", "NES / Famicom", "nes", [".nes", ".fds", ".unf", ".unif"]),
    RomSystem("snes", "Super Nintendo", "snes", [".sfc", ".smc"]),
    RomSystem("n64", "Nintendo 64", "n64", [".n64", ".z64", ".v64"]),
    RomSystem("nds", "Nintendo DS", "nds", [".nds"]),
    RomSystem("virtualboy", "Virtual Boy", "virtualboy", [".vb"]),
    RomSystem("psx", "PlayStation", "psx", [".pbp", ".chd", ".cue", ".m3u", ".ecm"]),
    RomSystem("psp", "PlayStation Portable", "psp", [".cso", ".iso"]),
    RomSystem("megadrive", "Mega Drive / Genesis", "megadrive", [".md", ".gen", ".smd"]),
    RomSystem("mastersystem", "Master System", "mastersystem", [".sms"]),
    RomSystem("gamegear", "Game Gear", "gamegear", [".gg"]),
    RomSystem("segacd", "Sega CD", "segacd", []),
    RomSystem("sega32x", "Sega 32X", "sega32x", [".32x"]),
    RomSystem("dreamcast", "Dreamcast", "dreamcast", [".cdi", ".gdi"]),
    RomSystem("pcengine", "PC Engine / TurboGrafx-16", "pcengine", [".pce"]),
    RomSystem("ngp", "Neo Geo Pocket", "ngp", [".ngp"]),
    RomSystem("ngpc", "Neo Geo Pocket Color", "ngpc", [".ngc"]),
    RomSystem("neogeo", "Neo Geo", "neogeo", []),
    RomSystem("mame", "MAME", "mame", []),
    RomSystem("fbneo", "FinalBurn Neo", "fbneo", []),
    RomSystem("atari2600", "Atari 2600", "atari2600", [".a26"]),
    RomSystem("atari7800", "Atari 7800", "atari7800", [".a78"]),
    RomSystem("lynx", "Atari Lynx", "lynx", [".lnx"]),
    RomSystem("wonderswan", "WonderSwan", "wonderswan", [".ws"]),
    RomSystem("wonderswancolor", "WonderSwan Color", "wonderswancolor", [".wsc"]),
    RomSystem("pico8", "PICO-8", "pico8", [".p8"]),
    RomSystem("ports", "Ports", "ports", [".sh"]),
)


def default_rom_systems() -> list[RomSystem]:
    return [RomSystem(s.key, s.name, s.folder, list(s.extensions)) for s in DEFAULT_ROM_SYSTEMS]


def suggest_system(filename: str, systems: list[RomSystem]) -> RomSystem | None:
    """Suggest a system from a file extension, only when the extension is unambiguous."""
    ext = os.path.splitext(filename)[1].lower()
    if not ext:
        return None
    matches = [s for s in systems if ext in s.extensions]
    return matches[0] if len(matches) == 1 else None


def resolve_rom_destination(rom_root: str, system: RomSystem) -> str:
    from openadb.core.paths import remote_join_relative

    return remote_join_relative(rom_root, system.folder)
