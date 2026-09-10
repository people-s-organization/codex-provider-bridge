"""The bridge-owned Responses store: store, previous_response_id chaining, TTL/LRU."""
import asyncio
import time

import pytest

from bridge import ChatGPTBridge
from response_store import ResponseStore
from schemas import ResponsesRequest


def completed_response(response_id="resp_store_1", text="hello"):
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "status": "completed",
            "output": [
                {
                    "id": f"msg_{response_id}",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    }


def bridge_with_events(events, captured):
    bridge = ChatGPTBridge()

    async def fake(payload):
        captured.append(payload)
        for event in events:
            yield event

    bridge._codex_event_stream_from_payload = fake
    return bridge


def turn(bridge, **kwargs):
    return asyncio.run(bridge.responses(ResponsesRequest(model="gpt-fixture-a", input="hi", **kwargs)))


def test_completed_response_is_stored_and_retrievable(monkeypatch):
    import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "response_store", ResponseStore(max_entries=8, ttl_seconds=60))
    captured = []
    bridge = bridge_with_events([completed_response()], captured)

    result, error = turn(bridge)
    assert error is None
    assert result["id"] == "resp_store_1"
    assert result["store"] is True

    stored, get_error = asyncio.run(bridge.get_stored_response("resp_store_1"))
    assert get_error is None and stored["id"] == "resp_store_1"

    deleted, delete_error = asyncio.run(bridge.delete_stored_response("resp_store_1"))
    assert delete_error is None and deleted == {"id": "resp_store_1", "object": "response.deleted", "deleted": True}
    missing, missing_error = asyncio.run(bridge.get_stored_response("resp_store_1"))
    assert missing is None
    assert missing_error["status"] == 404 and missing_error["code"] == "response_not_found"
    assert missing_error["local"] is True


def test_store_false_opts_out(monkeypatch):
    import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "response_store", ResponseStore(max_entries=8, ttl_seconds=60))
    bridge = bridge_with_events([completed_response()], [])
    result, error = turn(bridge, store=False)
    assert error is None
    assert "store" not in result or result["store"] is not True
    assert asyncio.run(bridge.get_stored_response("resp_store_1"))[0] is None


def test_previous_response_id_replays_stored_conversation(monkeypatch):
    import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "response_store", ResponseStore(max_entries=8, ttl_seconds=60))
    captured = []
    bridge = bridge_with_events([completed_response()], captured)
    result, error = turn(bridge)
    assert error is None
    assert [item["role"] for item in captured[0]["input"]] == ["user"]

    captured.clear()
    bridge = bridge_with_events([completed_response("resp_store_2", "again")], captured)
    second, second_error = turn(bridge, previous_response_id="resp_store_1")
    assert second_error is None
    assert second["id"] == "resp_store_2"

    replayed = captured[0]["input"]
    # The stored user turn and the stored assistant answer precede the new user turn.
    assert [item.get("role") for item in replayed] == ["user", "assistant", "user"]
    assert replayed[0]["content"][0]["text"] == "hi"
    assert replayed[1]["content"][0]["text"] == "hello"
    assert replayed[2]["content"][0]["text"] == "hi"
    assert captured[0]["store"] is False  # the bridge never trusts upstream storage


def test_unknown_previous_response_id_is_a_404(monkeypatch):
    import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "response_store", ResponseStore(max_entries=8, ttl_seconds=60))
    bridge = bridge_with_events([completed_response()], [])
    result, error = turn(bridge, previous_response_id="resp_missing")
    assert result is None
    assert error["status"] == 404
    assert error["code"] == "previous_response_not_found"
    assert error["param"] == "previous_response_id"
    assert error["local"] is True


def test_streaming_completion_is_stored(monkeypatch):
    import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "response_store", ResponseStore(max_entries=8, ttl_seconds=60))
    bridge = bridge_with_events(
        [
            {"type": "response.output_text.delta", "delta": "hi"},
            completed_response("resp_stream_store"),
        ],
        [],
    )

    async def collect():
        return [chunk async for chunk in bridge.responses_stream(ResponsesRequest(model="gpt-fixture-a", input="hi"))]

    chunks = asyncio.run(collect())
    assert any("response.completed" in chunk for chunk in chunks)
    stored, error = asyncio.run(bridge.get_stored_response("resp_stream_store"))
    assert error is None and stored["store"] is True


