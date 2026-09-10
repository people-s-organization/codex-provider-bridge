"""HTTP-surface conformance: behave like a real OpenAI API endpoint.

These tests pin the OpenAI-shaped error envelope, 400-vs-422 validation status,
404 for unknown routes, CORS, model retrieval, stored-response retrieval/deletion,
SSE keep-alive pings and the bridge-key 401 envelope.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import main
from config import settings
from response_store import response_store

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def conformance_defaults(monkeypatch):
    monkeypatch.setattr(settings, "bridge_api_key", "")
    monkeypatch.setattr(settings, "stream_keepalive_seconds", 15.0)
    monkeypatch.setattr(settings, "cors_origins", "*")
    monkeypatch.delenv("BRIDGE_CORS_ORIGINS", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("BRIDGE_STRICT_COMPATIBILITY", raising=False)
    yield


def assert_openai_error(response, status, code, error_type="invalid_request_error"):
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-request-id"]
    error = response.json()["error"]
    assert set(error) == {"message", "type", "param", "code"}
    assert error["type"] == error_type
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    return error


# --- 1. validation: 400 + OpenAI envelope -----------------------------------

def test_invalid_parameter_is_400_with_actionable_envelope():
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-fixture-a",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "bogus",
        },
    )
    error = assert_openai_error(response, 400, "invalid_request")
    assert error["message"] == "tool_choice must be auto, none, required, or a named function"
    assert error["param"] == "tool_choice"
    assert "hi" not in response.text


def test_missing_required_field_is_400_naming_the_param():
    response = client.post("/v1/chat/completions", json={"model": "gpt-fixture-a"})
    error = assert_openai_error(response, 400, "invalid_request")
    assert error["param"] == "messages"


def test_malformed_json_is_400_with_null_param_and_no_echo():
    response = client.post(
        "/v1/chat/completions",
        content=b'{"secret-token": ',
        headers={"Content-Type": "application/json"},
    )
    error = assert_openai_error(response, 400, "invalid_request")
    assert error["message"] == "Invalid request body"
    assert error["param"] is None
    assert "secret-token" not in response.text


def test_validation_400_keeps_request_id_header():
    response = client.post(
        "/v1/responses",
        json={"model": "gpt-fixture-a", "input": "hi", "stream": {"not": "a bool"}},
    )
    assert response.status_code == 400
    assert len(response.headers["x-request-id"]) == 32


# --- 2. unknown routes vs known-but-unavailable capabilities ----------------

@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_unknown_v1_route_is_404_invalid_url(method):
    response = client.request(method, "/v1/unknown-capability")
    error = assert_openai_error(response, 404, None)
    assert error["message"] == f"Invalid URL ({method} /v1/unknown-capability)"
    assert error["param"] is None


@pytest.mark.parametrize(
    "path",
    [
        "/v1/embeddings",
        "/v1/moderations",
        "/v1/files",
        "/v1/files/file-abc",
        "/v1/batches",
        "/v1/fine_tuning/jobs",
        "/v1/vector_stores",
        "/v1/assistants",
        "/v1/audio/transcriptions",
        "/v1/audio/translations",
        "/v1/images/edits",
        "/v1/images/variations",
        "/v1/responses/input_tokens",
    ],
)
def test_known_unavailable_capabilities_stay_unsupported(path):
    response = client.post(path, json={"model": "x"})
    error = assert_openai_error(response, 501, "unsupported_endpoint", error_type="unsupported_endpoint")
    assert "Reason:" in error["message"]


def test_unsupported_route_does_not_mask_unknown_route():
    assert client.post("/v1/definitely-not-real", json={}).status_code == 404
    assert client.post("/v1/embeddings", json={}).status_code == 501


def test_unknown_route_is_404_even_with_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proxy-test")
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.captured = None

    response = client.post("/v1/definitely-not-real", json={})

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Invalid URL (POST /v1/definitely-not-real)"
    assert _FakeAsyncClient.captured is None


def test_non_v1_unknown_route_keeps_openai_json_envelope():
    error = assert_openai_error(client.get("/definitely-not-real"), 404, None)
    assert error["message"] == "Invalid URL (GET /definitely-not-real)"


# --- 2b. proxy path still works (JSON and multipart) ------------------------

class _FakeUpstreamResponse:
    def __init__(self, status_code=200, content=b'{"object":"list","data":[]}', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "application/json"}
        self.is_error = status_code >= 400


class _FakeAsyncClient:
    captured = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, params=None, headers=None, content=None):
        type(self).captured = {
            "method": method,
            "url": url,
            "params": params,
            "headers": headers,
            "content": content,
        }
        return _FakeUpstreamResponse()


@pytest.fixture
def proxy_capture(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proxy-test")
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.captured = None
    return _FakeAsyncClient


def test_proxy_forwards_json_body_when_openai_key_configured(proxy_capture):
    payload = {"model": "text-embedding-3-small", "input": "hello"}
    response = client.post("/v1/embeddings", json=payload)

    assert response.status_code == 200
    captured = proxy_capture.captured
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/embeddings")
    assert captured["headers"]["Authorization"] == "Bearer sk-proxy-test"
    assert captured["headers"]["content-type"] == "application/json"
    assert json.loads(captured["content"]) == payload


def test_proxy_forwards_multipart_body_when_openai_key_configured(proxy_capture):
    response = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", b"RIFF-FAKE-AUDIO", "audio/wav")},
        data={"model": "whisper-1"},
    )

    assert response.status_code == 200
    captured = proxy_capture.captured
    assert captured["url"].endswith("/v1/audio/transcriptions")
    assert captured["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"' in captured["content"]
    assert b"RIFF-FAKE-AUDIO" in captured["content"]
    assert b'name="model"' in captured["content"]


# --- 3. CORS ----------------------------------------------------------------

def test_cors_preflight_defaults_to_wildcard_without_credentials():
    response = client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert len(response.headers["x-request-id"]) == 32
    methods = response.headers["access-control-allow-methods"]
    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        assert method in methods
    assert "authorization" in response.headers["access-control-allow-headers"]
    assert "content-type" in response.headers["access-control-allow-headers"]
    assert "access-control-allow-credentials" not in response.headers


def test_cors_preflight_bypasses_bridge_auth(monkeypatch):
    monkeypatch.setattr(settings, "bridge_api_key", "secret-key")

    response = client.options(
        "/v1/chat/completions",
        headers={"Origin": "https://app.example", "Access-Control-Request-Method": "POST"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_cors_actual_request_exposes_bridge_headers():
    response = client.get("/v1/models", headers={"Origin": "https://app.example"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    exposed = response.headers["access-control-expose-headers"]
    assert "x-request-id" in exposed


def test_cors_explicit_origin_list_enables_credentials(monkeypatch):
    monkeypatch.setenv("BRIDGE_CORS_ORIGINS", "https://allowed.example, https://other.example")

    allowed = client.options(
        "/v1/chat/completions",
        headers={"Origin": "https://allowed.example", "Access-Control-Request-Method": "POST"},
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://allowed.example"
    assert allowed.headers["access-control-allow-credentials"] == "true"

    actual = client.get("/v1/models", headers={"Origin": "https://allowed.example"})
    assert actual.headers["access-control-allow-origin"] == "https://allowed.example"
    assert actual.headers["access-control-allow-credentials"] == "true"

    denied = client.options(
        "/v1/chat/completions",
        headers={"Origin": "https://denied.example", "Access-Control-Request-Method": "POST"},
    )
    assert denied.status_code == 400


# --- 4. GET /v1/models/{model_id} -------------------------------------------

def test_retrieve_discovered_model_by_id():
    response = client.get("/v1/models/gpt-fixture-a")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "id": "gpt-fixture-a",
        "object": "model",
        "created": 0,
        "owned_by": "openai",
    }


def test_retrieve_model_alias_route_without_v1():
    response = client.get("/models/gpt-fixture-b")

    assert response.status_code == 200
    assert response.json()["id"] == "gpt-fixture-b"


def test_retrieve_configured_alias_model(monkeypatch):
    monkeypatch.setenv("CHATGPT_EXTRA_MODEL_ALIASES", "my-alias=gpt-fixture-a")

    response = client.get("/v1/models/my-alias")

    assert response.status_code == 200
    assert response.json()["id"] == "my-alias"


@pytest.mark.parametrize("model_id", ["does-not-exist", "gpt-fixture-hidden", "gpt-fixture-no-api"])
def test_retrieve_unknown_model_is_404_model_not_found(model_id):
    response = client.get(f"/v1/models/{model_id}")

    error = assert_openai_error(response, 404, "model_not_found")
    assert error["message"] == (
        f"The model '{model_id}' does not exist or you do not have access to it."
    )
    assert error["param"] is None


def test_retrieve_unknown_model_alias_route_is_404():
    response = client.get("/models/nope-not-a-model")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_chat_unknown_model_is_local_404_model_not_found():
    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-does-not-exist-xyz", "messages": [{"role": "user", "content": "hi"}]},
    )
    error = assert_openai_error(response, 404, "model_not_found")
    assert error["param"] == "model"


def test_unknown_previous_response_id_is_local_404():
    response = client.post(
        "/v1/responses",
        json={"model": "gpt-fixture-a", "input": "hi", "previous_response_id": "resp_missing_xyz"},
    )
    error = assert_openai_error(response, 404, "previous_response_not_found")
    assert error["param"] == "previous_response_id"


def test_missing_body_is_400_not_a_crash():
    response = client.post("/v1/chat/completions")
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")


# --- 5. GET/DELETE /v1/responses/{response_id} ------------------------------

def test_stored_response_roundtrip_through_real_store():
    response_store.store(
        {"id": "resp_conformance", "object": "response", "output_text": "ok"}
    )

    fetched = client.get("/v1/responses/resp_conformance")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == "resp_conformance"
    assert fetched.json()["output_text"] == "ok"

    deleted = client.delete("/v1/responses/resp_conformance")
    assert deleted.status_code == 200
    assert deleted.json() == {
        "id": "resp_conformance",
        "object": "response.deleted",
        "deleted": True,
    }

    missing = client.get("/v1/responses/resp_conformance")
    assert_openai_error(missing, 404, "response_not_found")


def test_responses_routes_win_over_catch_all_and_have_aliases(monkeypatch):
    async def fake_get(response_id):
        return {"id": response_id, "object": "response", "output_text": "hi"}, None

    async def fake_delete(response_id):
        return {"id": response_id, "object": "response.deleted", "deleted": True}, None

    monkeypatch.setattr(main.bridge, "get_stored_response", fake_get)
    monkeypatch.setattr(main.bridge, "delete_stored_response", fake_delete)

    assert client.get("/v1/responses/resp_x").json()["output_text"] == "hi"
    assert client.get("/responses/resp_y").json()["id"] == "resp_y"
    assert client.delete("/v1/responses/resp_x").json()["deleted"] is True
    assert client.delete("/responses/resp_y").json()["deleted"] is True


def test_stored_response_local_error_is_returned_verbatim(monkeypatch):
    async def fake_get(response_id):
        return None, {
            "status": 404,
            "local": True,
            "type": "invalid_request_error",
            "param": "response_id",
            "code": "response_not_found",
            "error": "No response found with id 'resp_missing'.",
        }

    monkeypatch.setattr(main.bridge, "get_stored_response", fake_get)

    response = client.get("/v1/responses/resp_missing")
    error = assert_openai_error(response, 404, "response_not_found")
    assert error["message"] == "No response found with id 'resp_missing'."
    assert error["param"] == "response_id"


def test_delete_missing_stored_response_is_404(monkeypatch):
    async def fake_delete(response_id):
        return None, {
            "status": 404,
            "local": True,
            "type": "invalid_request_error",
            "param": "response_id",
            "code": "response_not_found",
            "error": f"No response found with id '{response_id}'.",
        }

    monkeypatch.setattr(main.bridge, "delete_stored_response", fake_delete)

    error = assert_openai_error(client.delete("/v1/responses/resp_missing"), 404, "response_not_found")
    assert error["code"] == "response_not_found"


# --- upstream_error_to_response contract ------------------------------------

def test_upstream_error_local_fields_returned_verbatim():
    response = main.upstream_error_to_response(
        {
            "status": 404,
            "local": True,
            "type": "invalid_request_error",
            "param": "model",
            "code": "model_not_found",
            "error": "The model 'x' does not exist or you do not have access to it.",
        }
    )

    assert response.status_code == 404
    assert json.loads(response.body)["error"] == {
        "message": "The model 'x' does not exist or you do not have access to it.",
        "type": "invalid_request_error",
        "param": "model",
        "code": "model_not_found",
    }


def test_upstream_error_uses_sanitised_upstream_detail_when_present():
    response = main.upstream_error_to_response(
        {"status": 400, "upstream_detail": "Unsupported parameter: max_output_tokens", "error": "raw"}
    )

    assert response.status_code == 400
    error = json.loads(response.body)["error"]
    assert error["message"] == "Unsupported parameter: max_output_tokens"
    assert error["code"] == "upstream_error"


def test_upstream_error_without_detail_stays_generic():
    response = main.upstream_error_to_response({"status": 502, "error": "secret-token"})

    assert response.status_code == 502
    assert json.loads(response.body)["error"]["message"] == "Upstream request failed"
    assert b"secret-token" not in response.body


# --- 6. SSE keep-alive ------------------------------------------------------

def test_stream_keepalive_emits_ping_comments(monkeypatch):
    monkeypatch.setattr(settings, "stream_keepalive_seconds", 0.05)

    async def slow_stream(request):
        yield 'event: response.created\ndata: {"type":"response.created"}\n\n'
        await asyncio.sleep(0.2)
        yield 'event: response.completed\ndata: {"type":"response.completed"}\n\n'

    monkeypatch.setattr(main.bridge, "responses_stream", slow_stream)

    response = client.post("/v1/responses", json={"model": "gpt-fixture-a", "input": "hi", "stream": True})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert ": ping\n\n" in response.text
    assert "response.completed" in response.text


def test_stream_keepalive_disabled_injects_nothing(monkeypatch):
    monkeypatch.setattr(settings, "stream_keepalive_seconds", 0)

    async def slow_stream(request):
        yield 'event: response.created\ndata: {"type":"response.created"}\n\n'
        await asyncio.sleep(0.05)
        yield 'event: response.completed\ndata: {"type":"response.completed"}\n\n'

    monkeypatch.setattr(main.bridge, "responses_stream", slow_stream)

    response = client.post("/v1/responses", json={"model": "gpt-fixture-a", "input": "hi", "stream": True})

    assert response.status_code == 200
    assert ": ping" not in response.text


def test_non_streaming_never_gets_ping_bytes(monkeypatch):
    async def fake_responses(request):
        return {"id": "resp_1", "object": "response", "output_text": "ok"}, None

    monkeypatch.setattr(main.bridge, "responses", fake_responses)
    monkeypatch.setattr(settings, "stream_keepalive_seconds", 0.01)

    response = client.post("/v1/responses", json={"model": "gpt-fixture-a", "input": "hi"})

    assert response.status_code == 200
    assert ": ping" not in response.text


def test_stream_error_is_forwarded_after_keepalive_wrapper(monkeypatch):
    monkeypatch.setattr(settings, "stream_keepalive_seconds", 0.05)

    async def failing_stream(request):
        raise RuntimeError("secret-token in stream")
        yield  # pragma: no cover

    monkeypatch.setattr(main.bridge, "responses_stream", failing_stream)

    response = client.post("/v1/responses", json={"model": "gpt-fixture-a", "input": "hi", "stream": True})

    # Streaming failures are represented inside SSE, never as a leaked traceback.
    assert "secret-token" not in response.text


# --- 7. 401 envelope and JSON content type ----------------------------------

def test_bridge_api_key_401_uses_openai_envelope(monkeypatch):
    monkeypatch.setattr(settings, "bridge_api_key", "test-secret")

    response = client.get("/v1/models")

    error = assert_openai_error(response, 401, "invalid_api_key")
    assert error["message"] == "Incorrect API key provided"
    assert error["param"] is None
    assert response.json() == {
        "error": {
            "message": "Incorrect API key provided",
            "type": "invalid_request_error",
            "param": None,
            "code": "invalid_api_key",
        }
    }
    assert "test-secret" not in response.text

    authorized = client.get("/v1/models", headers={"Authorization": "Bearer test-secret"})
    assert authorized.status_code == 200


def test_error_responses_are_json_with_request_id():
    for response in (
        client.post("/v1/chat/completions", json={}),
        client.get("/v1/models/nope"),
        client.post("/v1/unknown-route", json={}),
    ):
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["x-request-id"]
