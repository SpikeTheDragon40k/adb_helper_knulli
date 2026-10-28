import os

import pytest

from openadb.core.capabilities import probe_capabilities
from openadb.core.errors import ProtectedPathError, RemoteOperationError
from openadb.core.remote_fs import RC_EXISTS, RemoteFS

from conftest import SERIAL

GBA = "/userdata/roms/gba"


def names(entries):
    return sorted(e.name for e in entries)


def test_browse_userdata_roms_gba(rfs):
    entries = rfs.list_dir(GBA)
    assert names(entries) == sorted([
        "Pokemon FireRed.gba", "Mario & Luigi (USA) [!].gba", "It's a Test's ROM.gba", "ポケモン ルビー.gba",
        ".hidden.gba", "gamelist.xml",
    ])
    by = {e.name: e for e in entries}
    assert by["Pokemon FireRed.gba"].size == 4096
    assert by["ポケモン ルビー.gba"].size == 333
    assert by["Pokemon FireRed.gba"].kind == "file"
    assert by["Pokemon FireRed.gba"].permissions.startswith("-rw")
    assert by["Pokemon FireRed.gba"].path == GBA + "/Pokemon FireRed.gba"


def test_browse_root_and_userdata(rfs):
    assert "userdata" in names(rfs.list_dir("/"))
    entries = {e.name: e for e in rfs.list_dir("/userdata")}
    assert entries["roms"].is_dir and entries["bios"].is_dir


@pytest.mark.parametrize("weird", ["new\nline.gba", "tab\there.gba", "  leading spaces", "trailing space ",
                                   "-rf", "$(id)", "`id`", "*", "a;b|c&d", "quote\"s", "back\\slash", "[brackets]",
                                   "ünïcødé ✓"])
def test_weird_names_listed_exactly(rfs, rg35xx, weird):
    (rg35xx.host(GBA) / weird).write_bytes(b"12345")
    by = {e.name: e for e in rfs.list_dir(GBA)}
    assert weird in by
    assert by[weird].size == 5


def test_invalid_utf8_name_roundtrip(rfs, rg35xx):
    raw = os.path.join(os.fsencode(rg35xx.host(GBA)), b"latin1-\xe9.gba")
    with open(raw, "wb") as fh:
        fh.write(b"x")
    by = {e.name: e for e in rfs.list_dir(GBA)}
    name = b"latin1-\xe9.gba".decode("utf-8", "surrogateescape")
    assert name in by
    assert rfs.stat(by[name].path).size == 1


def test_symlinks(rfs, rg35xx):
    os.symlink("../snes", rg35xx.host(GBA) / "to-snes")
    os.symlink("missing-target", rg35xx.host(GBA) / "dangling")
    by = {e.name: e for e in rfs.list_dir(GBA)}
    assert by["to-snes"].is_link and by["to-snes"].link_target == "../snes"
    assert by["to-snes"].link_target_kind == "dir" and by["to-snes"].navigable
    assert by["dangling"].link_target_kind == "broken"


def test_listing_errors(rfs):
    with pytest.raises(RemoteOperationError, match="No such directory"):
        rfs.list_dir("/userdata/nope")
    with pytest.raises(RemoteOperationError, match="Not a directory"):
        rfs.list_dir(GBA + "/gamelist.xml")


def test_stat_and_exists(rfs):
    e = rfs.stat(GBA + "/Mario & Luigi (USA) [!].gba")
    assert e.size == 2048 and e.kind == "file"
    assert rfs.stat(GBA + "/nope") is None
    assert rfs.exists(GBA)
    assert not rfs.exists(GBA + "/nope")
    assert rfs.stat("/").is_dir


def test_walk_recursive(rfs):
    paths = {e.path for e in rfs.walk("/userdata")}
    assert "/userdata/roms/gba/Pokemon FireRed.gba" in paths
    assert "/userdata/system/logs/es_log.txt" in paths


def test_mkdir_nested_and_conflict(rfs, rg35xx):
    assert rfs.mkdir(GBA + "/a/b/c").ok
    assert (rg35xx.host(GBA) / "a/b/c").is_dir()
    r = rfs.mkdir(GBA + "/gamelist.xml")
    assert not r.ok and r.returncode == RC_EXISTS
    r = rfs.mkdir(GBA + "/a", parents=False)
    assert not r.ok


def test_rename_never_overwrites(rfs, rg35xx):
    src = GBA + "/It's a Test's ROM.gba"
    r = rfs.rename(src, GBA + "/gamelist.xml")
    assert not r.ok and "exists" in r.message
    assert (rg35xx.host(GBA) / "gamelist.xml").read_text() == "<gameList/>\n"
    r = rfs.rename(src, GBA + "/renamed (1).gba")
    assert r.ok
    assert (rg35xx.host(GBA) / "renamed (1).gba").exists()


def test_rename_into_existing_dir_is_refused(rfs, rg35xx):
    # mv a b where b is a directory would move a INTO b; we must refuse instead
    r = rfs.rename(GBA + "/gamelist.xml", "/userdata/roms/snes")
    assert not r.ok
    assert (rg35xx.host(GBA) / "gamelist.xml").exists()


def test_move_and_copy(rfs, rg35xx):
    assert rfs.rename(GBA + "/Pokemon FireRed.gba", "/userdata/roms/snes/Pokemon FireRed.gba").ok
    assert (rg35xx.host("/userdata/roms/snes") / "Pokemon FireRed.gba").exists()
    assert rfs.copy("/userdata/roms/snes", "/userdata/roms/snes-copy").ok
    assert (rg35xx.host("/userdata/roms/snes-copy") / "Pokemon FireRed.gba").exists()
    assert not rfs.copy("/userdata/roms/snes", "/userdata/roms/snes/inner").ok


