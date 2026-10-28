import hashlib
import os
import threading
import time

import pytest

from openadb.core.process import PTY_AVAILABLE
from openadb.core.progress import ProgressSource
from openadb.core.remote_fs import RemoteFS
from openadb.core.transfer import (
    ConflictAction,
    Direction,
    ItemStatus,
    JobStatus,
    TransferJob,
    TransferQueue,
)
from openadb.core.verify import VerifyMode

from conftest import SERIAL

GBA = "/userdata/roms/gba"


class Recorder:
    def __init__(self, answer=(ConflictAction.SKIP, False)):
        self.snapshots = []
        self.changes = []
        self.answer = answer
        self.queries = []

    def job_changed(self, job):
        self.changes.append(job.status)

    def progress(self, snap):
        self.snapshots.append(snap)

    def resolve_conflict(self, q):
        self.queries.append(q)
        return self.answer


@pytest.fixture
def queue_factory(rfs, logbook, tmp_path):
    queues = []

    def make(listener=None, **kw):
        q = TransferQueue(lambda serial: rfs, logbook, listener or Recorder(), tmp_path / "queue.json", **kw)
        queues.append(q)
        return q

    yield make
    for q in queues:
        q.shutdown()


def run(q, job, timeout=60):
    q.add(job)
    assert q.wait_idle(timeout)
    return job


def make_tree(base):
    (base / "Set").mkdir()
    (base / "Set" / "sub dir").mkdir()
    (base / "Set" / "empty").mkdir()
    (base / "Set" / "a.gba").write_bytes(os.urandom(10000))
    (base / "Set" / "sub dir" / "b (USA).gba").write_bytes(os.urandom(5000))
    (base / "Set" / "sub dir" / "ü it's.gba").write_bytes(b"u" * 77)
    return base / "Set"


def test_upload_single_file(queue_factory, rg35xx, tmp_path):
    src = tmp_path / "Pokemon Emerald.gba"
    src.write_bytes(os.urandom(200_000))
    rec = Recorder()
    job = run(queue_factory(rec), TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA, verify=VerifyMode.HASH))
    assert job.status is JobStatus.COMPLETED, job.error
    assert (rg35xx.host(GBA) / "Pokemon Emerald.gba").read_bytes() == src.read_bytes()
    assert job.verify_ok == 1 and job.files_done == 1 and job.bytes_done == 200_000
    assert JobStatus.PREPARING in rec.changes and JobStatus.VERIFYING in rec.changes
    assert job.items[0].verify.method == "sha256"


def test_upload_directory_recursive_with_empty_dirs(queue_factory, rg35xx, tmp_path):
    src = make_tree(tmp_path)
    job = run(queue_factory(), TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA, verify=VerifyMode.SIZE))
    assert job.status is JobStatus.COMPLETED, job.error
    dst = rg35xx.host(GBA) / "Set"
    assert (dst / "a.gba").read_bytes() == (src / "a.gba").read_bytes()
    assert (dst / "sub dir" / "b (USA).gba").exists()
    assert (dst / "sub dir" / "ü it's.gba").read_bytes() == b"u" * 77
    assert (dst / "empty").is_dir()
    assert job.files_total == 3 and job.bytes_total == 15077


def test_download_directory_recursive(queue_factory, rg35xx, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    job = run(queue_factory(), TransferJob(SERIAL, Direction.DOWNLOAD, ["/userdata/roms"], str(out),
                                           verify=VerifyMode.HASH))
    assert job.status is JobStatus.COMPLETED, job.error
    got = out / "roms" / "gba" / "ポケモン ルビー.gba"
    assert got.read_bytes() == b"\x03" * 333
    assert (out / "roms" / "snes").is_dir()
    assert job.verify_ok == job.files_done


@pytest.mark.skipif(not PTY_AVAILABLE, reason="pty required")
def test_progress_from_adb_output_is_real(queue_factory, rg35xx, tmp_path, mock_env):
    mock_env.set_option(SERIAL, "rate", 400_000)
    src = tmp_path / "big.gba"
    src.write_bytes(os.urandom(800_000))
    rec = Recorder()
    job = run(queue_factory(rec), TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA))
    assert job.status is JobStatus.COMPLETED
    adb_snaps = [s for s in rec.snapshots if s.file_source is ProgressSource.ADB_PERCENT]
    assert adb_snaps, "expected progress parsed from adb output"
    pcts = [s.file_percent for s in adb_snaps]
    assert pcts == sorted(pcts) and 0 < pcts[0] < 100
    assert any(s.speed for s in rec.snapshots), "speed should be measured on a 2s transfer"
    assert all(s.file_total == 800_000 for s in adb_snaps)
    assert rec.snapshots[-1].batch_bytes_done == 800_000


