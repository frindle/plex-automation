import pytest

import llm_advisor


@pytest.fixture(autouse=True)
def _isolated_llm_pending(tmp_path, monkeypatch):
    """Keep the pending store off /data and out of other tests."""
    monkeypatch.setenv('LLM_PENDING_PATH', str(tmp_path / 'llm_pending.json'))
    llm_advisor._STORES.clear()
    llm_advisor._MODELS_CACHE.clear()
    yield