def test_delete_files_and_empty_dirs(rfs, rg35xx):
    rfs.mkdir(GBA + "/emptydir")
    res = rfs.delete([GBA + "/Pokemon FireRed.gba", GBA + "/emptydir", GBA + "/nope"], recursive=False)
    by = {r.path: r for r in res}
    assert by[GBA + "/Pokemon FireRed.gba"].ok
    assert by[GBA + "/emptydir"].ok
    assert not by[GBA + "/nope"].ok and "No such" in by[GBA + "/nope"].message
    assert not (rg35xx.host(GBA) / "Pokemon FireRed.gba").exists()


def test_non_recursive_delete_refuses_nonempty_dir(rfs, rg35xx):
    res = rfs.delete(["/userdata/system/logs"], recursive=False)
    assert not res[0].ok
    assert (rg35xx.host("/userdata/system/logs") / "es_log.txt").exists()


def test_recursive_delete_does_not_follow_symlinks(rfs, rg35xx, tmp_path):
    keep = rg35xx.host("/userdata/saves/gba")
    (keep / "save.sav").write_bytes(b"precious")
    victim = rg35xx.host(GBA) / "victim"
    victim.mkdir()
    (victim / "f").write_text("x")
    os.symlink("../../../saves/gba", victim / "link-to-saves")
    os.symlink("../../saves", rg35xx.host(GBA) / "toplink")
    res = rfs.delete([GBA + "/victim", GBA + "/toplink"], recursive=True)
    assert all(r.ok for r in res), res
    assert not victim.exists()
    assert (keep / "save.sav").read_bytes() == b"precious"


def test_delete_refuses_protected(rfs, rg35xx):
    for p in ("/", "/userdata", "/bin", "/etc", "/usr", "/userdata/roms", "/userdata/../userdata"):
        with pytest.raises(ProtectedPathError):
            rfs.delete([p], recursive=True)
    assert rg35xx.host("/userdata/roms/gba").exists()


def test_delete_batch_partial_failure(rfs, rg35xx):
    paths = [f"{GBA}/batch-{i}.bin" for i in range(30)]
    for p in paths[:20]:
        rg35xx.host(p).write_bytes(b"x")
    results = rfs.delete(paths, recursive=False)
    assert len(results) == 30
    assert sum(r.ok for r in results) == 20
    assert all(not r.ok for r in results if r.path in paths[20:])


def test_inspect_for_delete(rfs, rg35xx):
    rfs.mkdir(GBA + "/empty")
    info = {i.path: i for i in rfs.inspect_for_delete([GBA, GBA + "/empty", GBA + "/gamelist.xml", GBA + "/zz"])}
    assert info[GBA].kind == "dir" and info[GBA].non_empty and info[GBA].item_count >= 6
    assert info[GBA + "/empty"].kind == "dir" and not info[GBA + "/empty"].non_empty
    assert info[GBA + "/gamelist.xml"].kind == "file"
    assert info[GBA + "/zz"].kind == "missing"


def test_disk_space_and_hash(rfs):
    space = rfs.disk_space("/userdata")
    assert space is not None and space.total > 0 and space.available >= 0
    algo, digest = rfs.file_hash(GBA + "/gamelist.xml")
    import hashlib

    assert algo == "sha256" and digest == hashlib.sha256(b"<gameList/>\n").hexdigest()


def test_hex_transport_listing_handles_newlines(mock_env, adb):
    dev = mock_env.add_device("PTY2", pty_mode=True)
    (dev.host(GBA) / "new\nline.gba").write_bytes(b"abc")
    (dev.host(GBA) / "cr\r\nname.gba").write_bytes(b"abcd")
    caps = probe_capabilities(adb, "PTY2")
    fs = RemoteFS(adb, "PTY2", caps)
    by = {e.name: e for e in fs.list_dir(GBA)}
    assert by["new\nline.gba"].size == 3
    assert by["cr\r\nname.gba"].size == 4
    assert fs.mkdir(GBA + "/made here").ok
    res = fs.delete([GBA + "/made here"], recursive=False)
    assert res[0].ok


def test_minimal_listing_without_stat(mock_env, adb):
    dev = mock_env.add_device("MIN2", exclude_tools=("stat",))
    caps = probe_capabilities(adb, "MIN2")
    fs = RemoteFS(adb, "MIN2", caps)
    by = {e.name: e for e in fs.list_dir(GBA)}
    assert by["Pokemon FireRed.gba"].kind == "file" and by["Pokemon FireRed.gba"].size is None
    assert {e.name for e in fs.list_dir("/userdata/roms")} >= {"gba", "snes"}
    del dev


def test_bulk_mismatch_falls_back_to_perfile(rfs, monkeypatch):
    import openadb.core.remote_fs as mod

    calls = []
    real = mod.build_list_script

    def fake(path, *, recursive, mode):
        calls.append(mode)
        if mode == "bulk":
            return real(path, recursive=recursive, mode=mode).replace("%n'", "%n-garbage'")
        return real(path, recursive=recursive, mode=mode)

    monkeypatch.setattr(mod, "build_list_script", fake)
    entries = rfs.list_dir(GBA)
    assert calls == ["bulk", "perfile"]
    assert "Pokemon FireRed.gba" in names(entries)


def test_device_disconnect_raises(rfs, mock_env):
    from openadb.core.errors import DeviceUnavailableError

    mock_env.set_state(SERIAL, "offline")
    with pytest.raises((DeviceUnavailableError, RemoteOperationError)):
        rfs.list_dir(GBA)
