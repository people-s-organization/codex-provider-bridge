from copy import deepcopy

import pytest

from bridge import ChatGPTBridge
from schemas import ChatCompletionRequest, CompletionRequest, ResponsesRequest, chat_history_items


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


def test_server_state_is_served_by_the_bridge_owned_store():
    # store and previous_response_id now work through the bridge's own bounded
    # in-memory store; an unknown previous id becomes a 404 at request time instead.
    assert chat(store=True).store is True
    assert ResponsesRequest(model="gpt-fixture-a", input="hi").store is True
    request = ResponsesRequest(model="gpt-fixture-a", input="hi", previous_response_id="resp_old")
    assert request.previous_response_id == "resp_old"


def test_blank_previous_response_id_is_rejected():
    with pytest.raises(ValueError):
        ResponsesRequest(model="gpt-fixture-a", input="hi", previous_response_id="   ")


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
    # Instruction turns keep their order and role by becoming instructions; the backend
    # rejects system role items in input, and instructions is a plain string upstream.
    assert first["instructions"] == "system rules\n\ndeveloper rules"
    assert [item.get("role") for item in first["input"]] == [None, None]
    call, result = first["input"]
    assert call["type"] == "function_call" and result["type"] == "function_call_output"
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
    assert payload["instructions"] == "rules"
    assert payload["input"] == original[1:]
    payload["input"][1]["content"][0]["annotations"].append({"type": "mutated"})
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


def test_history_tool_calls_may_omit_the_constant_type():
    request = ChatCompletionRequest(
        model="gpt-fixture-a",
        messages=[
            {"role": "user", "content": "list"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "function": {"name": "run", "arguments": '{"x":1}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ],
    )
    items = chat_history_items(request.messages)
    assert [item["type"] for item in items] == ["message", "function_call", "function_call_output"]
    assert items[1]["name"] == "run"


def test_explicit_non_function_history_call_type_is_rejected():
    with pytest.raises(ValueError):
        ChatCompletionRequest(
            model="gpt-fixture-a",
            messages=[
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "web_search", "function": {"name": "run", "arguments": "{}"}}]},
            ],
        )


def test_responses_input_never_forwards_system_or_developer_roles():
    payload = ChatGPTBridge()._build_responses_payload(
        ResponsesRequest(
            model="gpt-fixture-a",
            instructions="explicit rules",
            input=[
                {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "rules"}]},
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
        )
    )
    # Upstream answers a system role inside input with 400 "System messages are not allowed".
    assert payload["instructions"] == "explicit rules\n\nrules\n\ndev"
    assert payload["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


def test_chat_store_is_reported_as_ignored_and_responses_store_is_honoured():
    assert "store is ignored by the Codex bridge for chat completions" in chat(store=True).compatibility_warnings()
    assert "store is ignored by the Codex bridge for chat completions" not in chat(store=False).compatibility_warnings()
    # Responses storage is bridge-owned, so it must never be reported as ignored.
    assert ResponsesRequest(model="gpt-fixture-a", input="hi", store=True).compatibility_warnings() == []
