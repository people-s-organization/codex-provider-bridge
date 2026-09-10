import pytest
from fastapi.testclient import TestClient

import model_registry
from bridge import ChatGPTBridge
from main import app
from schemas import (
    AudioSpeechRequest,
    ChatCompletionRequest,
    ImageGenerationRequest,
    ResponsesRequest,
)


client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_upstream_cache():
    model_registry._fetch_cache.update({"at": 0.0, "ids": [], "error": None})
    model_registry._capabilities_cache.update({"at": 0.0, "payload": None, "error": None})
    yield
    model_registry._fetch_cache.update({"at": 0.0, "ids": [], "error": None})
    model_registry._capabilities_cache.update({"at": 0.0, "payload": None, "error": None})


def _missing_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATGPT_MODELS_FILE", str(tmp_path / "does_not_exist.json"))


def _empty_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "empty_models_cache.json"
    cache_path.write_text('{"client_version": "0.0.0-test", "models": []}')
    monkeypatch.setenv("CHATGPT_MODELS_FILE", str(cache_path))


def test_model_list_comes_from_cache_without_media_extras():
    assert model_registry.available_model_ids() == ["gpt-fixture-a", "gpt-fixture-b"]


def test_model_snapshot_reports_ids_default_and_source():
    snapshot = model_registry.model_snapshot()

    assert snapshot["model_ids"] == ["gpt-fixture-a", "gpt-fixture-b"]
    assert [model["id"] for model in snapshot["models"]] == ["gpt-fixture-a", "gpt-fixture-b"]
    assert snapshot["default_model"] == "gpt-fixture-a"
    assert snapshot["source"]["source"] == "models_cache"


def test_no_hardcoded_models_when_nothing_is_available(monkeypatch, tmp_path):
    _missing_cache(monkeypatch, tmp_path)

    assert model_registry.available_model_ids() == []
    assert model_registry.available_models() == []
    assert model_registry.default_model_id() is None
    assert model_registry.model_source()["source"] == "none"

    response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}


def test_upstream_lookup_is_used_when_cache_is_empty(monkeypatch, tmp_path):
    _empty_cache(monkeypatch, tmp_path)
    captured = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"models": [{"slug": "gpt-live", "visibility": "list", "supported_in_api": True}]}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return _FakeResponse()

    monkeypatch.setenv("CHATGPT_MODELS_URL", "https://example.test/backend-api/codex/models")
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(model_registry.httpx, "Client", _FakeClient)

    assert model_registry.available_model_ids() == ["gpt-live"]
    assert captured["url"] == "https://example.test/backend-api/codex/models"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["params"] == {"client_version": "0.0.0-test"}
    assert model_registry.model_source()["source"] == "upstream"


def test_upstream_failure_is_reported_not_hidden(monkeypatch, tmp_path):
    _empty_cache(monkeypatch, tmp_path)

    class _FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            class _Response:
                status_code = 500

            return _Response()

    monkeypatch.setenv("CHATGPT_MODELS_URL", "https://example.test/backend-api/codex/models")
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(model_registry.httpx, "Client", _FailingClient)

    assert model_registry.available_model_ids() == []
    source = model_registry.model_source()
    assert source["source"] == "none"
    assert "500" in source["error"]


def test_configured_models_still_win(monkeypatch):
    monkeypatch.setenv("CHATGPT_MODELS", "gpt-explicit")

    assert model_registry.available_model_ids() == ["gpt-explicit"]
    assert model_registry.model_source()["source"] == "CHATGPT_MODELS"


def test_no_builtin_aliases_remain(monkeypatch):
    assert model_registry.resolve_model_name("gpt-4.1") == "gpt-4.1"

    monkeypatch.setenv("CHATGPT_EXTRA_MODEL_ALIASES", "gpt-4.1=gpt-fixture-a")
    assert model_registry.resolve_model_name("gpt-4.1") == "gpt-fixture-a"


def test_image_request_requires_explicit_model():
    response = client.post("/v1/images/generations", json={"prompt": "hello"})
    assert response.status_code == 422

    response = client.post("/v1/images/generations", json={"model": "", "prompt": "hello"})
    assert response.status_code == 422

    response = client.post("/v1/images/generations", json={"model": "   ", "prompt": "hello"})
    assert response.status_code == 422


