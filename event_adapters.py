"""Chat and Responses projections of the shared upstream event state."""
import json
import time
import uuid

from event_assembly import EventAssembly


async def chat_completion(bridge, request):
    state = EventAssembly()
    legacy = bool(request.functions) and not request.tools
    async for event in bridge._codex_event_stream(request):
        if not state.feed(event):
            continue
        error = state.chat_error(legacy)
        if error:
            return None, error
        if state.terminal:
            response = state.response
            calls = state.calls()
            result = {
                "id": response.get("id", f"chatcmpl-{uuid.uuid4()}"),
                "created": response.get("created_at", int(time.time())),
                "model": request.model,
                "content": state.text(),
                "tool_calls": [] if legacy else calls,
                "finish_reason": state.finish_reason(legacy),
                "usage": bridge._usage_from_response(response),
            }
            if legacy and calls:
                result["function_call"] = calls[0]["function"]
            return result, None
    return None, state.failure("Upstream stream ended before a terminal response", "upstream_stream_truncated")


async def chat_completion_stream(bridge, request):
    state = EventAssembly()
    legacy = bool(request.functions) and not request.tools
    response_id = f"chatcmpl-{uuid.uuid4()}"
    created = int(time.time())
    include_usage = bool((request.stream_options or {}).get("include_usage"))
    sent_role = False
    sent_text = {}
    sent_calls = {}

    def chunk(delta, finish_reason=None):
        return "data: " + json.dumps(bridge._chat_stream_chunk(
            response_id=response_id, created=created, model=request.model,
            delta=delta, finish_reason=finish_reason, include_usage=include_usage)) + "\n\n"

    async for event in bridge._codex_event_stream(request):
        if not state.feed(event):
            continue
        response_id = state.response.get("id", response_id)
        created = state.response.get("created_at", created)
        error = state.chat_error(legacy)
        if error:
            yield f"data: {json.dumps({'error': error})}\n\n"
            return
        # Legacy has no representation for multiple calls: defer calls until terminal.
        deltas = []
        for item_index, item in enumerate(state.output()):
            if item.get("type") != "message":
                continue
            identity = item.get("id") or item_index
            for part_index, part in enumerate(item.get("content", [])):
                if part.get("type") != "output_text":
                    continue
                key = (identity, part_index)
                text = part.get("text", "")
                if key not in sent_text and item.get("id") and (item_index, part_index) in sent_text:
                    sent_text[key] = sent_text.pop((item_index, part_index))
                previous_text = sent_text.get(key, "")
                if not text.startswith(previous_text):
                    error = state.failure("Upstream text snapshot contradicts emitted deltas", "inconsistent_upstream_output")
                    yield f"data: {json.dumps({'error': error})}\n\n"
                    return
                if text != previous_text:
                    deltas.append({"content": text[len(previous_text):]})
                    sent_text[key] = text
        if not legacy or state.terminal:
            for call in state.calls():
                identity = call["id"]
                function = call["function"]
                previous = sent_calls.get(identity)
                if previous is None:
                    index = len(sent_calls)
                    sent_calls[identity] = (index, function["arguments"], function["name"])
                    if legacy:
                        deltas.append({"function_call": function})
                    else:
                        deltas.append({"tool_calls": [{"index": index, **call}]})
                else:
                    index, arguments, name = previous
                    if name != function["name"] or not function["arguments"].startswith(arguments):
                        error = state.failure("Upstream tool snapshot contradicts emitted deltas", "inconsistent_upstream_output")
                        yield f"data: {json.dumps({'error': error})}\n\n"
                        return
                    suffix = function["arguments"][len(arguments):]
                    if suffix:
                        deltas.append({"tool_calls": [{"index": index, "function": {"arguments": suffix}}]})
                        sent_calls[identity] = (index, function["arguments"], name)
        if not sent_role and (deltas or event.get("type") == "response.created" or state.terminal):
            yield chunk({"role": "assistant"})
            sent_role = True
        for delta in deltas:
            yield chunk(delta)
        if state.terminal:
            yield chunk({}, state.finish_reason(legacy))
            if include_usage:
                yield "data: " + json.dumps({
                    "id": response_id, "object": "chat.completion.chunk", "created": created,
                    "model": request.model, "choices": [], "usage": bridge._usage_from_response(state.response),
                }) + "\n\n"
            yield "data: [DONE]\n\n"
            return
    error = state.failure("Upstream stream ended before a terminal response", "upstream_stream_truncated")
    yield f"data: {json.dumps({'error': error})}\n\n"


def response_result(state, request):
    response = dict(state.response)
    response.setdefault("id", f"resp_{uuid.uuid4().hex}")
    response.setdefault("created_at", int(time.time()))
    response.setdefault("object", "response")
    response.setdefault("model", request.model)
    response.setdefault("usage", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    response["output"] = state.output()
    response["output_text"] = state.text()
    return response


async def responses(bridge, request):
    state = EventAssembly()
    async for event in bridge._codex_event_stream_from_payload(bridge._build_responses_payload(request)):
        if not state.feed(event):
            continue
        if state.error:
            return None, state.error
        if state.terminal:
            return response_result(state, request), None
    return None, state.failure("Upstream stream ended before a terminal response", "upstream_stream_truncated")


async def responses_stream(bridge, request, on_complete=None):
    state = EventAssembly()
    async for event in bridge._codex_event_stream_from_payload(bridge._build_responses_payload(request)):
        if not state.feed(event):
            continue
        event_type = event.get("type") or "response.event"
        if state.error and event_type not in {"response.failed", "error"}:
            yield f"event: error\ndata: {json.dumps(state.error)}\n\n"
            return
        if state.terminal:
            result = response_result(state, request)
            if on_complete is not None and state.terminal == "response.completed":
                on_complete(result)
            event = {**event, "response": result}
        yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
        if state.terminal or state.error:
            return
    error = state.failure("Upstream stream ended before a terminal response", "upstream_stream_truncated")
    yield f"event: error\ndata: {json.dumps(error)}\n\n"
