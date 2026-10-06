"""Shared pytest configuration: marker gating + settings isolation."""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from splitwise_mcp.config import get_settings

# Load .env so live-marked tests can pick up the real key; non-live tests
# are isolated from it by the autouse fixture below.
load_dotenv()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    has_key = bool(os.environ.get("SPLITWISE_API_KEY"))
    allow_writes = os.environ.get("SPLITWISE_TEST_ALLOW_WRITES") == "1"
    skip_live = pytest.mark.skip(reason="live tests require SPLITWISE_API_KEY in the environment / .env")
    skip_write = pytest.mark.skip(
        reason="live_write tests run ONLY with SPLITWISE_API_KEY set AND SPLITWISE_TEST_ALLOW_WRITES=1"
    )
    for item in items:
        if "live" in item.keywords and not has_key:
            item.add_marker(skip_live)
        if "live_write" in item.keywords and not (has_key and allow_writes):
            item.add_marker(skip_write)


@pytest.fixture(autouse=True)
def _isolate_settings_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Keep the developer's real SPLITWISE_* env / .env out of non-live tests.

    Strips ambient SPLITWISE_* vars, chdirs away from the repo (so
    `Settings(env_file=".env")` finds nothing), and clears the settings cache
    before and after each non-live test. Live tests are left untouched.
    """
    if "live" in request.keywords or "live_write" in request.keywords:
        return
    for key in list(os.environ):
        if key.startswith("SPLITWISE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path_factory.mktemp("isolated-cwd"))
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
