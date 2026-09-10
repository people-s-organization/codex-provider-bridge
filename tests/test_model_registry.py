import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

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
def reset_upstream_cache(monkeypatch):
    monkeypatch.setenv("CHATGPT_CLIENT_VERSION", "")
    monkeypatch.setenv("CHATGPT_CODEX_EXECUTABLE", "")
    model_registry._fetch_cache.clear()
    model_registry._capabilities_cache.clear()
    model_registry._installed_codex_version.cache_clear()
    yield
    model_registry._fetch_cache.clear()
    model_registry._capabilities_cache.clear()
    model_registry._installed_codex_version.cache_clear()


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
    assert response.status_code == 400

    response = client.post("/v1/images/generations", json={"model": "", "prompt": "hello"})
    assert response.status_code == 400

    response = client.post("/v1/images/generations", json={"model": "   ", "prompt": "hello"})
    assert response.status_code == 400


def test_audio_request_requires_explicit_model():
    response = client.post("/v1/audio/speech", json={"input": "hello"})
    assert response.status_code == 400

    response = client.post("/v1/audio/speech", json={"model": "", "input": "hello"})
    assert response.status_code == 400

    response = client.post("/v1/audio/speech", json={"model": "   ", "input": "hello"})
    assert response.status_code == 400


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
    assert caps["image_generation"]["available"] is True
    assert caps["image_generation"]["tool_models"] == ["gpt-a", "gpt-b"]
    assert caps["image_generation"]["status"] == "available"
    assert caps["realtime_speech"]["model_listing"] is False
    assert caps["tool_calling"]["available"] is True
    assert caps["tool_calling"]["scope"] == "bridge"


def test_capabilities_without_a_source_report_unknown():
    caps = model_registry.capabilities()

    assert caps["source"] == "none"
    assert caps["image_generation"]["available"] is None
    assert caps["image_generation"]["status"] == "unknown"
    assert caps["image_generation"]["tool_models"] == []
    assert caps["tool_calling"]["available"] is True
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


def _mock_listing(monkeypatch, payload=None, status=200, callback=None):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if callback:
                callback()
            return SimpleNamespace(status_code=status, json=lambda: payload)

    monkeypatch.setattr(model_registry.httpx, "Client", Client)
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "token-one")
    monkeypatch.setenv("CHATGPT_MODELS_URL", "https://one.test/models")
    monkeypatch.setenv("CHATGPT_CAPABILITIES_URL", "https://one.test/capabilities")
    return calls


def test_missing_cache_discovers_installed_codex_version(monkeypatch, tmp_path):
    _missing_cache(monkeypatch, tmp_path)
    monkeypatch.delenv("CHATGPT_CLIENT_VERSION", raising=False)
    executable = tmp_path / "codex"
    executable.write_text("fixture")
    monkeypatch.setattr(model_registry.shutil, "which", lambda name: str(executable))
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="codex-cli 0.134.1\n")

    monkeypatch.setattr(model_registry.subprocess, "run", run)
    calls = _mock_listing(monkeypatch, {"models": [{"slug": "live"}]})
    assert model_registry.available_model_ids() == ["live"]
    assert commands == [[str(executable), "--version"]]
    assert calls[0][1]["params"] == {"client_version": "0.134.1"}
    assert calls[0][1]["headers"]["version"] == "0.134.1"


def test_installed_codex_local_bin_discovery_without_path(monkeypatch, tmp_path):
    _missing_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("CHATGPT_CLIENT_VERSION", "")
    monkeypatch.setattr(model_registry.shutil, "which", lambda name: None)
    monkeypatch.setattr(model_registry.Path, "home", lambda: tmp_path)
    executable = tmp_path / ".local/bin/codex"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture")
    monkeypatch.setattr(model_registry.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="codex-cli 0.154.0"))
    assert model_registry._client_version() == "0.154.0"


def test_missing_version_reports_actionable_error_without_fake_request(monkeypatch, tmp_path):
    _missing_cache(monkeypatch, tmp_path)
    monkeypatch.delenv("CHATGPT_CLIENT_VERSION", raising=False)
    monkeypatch.setattr(model_registry.shutil, "which", lambda name: None)
    monkeypatch.setenv("CHATGPT_CODEX_EXECUTABLE", str(tmp_path / "missing-codex"))
    calls = _mock_listing(monkeypatch, {"models": [{"slug": "live"}]})
    assert model_registry.available_model_ids() == []
    assert "client_version" in model_registry.model_source()["error"]
    assert not calls


def test_version_override_and_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("CHATGPT_MODELS_FILE", "")
    monkeypatch.setenv("CHATGPT_CODEX_CONFIG_FILE", "")
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": ["one", "two"]}))
    (tmp_path / "config.toml").write_text('model = "two"')
    monkeypatch.setenv("CHATGPT_CLIENT_VERSION", "1.2.3")
    assert model_registry.available_model_ids() == ["two", "one"]
    assert model_registry._client_version() == "1.2.3"


@pytest.mark.parametrize("source", ["env", "codex"])
def test_defaults_do_not_fabricate_models(monkeypatch, tmp_path, source):
    if source == "env":
        monkeypatch.setenv("CHATGPT_DEFAULT_MODEL", "unavailable")
    else:
        config = tmp_path / "config.toml"
        config.write_text('model = "unavailable"')
        monkeypatch.setenv("CHATGPT_CODEX_CONFIG_FILE", str(config))
    assert model_registry.available_model_ids() == ["gpt-fixture-a", "gpt-fixture-b"]
    _missing_cache(monkeypatch, tmp_path)
    assert model_registry.available_model_ids() == []


