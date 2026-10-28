import os

import pytest

from openadb.core.errors import ProtectedPathError, UnsafePathError
from openadb.core.paths import (
    check_remote_mutable,
    is_protected_local,
    normalize_remote,
    remote_join,
    remote_join_relative,
    safe_local_join,
    sq,
    unique_name,
)


@pytest.mark.parametrize("value", ["plain", "with space", "it's", "a'b'c", "$(rm -rf /)", "`id`", "*", "new\nline",
                                   "ポケモン", "-rf", "a\\b", '"dq"'])
def test_sq_roundtrip_via_sh(value, tmp_path):
    import subprocess

    out = subprocess.run(["/bin/sh", "-c", "printf '%s' " + sq(value)], capture_output=True, cwd=tmp_path)
    assert out.stdout.decode() == value


def test_sq_rejects_nul():
    with pytest.raises(UnsafePathError):
        sq("a\x00b")


def test_normalize():
    assert normalize_remote("/userdata//roms/./gba/") == "/userdata/roms/gba"
    assert normalize_remote("//x") == "/x"
    assert normalize_remote("/a/b/../c") == "/a/c"
    with pytest.raises(UnsafePathError):
        normalize_remote("relative/path")
    with pytest.raises(UnsafePathError):
        normalize_remote("")


def test_join_validates_components():
    assert remote_join("/userdata", "roms", "gba") == "/userdata/roms/gba"
    for bad in ("..", ".", "a/b", ""):
        with pytest.raises(UnsafePathError):
            remote_join("/userdata", bad)
    assert remote_join_relative("/userdata", "roms/gba/") == "/userdata/roms/gba"
    with pytest.raises(UnsafePathError):
        remote_join_relative("/userdata", "../etc")


@pytest.mark.parametrize("path", ["/", "/userdata", "/bin", "/etc", "/usr", "/userdata/roms", "/userdata/bios",
                                  "/userdata/", "/usr/../", "/anything"])
def test_protected_remote(path):
    with pytest.raises(ProtectedPathError):
        check_remote_mutable(path)


def test_unprotected_remote():
    assert check_remote_mutable("/userdata/roms/gba/x.gba") == "/userdata/roms/gba/x.gba"
    assert check_remote_mutable("/userdata/roms/gba") == "/userdata/roms/gba"
    with pytest.raises(ProtectedPathError):
        check_remote_mutable("/userdata/roms/custom", extra=["/userdata/roms/custom"])


def test_safe_local_join(tmp_path):
    assert safe_local_join(tmp_path, "a/b.txt") == os.path.join(str(tmp_path), "a", "b.txt")
    with pytest.raises(UnsafePathError):
        safe_local_join(tmp_path, "../escape")
    with pytest.raises(UnsafePathError):
        safe_local_join(tmp_path, "a/../../escape")


def test_unique_name():
    assert unique_name("a.gba", {"a.gba"}) == "a (1).gba"
    assert unique_name("a.gba", {"a.gba", "a (1).gba"}) == "a (2).gba"
    assert unique_name("noext", {"noext"}) == "noext (1)"
    assert unique_name(".hidden", {".hidden"}) == ".hidden (1)"


def test_local_protection():
    assert is_protected_local("/")
    assert is_protected_local("/usr")
    assert is_protected_local(os.path.expanduser("~"))
