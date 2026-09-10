import asyncio
import json

import pytest

from bridge import ChatGPTBridge
from event_assembly import EventAssembly
from schemas import ChatCompletionRequest, ResponsesRequest


def call(i, args=""):
    return {"id": f"fc_{i}", "type": "function_call", "call_id": f"call_{i}", "name": f"f{i}", "arguments": args}


def terminal(output=None, kind="completed", **extra):
    return {"type": f"response.{kind}", "response": {"id": "resp_test", "status": kind, "output": output or [], **extra}}


def bridge_for(events):
    bridge = ChatGPTBridge()

    async def fake(*args):
        for event in events:
            yield event

    bridge._codex_event_stream = fake
    bridge._codex_event_stream_from_payload = fake
    return bridge


def request(legacy=False):
    return ChatCompletionRequest(model="gpt-fixture-a", messages=[{"role": "user", "content": "hi"}], **({"functions": [{"name": "f1"}]} if legacy else {}))


def streamed(bridge, legacy=False):
    async def collect():
        return [chunk async for chunk in bridge.chat_completion_stream(request(legacy))]
    lines = asyncio.run(collect())
    return [json.loads(line[6:]) for line in lines if "[DONE]" not in line], lines


def test_sparse_interleaved_arguments_and_duplicate_sequences():
    events = [
        {"type": "response.output_item.added", "output_index": 9, "item": call(1)},
        {"type": "response.output_item.added", "output_index": 3, "item": call(2)},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{", "sequence_number": 5},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_2", "delta": "{}"},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{", "sequence_number": 5},
        {"type": "response.function_call_arguments.done", "item_id": "fc_1", "arguments": "{}"}, terminal(),
    ]
    result, error = asyncio.run(bridge_for(events).chat_completion(request()))
    assert error is None
    assert [c["id"] for c in result["tool_calls"]] == ["call_2", "call_1"]
    chunks, lines = streamed(bridge_for(events))
    args = {}
    for chunk in chunks:
        for delta in chunk["choices"][0]["delta"].get("tool_calls", []):
            args[delta["index"]] = args.get(delta["index"], "") + delta["function"]["arguments"]
    assert args == {0: "{}", 1: "{}"}
    assert "[DONE]" in lines[-1]


@pytest.mark.parametrize("mode", ["done", "terminal"])
def test_done_and_terminal_only_arguments(mode):
    events = ([{"type": "response.output_item.done", "output_index": 12, "item": call(1, "{}")}, terminal()] if mode == "done" else [terminal([call(1, "{}")])])
    chunks, _ = streamed(bridge_for(events))
    deltas = [d for c in chunks for d in c["choices"][0]["delta"].get("tool_calls", [])]
    assert deltas == [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "f1", "arguments": "{}"}}]


def test_retains_order_metadata_and_fills_terminal_output():
    items = [{"type": "reasoning", "id": "r1", "summary": [{"type": "summary_text", "text": "thought"}], "encrypted_content": "opaque"}, {"type": "message", "id": "m1", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "hello", "annotations": [{"type": "url_citation", "url": "https://example.com"}]}]}, call(1, "{}")]
    events = [{"type": "response.output_item.done", "output_index": i * 3, "item": item} for i, item in enumerate(items)] + [terminal()]
    req = ResponsesRequest(model="gpt-fixture-a", input="hi")
    result, error = asyncio.run(bridge_for(events).responses(req))
    assert error is None
    assert result["output"] == items
    assert result["output_text"] == "hello"
    async def collect():
        return [line async for line in bridge_for(events).responses_stream(req)]
    last = asyncio.run(collect())[-1]
    assert json.loads(last.split("data: ")[1])["response"]["output"] == items