def test_progress_fallback_without_pty_measures_destination(rfs, logbook, rg35xx, tmp_path, mock_env):
    mock_env.set_option(SERIAL, "rate", 300_000)
    src = tmp_path / "f.bin"
    src.write_bytes(os.urandom(900_000))
    rec = Recorder()
    q = TransferQueue(lambda s: rfs, logbook, rec, None, use_pty=False)
    try:
        job = run(q, TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA))
    finally:
        q.shutdown()
    assert job.status is JobStatus.COMPLETED
    polled = [s for s in rec.snapshots if s.file_source is ProgressSource.DESTINATION_SIZE]
    assert polled, "expected destination-size polling fallback"
    assert all(s.file_bytes_done <= 900_000 for s in polled)
    # Before any measurement, the file progress is reported as indeterminate
    first = rec.snapshots[0]
    assert first.file_indeterminate


def test_cancel_reports_partial_file(queue_factory, rg35xx, tmp_path, mock_env):
    mock_env.set_option(SERIAL, "rate", 200_000)
    src = tmp_path / "slow.gba"
    src.write_bytes(os.urandom(2_000_000))
    q = queue_factory()
    job = TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA)
    q.add(job)
    deadline = time.time() + 20
    while time.time() < deadline and not (rg35xx.host(GBA) / "slow.gba").exists():
        time.sleep(0.05)
    time.sleep(0.4)
    q.cancel(job.id)
    assert q.wait_idle(20)
    assert job.status is JobStatus.CANCELLED
    assert job.items[0].status is ItemStatus.PARTIAL
    assert job.partial_paths == [GBA + "/slow.gba"]
    assert (rg35xx.host(GBA) / "slow.gba").stat().st_size < 2_000_000


def test_failed_push(queue_factory, rg35xx, tmp_path, mock_env, logbook):
    mock_env.set_option(SERIAL, "fail_push", True)
    src = tmp_path / "x.gba"
    src.write_bytes(b"x")
    job = run(queue_factory(), TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA))
    assert job.status is JobStatus.FAILED
    assert "Permission denied" in job.items[0].error
    assert any(e.op_id == job.id and e.level.label == "ERROR" for e in logbook.events())


def test_connection_loss_aborts_job(queue_factory, rg35xx, tmp_path, mock_env):
    mock_env.set_option(SERIAL, "fail_after_bytes", 100_000)
    mock_env.set_option(SERIAL, "disconnect_on_fail", True)
    src = make_tree(tmp_path)
    (src / "big.gba").write_bytes(os.urandom(300_000))
    job = run(queue_factory(), TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA))
    assert job.status is JobStatus.FAILED
    assert "disconnected" in job.error.lower() or "failed" in job.error.lower()
    assert job.partial_paths


def test_overwrite_policies(queue_factory, rg35xx, tmp_path):
    src = tmp_path / "gamelist.xml"
    src.write_text("<gameList><game/></gameList>\n")
    dst = rg35xx.host(GBA) / "gamelist.xml"
    q = queue_factory()

    job = run(q, TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA, overwrite="skip"))
    assert job.status is JobStatus.COMPLETED and job.files_skipped == 1
    assert dst.read_text() == "<gameList/>\n"

    job = run(q, TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA, overwrite="rename"))
    assert job.status is JobStatus.COMPLETED
    assert dst.read_text() == "<gameList/>\n"
    assert (rg35xx.host(GBA) / "gamelist (1).xml").read_text() == src.read_text()

    job = run(q, TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA, overwrite="backup"))
    assert job.status is JobStatus.COMPLETED, job.error
    assert dst.read_text() == src.read_text()
    backups = [p for p in os.listdir(rg35xx.host(GBA)) if p.startswith("gamelist.xml.bak-")]
    assert len(backups) == 1
    assert (rg35xx.host(GBA) / backups[0]).read_text() == "<gameList/>\n"

    src.write_text("v3\n")
    job = run(q, TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA, overwrite="overwrite"))
    assert job.status is JobStatus.COMPLETED and dst.read_text() == "v3\n"


