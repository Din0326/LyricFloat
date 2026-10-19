import pytest


@pytest.fixture(autouse=True)
def private_app_data(tmp_path_factory, monkeypatch):
    """Tests never touch the real %APPDATA% / %LOCALAPPDATA% (play history, cache, settings):
    a test that forgot to pass a path once wrote into Din's history.jsonl."""
    root = tmp_path_factory.mktemp("appdata")
    monkeypatch.setenv("APPDATA", str(root / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(root / "Local"))


def pytest_addoption(parser):
    parser.addoption(
        "--record-contracts",
        action="store_true",
        default=False,
        help="re-record source contract cassettes against the live endpoints (lyric text is scrubbed)",
    )