def test_audio_request_requires_explicit_model():
    response = client.post("/v1/audio/speech", json={"input": "hello"})
    assert response.status_code == 422

    response = client.post("/v1/audio/speech", json={"model": "", "input": "hello"})
    assert response.status_code == 422

    response = client.post("/v1/audio/speech", json={"model": "   ", "input": "hello"})
    assert response.status_code == 422


def test_capabilities_report_image_tool_models(monkeypatch):
    class _FakeResponse:
        status_code = 200

        def json(self):
            return {
                "models": [
                    {"slug": "gpt-a", "enabled_tools": ["tools", "image_gen_tool_enabled"]},
                    {"slug": "gpt-b", "enabled_tools": ["tools", "dalle_3"]},
                    {"slug": "gpt-c", "enabled_tools": ["tools"]},
                ]
            }

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None, params=None):
            return _FakeResponse()

    monkeypatch.setenv("CHATGPT_CAPABILITIES_URL", "https://example.test/backend-api/models")
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(model_registry.httpx, "Client", _FakeClient)

    caps = model_registry.capabilities()

    assert caps["source"] == "chatgpt_models"
    assert caps["image_generation"] == {"available": True, "tool_models": ["gpt-a", "gpt-b"]}
    assert caps["realtime_speech"]["model_listing"] is False
    assert caps["tool_calling"]["available"] is False
    assert caps["tool_calling"]["scope"] == "bridge"


def test_capabilities_without_a_source_report_unavailable():
    caps = model_registry.capabilities()

    assert caps["source"] == "none"
    assert caps["image_generation"] == {"available": False, "tool_models": []}
    assert caps["tool_calling"]["available"] is False
    assert caps["error"]


def test_codex_image_payload_uses_detected_model_for_image_model_names():
    # gpt-image-* style names are rejected by the Codex Responses endpoint, so a real
    # model from the live listing has to drive the image tool instead.
    bridge = ChatGPTBridge()
    payload = bridge._build_codex_image_payload(
        ImageGenerationRequest(model="gpt-image-2", prompt="hello")
    )

    assert payload["model"] == "gpt-fixture-a"


def test_codex_image_payload_uses_requested_model_when_it_is_real():
    bridge = ChatGPTBridge()
    payload = bridge._build_codex_image_payload(
        ImageGenerationRequest(model="gpt-fixture-b", prompt="hello")
    )

    assert payload["model"] == "gpt-fixture-b"


def test_codex_image_payload_prefers_configured_media_model(monkeypatch):
    monkeypatch.setenv("CHATGPT_MEDIA_MODEL", "gpt-fixture-a")
    bridge = ChatGPTBridge()
    payload = bridge._build_codex_image_payload(
        ImageGenerationRequest(model="gpt-explicit-image", prompt="hello")
    )

    assert payload["model"] == "gpt-fixture-a"


def test_chat_payload_drops_upstream_rejected_output_limit():
    bridge = ChatGPTBridge()
    payload = bridge._build_payload(
        ChatCompletionRequest(
            model="gpt-fixture-a",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=16,
        )
    )

    assert "max_output_tokens" not in payload
    assert "max_tokens" not in payload


def test_responses_payload_drops_upstream_rejected_output_limit():
    bridge = ChatGPTBridge()
    payload = bridge._build_responses_payload(
        ResponsesRequest(model="gpt-fixture-a", input="hi", max_output_tokens=16)
    )

    assert "max_output_tokens" not in payload


def test_realtime_model_falls_back_to_request_model_only():
    bridge = ChatGPTBridge()
    request = AudioSpeechRequest(model="tts-explicit", input="hello")

    assert bridge._resolve_realtime_model(request) == "tts-explicit"


def test_realtime_model_prefers_configured_model(monkeypatch):
    monkeypatch.setenv("CHATGPT_REALTIME_MODEL", "gpt-fixture-b")
    bridge = ChatGPTBridge()
    request = AudioSpeechRequest(model="tts-explicit", input="hello")

    assert bridge._resolve_realtime_model(request) == "gpt-fixture-b"