def test_text_delta_done_terminal_not_duplicated():
    item = {"id": "m1", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello", "annotations": []}]}
    events = [{"type": "response.output_text.delta", "item_id": "m1", "output_index": 5, "delta": "hel", "sequence_number": 1}] * 2 + [{"type": "response.output_text.done", "item_id": "m1", "output_index": 5, "text": "hello"}, terminal([item])]
    chunks, _ = streamed(bridge_for(events))
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "hello"


@pytest.mark.parametrize("events", [[], [{"type": "response.output_text.delta", "delta": "partial"}], [terminal(kind="failed", error={"message": "bad"})], [terminal(kind="incomplete", incomplete_details={"reason": "content_filter"})]])
def test_chat_failures_never_finish_success(events):
    result, error = asyncio.run(bridge_for(events).chat_completion(request()))
    assert result is None and error
    chunks, lines = streamed(bridge_for(events))
    assert "error" in chunks[-1]
    assert not any("[DONE]" in line for line in lines)


def test_length_and_responses_incomplete():
    events = [terminal(kind="incomplete", incomplete_details={"reason": "max_output_tokens"})]
    result, error = asyncio.run(bridge_for(events).chat_completion(request()))
    assert error is None and result["finish_reason"] == "length"
    chunks, _ = streamed(bridge_for(events))
    assert chunks[-1]["choices"][0]["finish_reason"] == "length"
    result, error = asyncio.run(bridge_for(events).responses(ResponsesRequest(model="gpt-fixture-a", input="hi")))
    assert error is None and result["status"] == "incomplete"


def test_no_fabricated_call_identity():
    item = call(1, "{}")
    del item["call_id"]
    result, error = asyncio.run(bridge_for([terminal([item])]).chat_completion(request()))
    assert result is None and error["code"] == "invalid_upstream_tool_call"


def test_legacy_function_call_and_reject_multiple():
    events = [terminal([call(1, "{}")])]
    result, error = asyncio.run(bridge_for(events).chat_completion(request(True)))
    assert error is None
    assert result["function_call"] == {"name": "f1", "arguments": "{}"}
    assert result["finish_reason"] == "function_call" and not result["tool_calls"]
    chunks, _ = streamed(bridge_for(events), True)
    assert any(c["choices"][0]["delta"].get("function_call") for c in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "function_call"
    chunks, lines = streamed(bridge_for([terminal([call(1), call(2)])]), True)
    assert chunks[-1]["error"]["code"] == "unsupported_multiple_legacy_calls"
    assert not any("[DONE]" in line for line in lines)


def test_responses_eof_and_failed_stream_are_not_completed():
    req = ResponsesRequest(model="gpt-fixture-a", input="hi")
    async def collect(events):
        return [line async for line in bridge_for(events).responses_stream(req)]
    lines = asyncio.run(collect([]))
    assert "event: error" in lines[-1] and "upstream_stream_truncated" in lines[-1]
    lines = asyncio.run(collect([terminal(kind="failed", error={"message": "failed"})]))
    assert "event: response.failed" in lines[-1]
    assert "response.completed" not in "".join(lines)


def test_reasoning_summary_delta_done_and_annotations():
    state = EventAssembly()
    state.feed({"type": "response.reasoning_summary_text.delta", "item_id": "r", "output_index": 2, "delta": "think"})
    state.feed({"type": "response.reasoning_summary_text.done", "item_id": "r", "text": "thinking"})
    state.feed({"type": "response.output_text.delta", "item_id": "m", "output_index": 8, "delta": "answer"})
    annotation = {"type": "url_citation", "url": "https://example.com"}
    state.feed({"type": "response.output_text.annotation.added", "item_id": "m", "annotation": annotation})
    state.feed(terminal())
    assert state.output()[0]["summary"][0]["text"] == "thinking"
    assert state.output()[1]["content"][0]["annotations"] == [annotation]


@pytest.mark.parametrize("index", [-1, 1000000000, "0", None, True])
def test_invalid_content_index_is_rejected(index):
    state = EventAssembly()
    state.feed({"type": "response.output_text.delta", "content_index": index, "delta": "x"})
    assert state.error["code"] == "invalid_upstream_event"


@pytest.mark.parametrize("events", [[{"type": "response.output_text.delta", "content_index": -1, "delta": "x"}], [terminal([{"type": "function_call", "id": "fc", "name": "f", "arguments": "{}"}])]])
def test_responses_invalid_events_emit_errors(events):
    req = ResponsesRequest(model="gpt-fixture-a", input="hi")
    result, error = asyncio.run(bridge_for(events).responses(req))
    assert result is None and error
    async def collect():
        return [line async for line in bridge_for(events).responses_stream(req)]
    lines = asyncio.run(collect())
    assert lines[-1].startswith("event: error")
    assert "response.completed" not in "".join(lines)


def test_refusal_is_preserved_in_responses_and_explicit_chat_error():
    events = [{"type": "response.refusal.delta", "item_id": "m", "delta": "No"}, {"type": "response.refusal.done", "item_id": "m", "refusal": "No thanks"}, terminal()]
    result, error = asyncio.run(bridge_for(events).responses(ResponsesRequest(model="gpt-fixture-a", input="hi")))
    assert error is None
    assert result["output"][0]["content"][0] == {"type": "refusal", "refusal": "No thanks"}
    result, error = asyncio.run(bridge_for(events).chat_completion(request()))
    assert result is None and error["code"] == "upstream_refusal"


def test_argument_before_identity_is_buffered():
    state = EventAssembly()
    state.feed({"type": "response.function_call_arguments.delta", "output_index": 8, "delta": "{}"})
    assert state.calls() == []
    state.feed({"type": "response.output_item.added", "output_index": 8, "item": call(1)})
    assert state.calls()[0]["function"]["arguments"] == "{}"
