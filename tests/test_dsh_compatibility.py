"""Wire-format regression tests for OpenAI-compatible bridge clients."""

import pytest

from bridge import ChatGPTBridge
from schemas import ChatCompletionRequest, ResponsesRequest


@pytest.fixture
def bridge(monkeypatch):
    instance = ChatGPTBridge()
    # Payload checks must not discover models or contact an upstream service.
    monkeypatch.setattr(instance, "_resolve_model_name", lambda model: model)
    return instance


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "extra high", "ultra", "max", "future_effort-v2"])
@pytest.mark.parametrize("field", ["reasoning", "reasoning_effort"])
def test_reasoning_forwarded_by_both_apis(bridge, effort, field):
    kwargs = {field: {"effort": effort} if field == "reasoning" else effort}
    expected = {"effort": "xhigh" if effort == "extra high" else effort}
    chat = ChatCompletionRequest(model="fixture", messages=[{"role": "user", "content": "Hi"}], **kwargs)
    responses = ResponsesRequest(model="fixture", input="Hi", **kwargs)
    assert bridge._build_payload(chat)["reasoning"] == expected
    assert bridge._build_responses_payload(responses)["reasoning"] == expected


def test_responses_native_effort_takes_precedence(bridge):
    request = ResponsesRequest(model="fixture", input="Hi", reasoning={"effort": "low"}, reasoning_effort="high")
    snapshot = request.model_dump()
    assert bridge._build_responses_payload(request)["reasoning"] == {"effort": "low"}
    assert request.model_dump() == snapshot


@pytest.mark.parametrize("effort", [42, True, {}, []])
@pytest.mark.parametrize("field", ["reasoning", "reasoning_effort"])
def test_effort_must_be_a_string(effort, field):
    kwargs = {field: {"effort": effort} if field == "reasoning" else effort}
    with pytest.raises(ValueError):
        ResponsesRequest(model="fixture", input="Hi", **kwargs)
    with pytest.raises(ValueError):
        ChatCompletionRequest(model="fixture", messages=[], **kwargs)


@pytest.mark.parametrize("effort", [None, "", "  "])
def test_empty_effort_is_omitted(bridge, effort):
    request = ResponsesRequest(model="fixture", input="Hi", reasoning_effort=effort)
    assert "reasoning" not in bridge._build_responses_payload(request)


def test_custom_effort_spelling_is_preserved(bridge):
    request = ResponsesRequest(model="fixture", input="Hi", reasoning={"effort": "  Vendor_Ultra-v2  "})
    assert bridge._build_responses_payload(request)["reasoning"] == {"effort": "Vendor_Ultra-v2"}


def test_responses_tool_image_is_preserved(bridge):
    output = [
        {"type": "input_text", "text": "Screenshot"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAA", "detail": "auto"},
    ]
    request = ResponsesRequest(model="fixture", input=[
        {"type": "function_call", "call_id": "call_image", "name": "read_image", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_image", "output": output},
    ])
    assert bridge._build_responses_payload(request)["input"][1]["output"] == output


@pytest.mark.parametrize("effort,expected", [("extra high", "xhigh"), ("ultra", "ultra")])
def test_responses_http_alias_reaches_payload(monkeypatch, bridge, effort, expected):
    from fastapi.testclient import TestClient
    from main import app

    captured = []

    async def fake_responses(request):
        captured.append(bridge._build_responses_payload(request))
        return {"id": "resp_fixture", "object": "response", "status": "completed", "output": []}, None

    monkeypatch.setattr("main.bridge.responses", fake_responses)
    with TestClient(app) as client:
        response = client.post("/v1/responses", json={
            "model": "fixture", "input": "Hi", "reasoning_effort": effort,
        })
    assert response.status_code == 200
    assert captured[0]["reasoning"] == {"effort": expected}


@pytest.mark.parametrize("url", ["https://example.com/test.png", "data:image/png;base64,AAA"])
def test_image_and_effort_together(bridge, url):
    chat = ChatCompletionRequest(model="fixture", reasoning_effort="high", messages=[{
        "role": "user", "content": [
            {"type": "text", "text": "Describe this"},
            {"type": "image_url", "image_url": {"url": url, "detail": "auto"}},
        ],
    }])
    expected = [
        {"type": "input_text", "text": "Describe this"},
        {"type": "input_image", "image_url": url, "detail": "auto"},
    ]
    responses = ResponsesRequest(model="fixture", reasoning={"effort": "high"}, input=[{
        "role": "user", "content": expected,
    }])
    for payload in (bridge._build_payload(chat), bridge._build_responses_payload(responses)):
        assert payload["input"][0]["content"] == expected
        assert payload["reasoning"] == {"effort": "high"}