def test_ask_policy_uses_resolver_with_apply_all(queue_factory, rg35xx, tmp_path):
    src = make_tree(tmp_path)
    q = queue_factory()
    run(q, TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA))
    rec = Recorder(answer=(ConflictAction.SKIP, True))
    q2 = queue_factory(rec)
    job = run(q2, TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA, overwrite="ask"))
    assert job.status is JobStatus.COMPLETED
    assert len(rec.queries) == 1  # apply-to-all answered once
    assert job.files_skipped == 3


def test_download_does_not_overwrite_local_by_default(queue_factory, rg35xx, tmp_path):
    out = tmp_path / "dl"
    out.mkdir()
    (out / "gamelist.xml").write_text("LOCAL")
    job = run(queue_factory(), TransferJob(SERIAL, Direction.DOWNLOAD, [GBA + "/gamelist.xml"], str(out), overwrite="ask"))
    assert (out / "gamelist.xml").read_text() == "LOCAL"
    assert job.files_skipped == 1


def test_verification_failure_detected(queue_factory, rg35xx, tmp_path, mock_env):
    mock_env.set_option(SERIAL, "corrupt_push", True)
    src = tmp_path / "rom.gba"
    src.write_bytes(os.urandom(5000))
    job = run(queue_factory(), TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA, verify=VerifyMode.HASH))
    assert job.status is JobStatus.FAILED
    assert job.verify_failed == 1
    assert job.items[0].status is ItemStatus.VERIFY_FAILED
    assert job.items[0].verify.local_value != job.items[0].verify.remote_value


def test_hash_or_download_without_remote_hash_tool(mock_env, adb, logbook, tmp_path):
    from openadb.core.capabilities import probe_capabilities

    mock_env.add_device("NOHASH", exclude_tools=("sha256sum", "md5sum"))
    caps = probe_capabilities(adb, "NOHASH")
    fs = RemoteFS(adb, "NOHASH", caps)
    q = TransferQueue(lambda s: fs, logbook, Recorder(), None)
    try:
        src = tmp_path / "r.gba"
        src.write_bytes(os.urandom(3000))
        job = run(q, TransferJob("NOHASH", Direction.UPLOAD, [str(src)], GBA, verify=VerifyMode.HASH_OR_DOWNLOAD))
        assert job.status is JobStatus.COMPLETED
        assert job.items[0].verify.method == "download+sha256"
        job2 = run(q, TransferJob("NOHASH", Direction.UPLOAD, [str(src)], GBA + "/x", verify=VerifyMode.HASH))
        assert job2.items[0].verify.method == "size (hash unavailable)"
    finally:
        q.shutdown()


def test_move_deletes_source_only_after_verification(queue_factory, rg35xx, tmp_path):
    src = make_tree(tmp_path)
    job = run(queue_factory(), TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA, move=True))
    assert job.status is JobStatus.COMPLETED, job.error
    assert not src.exists()
    assert (rg35xx.host(GBA) / "Set" / "a.gba").exists()


def test_move_keeps_source_when_verification_fails(queue_factory, rg35xx, tmp_path, mock_env):
    mock_env.set_option(SERIAL, "corrupt_push", True)
    src = tmp_path / "keep.gba"
    src.write_bytes(os.urandom(100))
    job = run(queue_factory(), TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA, move=True, verify=VerifyMode.HASH))
    assert job.status is JobStatus.FAILED
    assert src.exists()