def test_store_evicts_least_recently_used_and_expires():
    store = ResponseStore(max_entries=2, ttl_seconds=60)
    store.store({"id": "a"}, [])
    store.store({"id": "b"}, [])
    assert store.get("a") is not None
    store.store({"id": "c"}, [])
    assert store.get("b") is None  # b was least recently used
    assert store.get("a") is not None and store.get("c") is not None

    expiring = ResponseStore(max_entries=4, ttl_seconds=0.05)
    expiring.store({"id": "short"}, [])
    time.sleep(0.06)
    assert expiring.get("short") is None


def test_store_requires_an_id_and_is_isolated_per_instance():
    store = ResponseStore(max_entries=2, ttl_seconds=60)
    assert store.store({"id": ""}, []) is None
    store.store({"id": "x"}, [{"type": "message"}])
    assert store.conversation("x") == [{"type": "message"}]
    assert ResponseStore(max_entries=2, ttl_seconds=60).get("x") is None


@pytest.mark.parametrize("configuration", [{"max_entries": 0}, {"ttl_seconds": -1}])
def test_invalid_store_configuration_falls_back_to_defaults(configuration):
    store = ResponseStore(**configuration)
    store.store({"id": "ok"}, [])
    assert store.get("ok") is not None


def test_chaining_can_omit_input_entirely(monkeypatch):
    import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "response_store", ResponseStore(max_entries=8, ttl_seconds=60))
    captured = []
    bridge = bridge_with_events([completed_response()], captured)
    first, first_error = turn(bridge)
    assert first_error is None and first["id"] == "resp_store_1"

    captured.clear()
    bridge = bridge_with_events([completed_response("resp_store_3", "sure")], captured)
    second, second_error = asyncio.run(
        bridge.responses(ResponsesRequest(model="gpt-fixture-a", previous_response_id="resp_store_1"))
    )
    assert second_error is None and second["id"] == "resp_store_3"
    # The new turn adds nothing, so the replayed conversation is just the stored one.
    assert [item.get("role") for item in captured[0]["input"]] == ["user", "assistant"]


def tool_call_response():
    event = completed_response("resp_tool")
    event["response"]["output"] = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup", "arguments": '{}'},
    ]
    return event


@pytest.mark.parametrize("stream", [False, True])
def test_tool_result_only_continuation_replays_and_stores_complete_history(monkeypatch, stream):
    import bridge as bridge_module

    store = ResponseStore()
    monkeypatch.setattr(bridge_module, "response_store", store)
    bridge = bridge_with_events([tool_call_response()], [])
    assert turn(bridge)[1] is None
    captured = []
    bridge = bridge_with_events([completed_response("resp_result")], captured)
    output = {"type": "function_call_output", "call_id": "call_1", "output": "found"}
    request = ResponsesRequest(model="gpt-fixture-a", previous_response_id="resp_tool", input=[output])

    async def collect():
        return [chunk async for chunk in bridge.responses_stream(request)]

    if stream:
        assert any("response.completed" in chunk for chunk in asyncio.run(collect()))
    else:
        assert asyncio.run(bridge.responses(request))[1] is None
    replay = captured[0]["input"]
    assert [item["type"] for item in replay] == ["message", "reasoning", "function_call", "function_call_output"]
    assert replay[-1] == output
    assert replay[1]["encrypted_content"] == "opaque"
    assert store.conversation("resp_result")[:-1] == replay

    # A third turn must retain the matched call/result pair, not silently drop the result.
    captured.clear()
    bridge = bridge_with_events([completed_response("resp_third")], captured)
    assert turn(bridge, previous_response_id="resp_result")[1] is None
    assert captured[0]["input"][:4] == replay


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("previous_id, expected_status", [("resp_tool", 400), ("resp_missing", 404)])
def test_tool_continuation_rejects_unmatched_result_or_unknown_prior(monkeypatch, stream, previous_id, expected_status):
    import json
    import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "response_store", ResponseStore())
    assert turn(bridge_with_events([tool_call_response()], []))[1] is None
    captured = []
    bridge = bridge_with_events([completed_response()], captured)
    request = ResponsesRequest(
        model="gpt-fixture-a", previous_response_id=previous_id,
        input=[{"type": "function_call_output", "call_id": "wrong", "output": "found"}],
    )

    async def collect():
        return [chunk async for chunk in bridge.responses_stream(request)]

    if stream:
        chunks = asyncio.run(collect())
        assert len(chunks) == 1
        error = json.loads(chunks[0].split("data: ", 1)[1])
    else:
        result, error = asyncio.run(bridge.responses(request))
        assert result is None
    assert error["status"] == expected_status
    assert error["local"] is True
    if expected_status == 404:
        assert error["code"] == "previous_response_not_found"
    assert captured == []


