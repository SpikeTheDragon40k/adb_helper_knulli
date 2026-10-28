from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mock_adb.mockdevice import MockAdbEnv  # noqa: E402

from openadb.core.adb import AdbClient  # noqa: E402
from openadb.core.capabilities import probe_capabilities  # noqa: E402
from openadb.core.logbook import Level, LogBook  # noqa: E402
from openadb.core.remote_fs import RemoteFS  # noqa: E402

SERIAL = "0123456789"


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("appdirs")
    monkeypatch.setenv("OPENADB_CONFIG_DIR", str(base / "config"))
    monkeypatch.setenv("OPENADB_DATA_DIR", str(base / "data"))


@pytest.fixture
def mock_env(tmp_path) -> MockAdbEnv:
    env = MockAdbEnv(tmp_path / "mock")
    (tmp_path / "mock").mkdir(exist_ok=True)
    return env


@pytest.fixture
def rg35xx(mock_env):
    """The simulated Anbernic RG35XX SP running Knulli."""
    return mock_env.add_device(SERIAL)


@pytest.fixture
def adb(mock_env) -> AdbClient:
    return AdbClient(mock_env.adb_path)


@pytest.fixture
def logbook(tmp_path) -> LogBook:
    book = LogBook(tmp_path / "logs", level=Level.DEBUG)
    yield book
    book.close()


@pytest.fixture
def caps(adb, rg35xx, logbook):
    return probe_capabilities(adb, SERIAL, logbook=logbook, adb_reported_model="Nexus 4")


@pytest.fixture
def rfs(adb, caps) -> RemoteFS:
    return RemoteFS(adb, SERIAL, caps)
