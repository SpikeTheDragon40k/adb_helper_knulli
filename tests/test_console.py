import pytest

from openadb.core.console import (
    ConsoleCommandError,
    build_console_script,
    destructive_reasons,
    parse_adb_command,
    split_console_output,
)

from conftest import SERIAL


@pytest.mark.parametrize("cmd", ["rm -rf /userdata/roms", "ls; rm x", "mv a b", "dd if=/dev/zero of=/dev/mmcblk0",
                                 "mkfs.ext4 /dev/sda1", "echo hi > /userdata/x", "reboot", "chmod 777 x", "cp a b"])
def test_destructive_detected(cmd):
    assert destructive_reasons(cmd)


@pytest.mark.parametrize("cmd", ["ls -lah /userdata", "df -h", "du -sh /userdata/roms", "uname -a", "pwd",
                                 "cat /proc/cpuinfo 2>/dev/null", "find /userdata -name '*.gba' >/dev/null",
                                 "grep -r form /etc"])
def test_safe_commands(cmd):
    assert destructive_reasons(cmd) == []


def test_parse_adb_command():
    c = parse_adb_command("adb devices -l")
    assert c.args == ["devices", "-l"] and not c.device_scoped
    c = parse_adb_command("shell 'ls /userdata'")
    assert c.args == ["shell", "ls /userdata"] and c.device_scoped
    assert parse_adb_command("reboot").warnings
    for bad in ("", "-s other shell ls", "install x.apk", "sideload x.zip", "$(rm -rf ~)"):
        with pytest.raises(ConsoleCommandError):
            parse_adb_command(bad)


def test_console_cwd_tracking(adb, rg35xx):
    res = adb.shell_raw(SERIAL, build_console_script("cd /userdata/roms && pwd && uname -a", None))
    out, cwd = split_console_output(res.stdout_text)
    assert cwd == "/userdata/roms"
    assert "/userdata/roms" in out and "KNULLI" in out
    res = adb.shell_raw(SERIAL, build_console_script("ls", cwd))
    out, cwd2 = split_console_output(res.stdout_text)
    assert "gba" in out.split() and cwd2 == "/userdata/roms"
    res = adb.shell_raw(SERIAL, build_console_script("false", cwd))
    assert res.returncode == 1


def test_getprop_missing_on_device(adb, rg35xx):
    res = adb.shell_raw(SERIAL, "getprop ro.product.model")
    assert res.returncode != 0
    assert "not found" in res.stderr_text
