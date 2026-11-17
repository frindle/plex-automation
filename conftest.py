import os

# Tests must never reach the real services. Every service address points at
# a port nothing listens on, so a test that forgets to fake a call fails fast
# instead of logging in to Deluge or calling the live arr-webhook. A test that
# needs a specific address still sets its own.
_DEAD = 'http://127.0.0.1:9'
for _k in ('DELUGE_URL', 'SONARR_URL', 'RADARR_URL', 'PLEX_URL', 'TIERING_API', 'WEBHOOK'):
    os.environ[_k] = _DEAD

import pytest

import llm_advisor


@pytest.fixture(autouse=True)
def _isolated_llm_pending(tmp_path, monkeypatch):
    """Keep the pending store off /data and out of other tests."""
    monkeypatch.setenv('LLM_PENDING_PATH', str(tmp_path / 'llm_pending.json'))
    llm_advisor._STORES.clear()
    llm_advisor._MODELS_CACHE.clear()
    llm_advisor._UNRAID_HEALTH.clear()
    yield
