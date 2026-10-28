import json
import os
import time

from openadb.core.diagnostics import create_bundle
from openadb.core.logbook import Level, LogBook, LogEvent, redact


def test_event_fields_and_files(tmp_path):
    book = LogBook(tmp_path / "logs")
    ev = book.info("Upload completed: a.gba", device="0123456789", op_id="up-1", op_type="upload",
                   source="/tmp/a.gba", destination="/userdata/roms/gba/a.gba", duration=3.02, result="success",
                   bytes=16)
    assert ev.ts.tzinfo is not None
    assert ev.extra == {"bytes": 16}
    line = ev.format_text()
    assert "[INFO]" in line and "[0123456789]" in line and "upload#up-1" in line and "duration=3.020s" in line
    book.close()
    data = [json.loads(x) for x in (tmp_path / "logs" / "activity.jsonl").read_text().splitlines()]
    assert data[0]["device"] == "0123456789" and data[0]["level"] == "INFO" and data[0]["duration"] == 3.02
    assert "+" in data[0]["ts"] or "-" in data[0]["ts"][19:]  # timezone offset present


def test_level_filter_and_listeners(tmp_path):
    book = LogBook(tmp_path / "l", level=Level.WARNING)
    seen = []
    book.subscribe(seen.append)
    book.info("hidden")
    book.error("shown", error="boom")
    assert [e.message for e in seen] == ["shown"]
    assert [e.message for e in book.events()] == ["shown"]
    book.close()


def test_export_txt_jsonl_roundtrip(tmp_path, logbook):
    logbook.info("one", device="d1", op_type="list")
    logbook.warning("two ü", device="d2", op_type="delete", extra={"exit_code": 1})
    n = LogBook.export_txt(tmp_path / "out.txt", logbook.events())
    assert n == 2 and "two ü" in (tmp_path / "out.txt").read_text(encoding="utf-8")
    LogBook.export_jsonl(tmp_path / "out.jsonl", logbook.events())
    back = LogBook.read_jsonl(tmp_path / "out.jsonl")
    assert [e.message for e in back] == ["one", "two ü"]
    assert back[1].level is Level.WARNING and back[1].extra["exit_code"] == 1


def test_rotation_and_retention(tmp_path):
    book = LogBook(tmp_path / "r", max_bytes=2000, backups=3, retention_days=1)
    for i in range(200):
        book.info(f"message {i} " + "x" * 50)
    book.close()
    files = book.log_files()
    assert 2 <= len(files) <= 4
    old = files[0]
    if old.name != "activity.jsonl":
        past = time.time() - 3 * 86400
        os.utime(old, (past, past))
        assert book.apply_retention() >= 1
        assert not old.exists()


def test_redaction():
    assert redact("password=hunter2 ok") == "password=*** ok"
    assert redact("token: abc123") == "token: ***"
    assert redact("http://user:secret@host/x") == "http://user:***@host/x"
    assert redact("adb pair 192.168.1.5:37000 123456") == "adb pair 192.168.1.5:37000 ******"


def test_operation_context(logbook):
    with logbook.operation("rename", "Rename x", device="s1") as op:
        op.extra["exit_code"] = 0
    evs = logbook.events()
    assert evs[-1].message == "Rename x completed" and evs[-1].duration is not None and evs[-1].op_id == op.op_id


def test_diagnostic_bundle(tmp_path, logbook):
    import zipfile

    logbook.info("hello")
    out = create_bundle(tmp_path / "b.zip", logbook, settings={"adb_path": "/usr/bin/adb"}, capabilities={"x": {}},
                        devices=[], adb_version="1.0.41")
    names = zipfile.ZipFile(out).namelist()
    assert "system.json" in names and "settings.json" in names and "logs/session.txt" in names
    assert any(n.startswith("logs/activity") for n in names)


def test_from_dict_tolerates_unknown():
    ev = LogEvent.from_dict({"ts": "2026-01-01T10:00:00.000+01:00", "level": "WARN", "message": "m"})
    assert ev.level is Level.WARNING
