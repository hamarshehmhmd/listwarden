import os
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(__file__)
FIX = os.path.join(HERE, "fixtures")
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cassette(monkeypatch):
    path = os.path.join(FIX, "cassette.json")
    monkeypatch.setenv("LISTWARDEN_CASSETTE", path)
    monkeypatch.setenv("LISTWARDEN_NOW", "2026-11-09T12:00:00Z")
    for k in ("LISTWARDEN_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    return path


@pytest.fixture
def list_md():
    with open(os.path.join(FIX, "list.md")) as fh:
        return fh.read()