@pytest.mark.parametrize("kind", ["models", "capabilities"])
@pytest.mark.parametrize("changed", ["CHATGPT_ACCOUNT_ID", "CHATGPT_ACCESS_TOKEN", "url"])
def test_fetch_caches_are_identity_keyed(monkeypatch, tmp_path, kind, changed):
    _empty_cache(monkeypatch, tmp_path)
    calls = _mock_listing(monkeypatch, {"models": [{"slug": "live", "enabled_tools": []}]})
    fetch = model_registry.available_model_ids if kind == "models" else model_registry.capabilities
    fetch()
    fetch()
    assert len(calls) == 1
    variable = ("CHATGPT_MODELS_URL" if kind == "models" else "CHATGPT_CAPABILITIES_URL") if changed == "url" else changed
    monkeypatch.setenv(variable, "https://two.test/list" if changed == "url" else "identity-two")
    fetch()
    assert len(calls) == 2


def test_refresh_bypasses_disk_and_ttl_without_duplicate_snapshot_fetch(monkeypatch):
    calls = _mock_listing(monkeypatch, {"models": [{"slug": "live"}]})
    assert model_registry.model_snapshot()["source"]["source"] == "models_cache"
    snapshot = model_registry.model_snapshot(refresh=True)
    assert snapshot["model_ids"] == ["live"]
    assert snapshot["source"]["source"] == "upstream"
    assert len(calls) == 1
    model_registry.model_snapshot(refresh=True)
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["models", "capabilities"])
def test_concurrent_fetches_coalesce(monkeypatch, tmp_path, kind):
    _empty_cache(monkeypatch, tmp_path)
    entered = threading.Event()
    release = threading.Event()
    waiter = threading.Event()
    original_result = model_registry.Future.result

    def result(self, *args, **kwargs):
        waiter.set()
        return original_result(self, *args, **kwargs)

    monkeypatch.setattr(model_registry.Future, "result", result)

    def block():
        entered.set()
        assert release.wait(5)

    calls = _mock_listing(monkeypatch, {"models": [{"slug": "live", "enabled_tools": []}]}, callback=block)
    fetch = model_registry.available_model_ids if kind == "models" else model_registry.capabilities
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fetch, refresh=True)
        assert entered.wait(5)
        second = pool.submit(fetch, refresh=True)
        try:
            assert waiter.wait(5)
        finally:
            release.set()
        assert first.result() == second.result()
    assert len(calls) == 1


@pytest.mark.parametrize("payload,status,expected", [
    ({"models": [{"slug": "one", "enabled_tools": []}]}, 200, "unsupported"),
    ({"models": [{"slug": "one"}]}, 200, "unknown"),
    ({"models": []}, 200, "unknown"),
    ({"unexpected": []}, 200, "unknown"),
    ({"models": []}, 403, "unknown"),
])
def test_capability_unknown_is_not_unsupported(monkeypatch, payload, status, expected):
    calls = _mock_listing(monkeypatch, payload, status)
    caps = model_registry.capabilities()
    assert caps["image_generation"]["status"] == expected
    assert caps["image_generation"]["available"] is (False if expected == "unsupported" else None)
    assert caps["realtime_speech"]["available"] is None
    assert caps["tool_calling"]["upstream_supports_function_tools"] is None
    model_registry.capabilities(refresh=True)
    assert len(calls) == 2


def test_cache_ttl_starts_when_fetch_completes(monkeypatch, tmp_path):
    _empty_cache(monkeypatch, tmp_path)
    clock = [0.0]
    monkeypatch.setattr(model_registry.time, "monotonic", lambda: clock[0])
    calls = _mock_listing(monkeypatch, {"models": ["live"]}, callback=lambda: clock.__setitem__(0, 100.0))
    assert model_registry.available_model_ids() == ["live"]
    clock[0] = 110.0
    assert model_registry.available_model_ids() == ["live"]
    assert len(calls) == 1
    clock[0] = 161.0
    model_registry.available_model_ids()
    assert len(calls) == 2


def test_expired_disk_cache_refreshes_on_ordinary_snapshot(monkeypatch):
    calls = _mock_listing(monkeypatch, {"models": ["fresh-live"]})
    written = model_registry._models_cache_path().stat().st_mtime
    monkeypatch.setattr(model_registry.time, "time", lambda: written + 61)
    snapshot = model_registry.model_snapshot()
    assert snapshot["model_ids"] == ["fresh-live"]
    assert snapshot["source"]["source"] == "upstream"
    assert snapshot["source"]["cache_stale"] is True
    assert model_registry.available_model_ids() == ["fresh-live"]
    assert len(calls) == 1


def test_expired_disk_cache_failure_does_not_advertise_stale_models(monkeypatch):
    calls = _mock_listing(monkeypatch, status=503)
    written = model_registry._models_cache_path().stat().st_mtime
    monkeypatch.setattr(model_registry.time, "time", lambda: written + 61)
    snapshot = model_registry.model_snapshot()
    assert snapshot["model_ids"] == []
    assert snapshot["default_model"] is None
    assert snapshot["source"]["source"] == "none"
    assert snapshot["source"]["status"] == "unknown"
    assert snapshot["source"]["cache_stale"] is True
    assert "503" in snapshot["source"]["error"]
    assert len(calls) == 1


def test_disk_cache_expires_without_restarting_process(monkeypatch):
    calls = _mock_listing(monkeypatch, {"models": ["fresh-live"]})
    written = model_registry._models_cache_path().stat().st_mtime
    now = [written + 10]
    monkeypatch.setattr(model_registry.time, "time", lambda: now[0])
    assert model_registry.available_model_ids() == ["gpt-fixture-a", "gpt-fixture-b"]
    assert not calls
    now[0] = written + 60
    assert model_registry.available_model_ids() == ["fresh-live"]
    assert len(calls) == 1
