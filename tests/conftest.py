import json

import pytest


@pytest.fixture(autouse=True)
def isolated_model_sources(monkeypatch, tmp_path):
    """Keep model discovery off the developer's real Codex cache and off the network.

    The cache file below is a fixture, not a fallback: tests that need an empty source
    point ``CHATGPT_MODELS_FILE`` somewhere else themselves.
    """

    cache_path = tmp_path / "models_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "client_version": "0.0.0-test",
                "models": [
                    {"slug": "gpt-fixture-a", "visibility": "list", "supported_in_api": True},
                    {"slug": "gpt-fixture-b", "visibility": "list", "supported_in_api": True},
                    {"slug": "gpt-fixture-hidden", "visibility": "hide", "supported_in_api": True},
                    {"slug": "gpt-fixture-no-api", "visibility": "list", "supported_in_api": False},
                ],
            }
        )
    )

    monkeypatch.setenv("CHATGPT_MODELS_FILE", str(cache_path))
    monkeypatch.setenv("CHATGPT_CODEX_CONFIG_FILE", str(tmp_path / "missing_config.toml"))
    monkeypatch.setenv("CHATGPT_MODELS_URL", "")
    monkeypatch.setenv("CHATGPT_CAPABILITIES_URL", "")
    monkeypatch.setenv("CHATGPT_ACCOUNT_ID", "test-account")
    for name in (
        "CHATGPT_MODELS",
        "CHATGPT_EXTRA_MODELS",
        "CHATGPT_DEFAULT_MODEL",
        "CHATGPT_MEDIA_MODEL",
        "CHATGPT_REALTIME_MODEL",
        "CHATGPT_MODEL_ALIASES",
        "CHATGPT_EXTRA_MODEL_ALIASES",
    ):
        monkeypatch.setenv(name, "")
