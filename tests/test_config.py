from openadb.core.config import ConfigStore, Settings
from openadb.core.knulli import default_rom_systems, resolve_rom_destination, suggest_system


def test_roundtrip(tmp_path):
    store = ConfigStore(tmp_path / "s.json")
    s = Settings()
    s.adb_path = "/usr/bin/adb"
    s.profile("0123456789").alias = "RG35XX SP"
    s.profile("0123456789").default_remote_dir = "/userdata/roms"
    s.rom_systems[0].folder = "gba-custom"
    store.save(s)
    s2 = store.load()
    assert s2.adb_path == "/usr/bin/adb"
    assert s2.device_profiles["0123456789"].alias == "RG35XX SP"
    assert s2.remote_home("0123456789") == "/userdata/roms"
    assert s2.remote_home("other") == "/userdata"
    assert s2.rom_systems[0].folder == "gba-custom"


def test_defaults_and_sanitize(tmp_path):
    (tmp_path / "s.json").write_text('{"concurrency": 99, "theme": "neon", "overwrite_policy": "yolo", "server_port": "x"}')
    s = ConfigStore(tmp_path / "s.json").load()
    assert s.concurrency == 4 and s.theme == "system" and s.overwrite_policy == "ask" and s.server_port == 5037
    assert s.default_remote_dir == "/userdata"


def test_broken_config_falls_back(tmp_path):
    (tmp_path / "s.json").write_text("{not json")
    s = ConfigStore(tmp_path / "s.json").load()
    assert isinstance(s, Settings)
    assert (tmp_path / "s.broken.json").exists()


def test_rom_mappings():
    systems = default_rom_systems()
    by = {s.key: s for s in systems}
    assert resolve_rom_destination("/userdata/roms", by["gba"]) == "/userdata/roms/gba"
    assert resolve_rom_destination("/userdata/roms", by["snes"]) == "/userdata/roms/snes"
    assert resolve_rom_destination("/userdata/roms", by["nds"]) == "/userdata/roms/nds"
    assert resolve_rom_destination("/userdata/roms", by["psx"]) == "/userdata/roms/psx"
    assert suggest_system("Pokemon FireRed.gba", systems).key == "gba"
    assert suggest_system("game.iso", systems).key == "psp"
    assert suggest_system("readme.txt", systems) is None