def test_download_move_removes_remote(queue_factory, rg35xx, tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    job = run(queue_factory(), TransferJob(SERIAL, Direction.DOWNLOAD, ["/userdata/system/logs"], str(out), move=True))
    assert job.status is JobStatus.COMPLETED, job.error
    assert (out / "logs" / "es_log.txt").read_text() == "log\n"
    assert not (rg35xx.host("/userdata/system/logs") / "es_log.txt").exists()


def test_free_space_check(queue_factory, rg35xx, tmp_path, monkeypatch):
    from openadb.core.remote_fs import DiskSpace, RemoteFS

    monkeypatch.setattr(RemoteFS, "disk_space", lambda self, p, cancel=None: DiskSpace(1000, 900, 100))
    src = tmp_path / "toobig.gba"
    src.write_bytes(b"x" * 1000)
    job = run(queue_factory(), TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA))
    assert job.status is JobStatus.FAILED and "free space" in job.error.lower()
    assert not (rg35xx.host(GBA) / "toobig.gba").exists()


def test_symlinks_are_not_followed_on_upload(queue_factory, rg35xx, tmp_path):
    src = make_tree(tmp_path)
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "s.txt").write_text("s")
    os.symlink(secret, src / "link")
    job = run(queue_factory(), TransferJob(SERIAL, Direction.UPLOAD, [str(src)], GBA))
    assert job.status is JobStatus.COMPLETED
    assert not (rg35xx.host(GBA) / "Set" / "link").exists()
    assert any("symbolic links are not followed" in w for w in job.warnings)


def test_queue_persistence_marks_interrupted(rfs, logbook, tmp_path):
    path = tmp_path / "q.json"
    q = TransferQueue(lambda s: rfs, logbook, Recorder(), path)
    q.pause()
    job = TransferJob(SERIAL, Direction.UPLOAD, ["/nonexistent"], GBA)
    q.add(job)
    job.status = JobStatus.TRANSFERRING  # simulate crash mid-transfer
    q._persist()
    q.shutdown()
    q2 = TransferQueue(lambda s: rfs, logbook, Recorder(), path)
    try:
        loaded = q2.get(job.id)
        assert loaded.status is JobStatus.INTERRUPTED
        assert q2.paused
        assert q2.retry(job.id)
        assert loaded.status is JobStatus.PENDING
    finally:
        q2.shutdown()
    raw = path.read_text()
    assert "sources" in raw and "\"items\"" not in raw


def test_queue_pause_reorder_cancel_clear(rfs, logbook, tmp_path, rg35xx):
    q = TransferQueue(lambda s: rfs, logbook, Recorder(), None)
    try:
        q.pause()
        files = []
        for i in range(3):
            f = tmp_path / f"f{i}.gba"
            f.write_bytes(b"x")
            files.append(q.add(TransferJob(SERIAL, Direction.UPLOAD, [str(f)], GBA)))
        time.sleep(0.6)
        assert all(j.status is JobStatus.PENDING for j in files)
        assert q.move(files[2].id, -2)
        assert [j.id for j in q.jobs] == [files[2].id, files[0].id, files[1].id]
        q.cancel(files[1].id)
        assert files[1].status is JobStatus.CANCELLED
        q.resume()
        assert q.wait_idle(30)
        assert files[0].status is files[2].status is JobStatus.COMPLETED
        assert q.clear_finished() == 2
        assert [j.id for j in q.jobs] == [files[1].id]
    finally:
        q.shutdown()


def test_concurrency_two_jobs(rfs, logbook, tmp_path, rg35xx, mock_env):
    mock_env.set_option(SERIAL, "rate", 500_000)
    q = TransferQueue(lambda s: rfs, logbook, Recorder(), None, concurrency=2)
    try:
        jobs = []
        for i in range(2):
            f = tmp_path / f"c{i}.gba"
            f.write_bytes(os.urandom(500_000))
            jobs.append(q.add(TransferJob(SERIAL, Direction.UPLOAD, [str(f)], GBA)))
        seen = 0
        deadline = time.time() + 10
        while time.time() < deadline:
            seen = max(seen, len(q.running_ids()))
            if not q.has_active():
                break
            time.sleep(0.02)
        assert q.wait_idle(30)
        assert seen == 2
        assert all(j.status is JobStatus.COMPLETED for j in jobs)
    finally:
        q.shutdown()
