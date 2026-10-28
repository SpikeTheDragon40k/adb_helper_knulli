from openadb.core.progress import AdbProgressParser, ProgressSource, RateEstimator, format_bytes, format_duration


def test_modern_percent_with_cr_and_ansi():
    p = AdbProgressParser()
    ups = p.feed("\r\x1b[K[  5%] /userdata/roms/gba/a b.gba\r\x1b[K[ 50%] /userdata/roms/gba/a b.gba")
    pcts = [u.percent for u in ups]
    assert 5.0 in pcts and 50.0 in pcts
    assert ups[0].source is ProgressSource.ADB_PERCENT
    assert ups[0].current == "/userdata/roms/gba/a b.gba"
    assert ups[0].bytes_done is None  # never fabricated


def test_chunk_split_across_feeds():
    p = AdbProgressParser()
    assert [u.percent for u in p.feed("\r[ 1")] == []
    ups = p.feed("2%] /x\r[ 99%] /x\r")
    assert [u.percent for u in ups] == [12.0, 99.0]


def test_old_transferring_format():
    p = AdbProgressParser()
    ups = p.feed("Transferring: 1048576/4194304 (25%)\r")
    assert ups[0].source is ProgressSource.ADB_BYTES
    assert ups[0].bytes_done == 1048576 and ups[0].total == 4194304


def test_summary_lines():
    p = AdbProgressParser()
    p.feed("/tmp/a.gba: 1 file pushed, 0 skipped. 35.2 MB/s (16777216 bytes in 0.454s)\n")
    assert p.summary.files == 1 and p.summary.bytes == 16777216 and abs(p.summary.seconds - 0.454) < 1e-9
    p2 = AdbProgressParser()
    p2.feed("/x: 12 files pulled, 3 skipped.\n")
    assert p2.summary.files == 12 and p2.summary.skipped == 3 and p2.summary.bytes is None
    p3 = AdbProgressParser()
    p3.feed("1234 KB/s (5000 bytes in 0.004s)\n")
    assert p3.summary.bytes == 5000


def test_rate_estimator_needs_real_samples():
    r = RateEstimator()
    assert r.rate() is None
    r.add(0, now=0.0)
    r.add(100, now=0.2)
    assert r.rate() is None  # too short to be meaningful
    r.add(1000, now=1.0)
    assert r.rate() == 1000.0
    assert r.eta(5000) == 5.0


def test_formatters():
    assert format_bytes(None) == "—"
    assert format_bytes(512) == "512 B"
    assert format_bytes(8 * 1024 * 1024) == "8.0 MiB"
    assert format_duration(3.02) == "0:03"
    assert format_duration(3725) == "1:02:05"