@pytest.mark.parametrize("items", [
    [{"type": "function_call_output", "call_id": "call_1"}],
    [{"type": "function_call_output", "call_id": "", "output": "x"}],
    [{"type": "function_call_output", "call_id": "call_1", "output": 123}],
    [{"type": "function_call_output", "call_id": "call_1", "output": "x"}] * 2,
    [{"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "not json"}],
    [{"type": "message", "role": "invalid", "content": "x"}],
])
def test_chained_input_still_rejects_malformed_items(items):
    with pytest.raises(ValueError):
        ResponsesRequest(model="gpt-fixture-a", previous_response_id="resp_tool", input=items)


def test_standalone_tool_result_remains_invalid():
    with pytest.raises(ValueError, match="orphan"):
        ResponsesRequest(model="gpt-fixture-a", input=[
            {"type": "function_call_output", "call_id": "call_1", "output": "found"}
        ])


def test_chaining_cannot_leave_stored_tool_call_unanswered(monkeypatch):
    import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "response_store", ResponseStore())
    assert turn(bridge_with_events([tool_call_response()], []))[1] is None
    captured = []
    result, error = turn(bridge_with_events([completed_response()], captured), previous_response_id="resp_tool")
    assert result is None and error["status"] == 400
    assert captured == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("previous_id, status", [("resp_missing", 404), ("resp_tool", 400)])
def test_tool_continuation_http_errors_precede_stream_headers(monkeypatch, stream, previous_id, status):
    import bridge as bridge_module
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(bridge_module, "response_store", ResponseStore())
    assert turn(bridge_with_events([tool_call_response()], []))[1] is None
    captured = []
    monkeypatch.setattr(main, "bridge", bridge_with_events([completed_response()], captured))
    response = TestClient(main.app).post("/v1/responses", json={
        "model": "gpt-fixture-a", "previous_response_id": previous_id, "stream": stream,
        "input": [{"type": "function_call_output", "call_id": "wrong", "output": "x"}],
    })
    assert response.status_code == status
    assert response.json()["error"]["param"] == ("previous_response_id" if status == 404 else "input")
    assert captured == []


@pytest.mark.parametrize("stream", [False, True])
def test_chaining_retains_snapshot_when_prior_deleted_during_generation(monkeypatch, stream):
    import bridge as bridge_module

    store = ResponseStore()
    monkeypatch.setattr(bridge_module, "response_store", store)
    assert turn(bridge_with_events([tool_call_response()], []))[1] is None
    bridge = ChatGPTBridge()

    async def fake(payload):
        store.delete("resp_tool")
        yield completed_response("resp_after_delete")

    bridge._codex_event_stream_from_payload = fake
    request = ResponsesRequest(model="gpt-fixture-a", previous_response_id="resp_tool", input=[
        {"type": "function_call_output", "call_id": "call_1", "output": "found"}
    ])

    async def collect():
        return [chunk async for chunk in bridge.responses_stream(request)]

    if stream:
        assert any("response.completed" in chunk for chunk in asyncio.run(collect()))
    else:
        assert asyncio.run(bridge.responses(request))[1] is None
    assert [item["type"] for item in store.conversation("resp_after_delete")] == [
        "message", "reasoning", "function_call", "function_call_output", "message"
    ]


def test_store_isolates_nested_values_on_write_and_read():
    store = ResponseStore()
    response = completed_response()["response"]
    conversation = [{"type": "message", "content": [{"text": "original"}]}]
    store.store(response, conversation)
    response["output"][0]["content"][0]["text"] = "mutated"
    conversation[0]["content"][0]["text"] = "mutated"
    fetched = store.get("resp_store_1")
    assert fetched["output"][0]["content"][0]["text"] == "hello"
    fetched["output"].clear()
    replay = store.conversation("resp_store_1")
    assert replay[0]["content"][0]["text"] == "original"
    replay[0]["content"].clear()
    assert store.get("resp_store_1")["output"]
    assert store.conversation("resp_store_1")[0]["content"]


def test_input_is_required_without_a_previous_response():
    import pytest as pytest_module

    with pytest_module.raises(ValueError):
        ResponsesRequest(model="gpt-fixture-a")
