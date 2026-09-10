from copy import deepcopy

import pytest

from bridge import ChatGPTBridge
from schemas import ChatCompletionRequest, CompletionRequest, ResponsesRequest


def chat(**kwargs):
    return ChatCompletionRequest(model="gpt-fixture-a", messages=[{"role": "user", "content": "hello"}], **kwargs)


@pytest.mark.parametrize("tool", [
    {}, {"type": "web_search"}, {"type": "code_interpreter"},
    {"type": "function", "function": None},
    {"type": "function", "name": "bad name"},
    {"type": "function", "name": "x" * 65},
    {"type": "function", "name": "ok", "strict": "true"},
    {"type": "function", "name": "ok", "description": 7},
    {"type": "function", "name": "ok", "parameters": []},
    {"type": "function", "name": "ok", "parameters": {"type": "array"}},
    {"type": "function", "name": "ok", "parameters": {"properties": []}},
    {"type": "function", "name": "ok", "parameters": {"required": "a"}},
    {"type": "function", "name": "ok", "parameters": {"properties": {"x": {"type": "bogus"}}}},
])
def test_invalid_tools_rejected(tool):
    with pytest.raises(ValueError):
        chat(tools=[tool])
    with pytest.raises(ValueError):
        ResponsesRequest(model="gpt-fixture-a", input="hi", tools=[tool])


@pytest.mark.parametrize("choice", ["bogus", "AUTO", {}, {"name": "ok"}, {"type": "web_search"}, {"type": "function", "name": "missing"}, {"type": "function", "function": {"name": "ok", "extra": 1}}])
def test_invalid_choices_rejected(choice):
    with pytest.raises(ValueError):
        chat(tools=[{"type": "function", "name": "ok"}], tool_choice=choice)


def test_tool_cross_validation():
    tool = {"type": "function", "name": "ok"}
    for kwargs in [dict(tools=[tool, tool]), dict(tool_choice="required"), dict(tool_choice={"type": "function", "name": "ok"}), dict(tools=[tool], functions=[]), dict(tool_choice="auto", function_call="auto")]:
        with pytest.raises(ValueError):
            chat(**kwargs)
    assert chat(tool_choice="none").tool_choice == "none"


@pytest.mark.parametrize("kwargs", [{"store": True}, {"previous_response_id": "resp_old"}])
def test_server_state_unavailable(kwargs):
    with pytest.raises(ValueError):
        chat(**kwargs)
    with pytest.raises(ValueError):
        ResponsesRequest(model="gpt-fixture-a", input="hi", **kwargs)


def test_explicit_only_compatibility_warnings_and_strict(monkeypatch):
    monkeypatch.delenv("BRIDGE_STRICT_COMPATIBILITY", raising=False)
    assert chat().compatibility_warnings() == []
    req = chat(max_tokens=10, temperature=1, seed=0)
    assert req.compatibility_warnings() == ["max_tokens is ignored by the Codex bridge", "seed is ignored by the Codex bridge", "temperature is ignored by the Codex bridge"]
    assert CompletionRequest(model="gpt-fixture-a", prompt="hi").compatibility_warnings() == []
    assert len(ResponsesRequest(model="gpt-fixture-a", input="hi", max_output_tokens=2, truncation="auto").compatibility_warnings()) == 2
    monkeypatch.setenv("BRIDGE_STRICT_COMPATIBILITY", "true")
    with pytest.raises(ValueError, match="temperature is ignored"):
        chat(temperature=1)
    chat()


def test_legacy_replay_is_deterministic_and_internal_only():
    messages = [
        {"role": "system", "content": "system rules", "metadata": {"x": 1}},
        {"role": "developer", "content": "developer rules"},
        {"role": "assistant", "function_call": {"name": "lookup", "arguments": "{}"}},
        {"role": "function", "name": "lookup", "content": "answer"},
    ]
    original = deepcopy(messages)
    request = ChatCompletionRequest(model="gpt-fixture-a", messages=messages)
    snapshot = request.model_dump()
    bridge = ChatGPTBridge()
    first = bridge._build_payload(request)
    assert first == bridge._build_payload(request)
    assert messages == original and request.model_dump() == snapshot
    assert [i.get("role") for i in first["input"][:2]] == ["system", "developer"]
    assert first["input"][0]["metadata"] == {"x": 1}
    call, result = first["input"][2:]
    assert call["call_id"] == result["call_id"]
    assert "call_id" not in messages[2]["function_call"]


def test_responses_preserve_metadata_reasoning_and_immutability():
    items = [
        {"type": "message", "role": "system", "content": "rules", "metadata": {"key": [1]}},
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque", "summary": []},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello", "annotations": [{"type": "test"}]}]},
    ]
    original = deepcopy(items)
    request = ResponsesRequest(model="gpt-fixture-a", input=items)
    payload = ChatGPTBridge()._build_responses_payload(request)
    assert payload["input"][1:] == original[1:]
    payload["input"][0]["metadata"]["key"].append(2)
    assert request.input == original and items == original


@pytest.mark.parametrize("items", [
    [{"type": "function_call_output", "call_id": "x", "output": "42"}],
    [{"type": "function_call", "call_id": "x", "name": "ok", "arguments": "{}"}],
    [{"type": "function_call", "call_id": "x", "name": "ok", "arguments": "[]"}],
    [{"type": "function_call", "call_id": "x", "arguments": "{}"}],
])
def test_invalid_responses_replay(items):
    with pytest.raises(ValueError):
        ResponsesRequest(model="gpt-fixture-a", input=items)
    with pytest.raises(ValueError):
        ChatGPTBridge()._normalize_responses_input_items(items)


def test_duplicate_results_and_call_ids_rejected():
    call = {"type": "function_call", "call_id": "x", "name": "ok", "arguments": "{}"}
    output = {"type": "function_call_output", "call_id": "x", "output": ""}
    for items in ([call, output, output], [call, call, output], [output, call]):
        with pytest.raises(ValueError):
            ResponsesRequest(model="gpt-fixture-a", input=items)
