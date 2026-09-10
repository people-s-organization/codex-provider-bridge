import json

from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def _tool_events(call_id="call_1", item_id="fc_1", name="get_weather", chunks=('{"city":', '"Paris"}')):
    arguments = "".join(chunks)
    yield {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "id": item_id,
            "type": "function_call",
            "status": "in_progress",
            "arguments": "",
            "call_id": call_id,
            "name": name,
        },
    }
    for chunk in chunks:
        yield {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "output_index": 0,
            "delta": chunk,
        }
    yield {
        "type": "response.function_call_arguments.done",
        "item_id": item_id,
        "output_index": 0,
        "arguments": arguments,
    }
    yield {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "id": item_id,
            "type": "function_call",
            "status": "completed",
            "arguments": arguments,
            "call_id": call_id,
            "name": name,
        },
    }


def _completed():
    return {
        "type": "response.completed",
        "response": {
            "id": "resp_test",
            "model": "gpt-5.5",
            "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        },
    }


def _patch_stream(monkeypatch, events, captured=None):
    async def _fake_codex_event_stream_from_payload(payload):
        if captured is not None:
            captured.append(payload)
        for event in events:
            yield event

    monkeypatch.setattr(
        "main.bridge._codex_event_stream_from_payload",
        _fake_codex_event_stream_from_payload,
    )


def test_chat_tool_definitions_are_forwarded(monkeypatch):
    captured = []
    events = [{"type": "response.created", "response": {"id": "resp_test", "created_at": 1}}, *_tool_events(), _completed()]
    _patch_stream(monkeypatch, events, captured)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
            "parallel_tool_calls": False,
        },
    )

    assert response.status_code == 200
    payload = captured[0]
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    assert payload["tool_choice"] == {"type": "function", "name": "get_weather"}
    assert payload["parallel_tool_calls"] is False
    assert "Compatibility note" not in payload["instructions"]


def test_chat_non_stream_returns_tool_calls(monkeypatch):
    _patch_stream(monkeypatch, [*_tool_events(), _completed()])

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "weather?"}]},
    )

    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
        }
    ]


def test_chat_stream_emits_tool_call_deltas(monkeypatch):
    _patch_stream(monkeypatch, [*_tool_events(), _completed()])

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "weather?"}], "stream": True},
    )

    assert response.status_code == 200
    lines = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(line) for line in lines[:-1]]

    tool_deltas = [
        delta
        for chunk in chunks
        for delta in (chunk["choices"][0]["delta"].get("tool_calls") or [])
    ]
    assert tool_deltas[0] == {
        "index": 0,
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": ""},
    }
    assert [delta["function"]["arguments"] for delta in tool_deltas[1:]] == ['{"city":', '"Paris"}']
    assert all(delta["index"] == 0 for delta in tool_deltas)
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_parallel_tool_calls_get_distinct_indexes(monkeypatch):
    events = [
        *_tool_events(call_id="call_a", item_id="fc_a", name="get_weather", chunks=("{}",)),
        *_tool_events(call_id="call_b", item_id="fc_b", name="get_time", chunks=("{}",)),
        _completed(),
    ]
    _patch_stream(monkeypatch, events)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "both"}]},
    )

    tool_calls = response.json()["choices"][0]["message"]["tool_calls"]
    assert [call["id"] for call in tool_calls] == ["call_a", "call_b"]
    assert [call["function"]["name"] for call in tool_calls] == ["get_weather", "get_time"]


def test_responses_non_stream_returns_function_call_items(monkeypatch):
    _patch_stream(monkeypatch, [*_tool_events(), _completed()])

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-5.5", "input": "weather?"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["output_text"] == ""
    assert data["output"] == [
        {
            "id": "fc_1",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city":"Paris"}',
        }
    ]


def test_responses_stream_passes_function_call_events(monkeypatch):
    _patch_stream(monkeypatch, [*_tool_events(), _completed()])

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-5.5", "input": "weather?", "stream": True},
    )

    assert response.status_code == 200
    assert "event: response.function_call_arguments.delta" in response.text
    assert '"type": "function_call"' in response.text


def test_non_function_tools_are_rejected_before_upstream(monkeypatch):
    captured = []
    _patch_stream(monkeypatch, [*_tool_events(), _completed()], captured)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "search"}],
            "tools": [{"type": "web_search"}],
        },
    )

    assert response.status_code in {400, 422}
    assert "function tools" in response.text
    assert captured == []


def test_legacy_functions_are_forwarded(monkeypatch):
    captured = []
    _patch_stream(monkeypatch, [*_tool_events(), _completed()], captured)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "weather?"}],
            "functions": [{"name": "get_weather", "description": "Get weather"}],
            "function_call": "auto",
        },
    )

    assert response.status_code == 200
    payload = captured[0]
    assert payload["tools"] == [
        {"type": "function", "name": "get_weather", "description": "Get weather"}
    ]
    assert payload["tool_choice"] == "auto"


def test_tool_result_round_trip_replays_call_and_output(monkeypatch):
    captured = []
    _patch_stream(
        monkeypatch,
        [
            {"type": "response.output_text.delta", "delta": "18C"},
            _completed(),
        ],
        captured,
    )

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "18C and sunny"},
            ],
        },
    )

    assert response.status_code == 200
    payload = captured[0]
    assert payload["input"][1] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "get_weather",
        "arguments": '{"city":"Paris"}',
    }
    assert payload["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "18C and sunny",
    }
