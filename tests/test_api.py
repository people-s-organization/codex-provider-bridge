import asyncio
import json

from fastapi.testclient import TestClient

from bridge import ChatGPTBridge
from main import app
from schemas import AudioSpeechRequest, ChatCompletionRequest, ImageGenerationRequest


client = TestClient(app)


def _sse_data_lines(text):
    return [line.removeprefix("data: ") for line in text.splitlines() if line.startswith("data: ")]


def test_models_route_exists():
    response = client.get("/v1/models")

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert data["data"]
    assert data["data"][0]["id"]


def test_models_route_can_be_configured(monkeypatch):
    monkeypatch.setenv("CHATGPT_MODELS", "gpt-test-a,gpt-test-b")
    monkeypatch.setenv("CHATGPT_DEFAULT_MODEL", "")
    monkeypatch.setenv("CHATGPT_EXTRA_MODELS", "")

    response = client.get("/v1/models")

    assert response.status_code == 200
    data = response.json()
    assert [model["id"] for model in data["data"]] == ["gpt-test-a", "gpt-test-b"]


def test_responses_route_returns_openai_like_shape(monkeypatch):
    async def _fake_responses(request):
        return (
            {
                "id": "resp_test",
                "object": "response",
                "created_at": 123,
                "status": "completed",
                "model": request.model,
                "output_text": '{"summary":"ok"}',
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"summary":"ok"}', "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
            None,
        )

    monkeypatch.setattr("main.bridge.responses", _fake_responses)

    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.4-mini",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
            "text": {"format": {"type": "json_schema", "name": "demo", "schema": {"type": "object"}, "strict": True}},
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "response"
    assert data["output_text"] == '{"summary":"ok"}'


def test_responses_stream_route_returns_sse(monkeypatch):
    async def _fake_responses_stream(request):
        yield 'event: response.created\ndata: {"type":"response.created"}\n\n'
        yield 'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        yield 'event: response.completed\ndata: {"type":"response.completed"}\n\n'

    monkeypatch.setattr("main.bridge.responses_stream", _fake_responses_stream)

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-5.4-mini", "input": "hello", "stream": True},
    )

    assert response.status_code == 200
    assert "event: response.output_text.delta" in response.text


def test_chat_completions_stream_returns_openai_chunks(monkeypatch):
    async def _fake_codex_event_stream(request):
        yield {"type": "response.created", "response": {"id": "chatcmpl_test", "created_at": 123}}
        yield {"type": "response.output_text.delta", "delta": "Hel"}
        yield {"type": "response.output_text.delta", "delta": "lo"}
        yield {
            "type": "response.completed",
            "response": {
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "total_tokens": 3,
                }
            },
        }

    monkeypatch.setattr("main.bridge._codex_event_stream", _fake_codex_event_stream)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )

    assert response.status_code == 200
    data_lines = _sse_data_lines(response.text)
    assert data_lines[-1] == "[DONE]"

    chunks = [json.loads(line) for line in data_lines[:-1]]
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[0]["usage"] is None
    assert [chunk["choices"][0]["delta"].get("content") for chunk in chunks[1:3]] == ["Hel", "lo"]
    assert chunks[3]["choices"][0]["finish_reason"] == "stop"
    assert chunks[3]["usage"] is None
    assert chunks[4]["choices"] == []
    assert chunks[4]["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
    }


def test_chat_content_parts_are_converted_for_codex():
    bridge = ChatGPTBridge()
    payload = bridge._build_payload(
        ChatCompletionRequest(
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": [{"type": "text", "text": "Be terse."}]},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is in this image?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAA", "detail": "low"},
                        },
                    ],
                },
            ],
        )
    )

    assert payload["instructions"] == "Be terse."
    assert payload["input"][0]["content"][0] == {"type": "input_text", "text": "What is in this image?"}
    assert payload["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,AAA",
        "detail": "low",
    }


def test_completions_route_returns_legacy_shape(monkeypatch):
    async def _fake_completion(request):
        return (
            {
                "id": "cmpl_test",
                "object": "text_completion",
                "created": 123,
                "model": request.model,
                "choices": [{"text": "ok", "index": 0, "logprobs": None, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            None,
        )

    monkeypatch.setattr("main.bridge.completion", _fake_completion)

    response = client.post("/v1/completions", json={"model": "gpt-5.5", "prompt": "hello"})

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "text_completion"
    assert data["choices"][0]["text"] == "ok"


def test_completions_stream_skips_chat_role_chunk(monkeypatch):
    async def _fake_codex_event_stream(request):
        yield {"type": "response.created", "response": {"id": "chatcmpl_test", "created_at": 123}}
        yield {"type": "response.output_text.delta", "delta": "ok"}
        yield {"type": "response.completed", "response": {"usage": {}}}

    monkeypatch.setattr("main.bridge._codex_event_stream", _fake_codex_event_stream)

    response = client.post(
        "/v1/completions",
        json={"model": "gpt-5.5", "prompt": "hello", "stream": True},
    )

    assert response.status_code == 200
    data_lines = _sse_data_lines(response.text)
    assert data_lines[-1] == "[DONE]"
    chunks = [json.loads(line) for line in data_lines[:-1]]
    assert chunks[0]["choices"][0]["text"] == "ok"
    assert chunks[1]["choices"][0]["finish_reason"] == "stop"


def test_required_tool_choice_reports_openai_error():
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "demo", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "function": {"name": "demo"}},
        },
    )

    assert response.status_code == 501
    data = response.json()
    assert data["error"]["code"] == "unsupported_tool_calling"
    assert data["error"]["param"] == "tool_choice"


def test_image_generation_route_returns_b64_payload(monkeypatch):
    async def _fake_image_generation(request):
        return (
            {
                "created": 123,
                "data": [{"b64_json": "dGVzdA==", "revised_prompt": request.prompt}],
            },
            None,
        )

    monkeypatch.setattr("main.bridge.image_generation", _fake_image_generation)

    response = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-2", "prompt": "hello", "size": "1024x1024", "response_format": "b64_json"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["data"][0]["b64_json"]


def test_codex_image_generation_uses_image_tool(monkeypatch):
    captured_payloads = []

    async def _fake_codex_event_stream(payload):
        captured_payloads.append(payload)
        yield {"type": "response.created", "response": {"id": "resp_test", "created_at": 123, "model": "gpt-5.5"}}
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "image_generation_call",
                "id": "ig_test",
                "status": "completed",
                "revised_prompt": "a small blue square",
                "result": "Zm9v",
            },
        }
        yield {"type": "response.completed", "response": {"id": "resp_test", "model": "gpt-5.5"}}

    monkeypatch.setenv("CHATGPT_MEDIA_MODEL", "gpt-5.5")
    bridge = ChatGPTBridge()
    monkeypatch.setattr(bridge, "_codex_event_stream_from_payload", _fake_codex_event_stream)

    result, error = asyncio.run(
        bridge._image_generation_via_codex(
            ImageGenerationRequest(model="gpt-image-2", prompt="hello", output_format="png")
        )
    )

    assert error is None
    assert result["data"][0]["b64_json"] == "Zm9v"
    assert result["data"][0]["revised_prompt"] == "a small blue square"
    assert captured_payloads[0]["tools"] == [{"type": "image_generation", "output_format": "png"}]
    assert captured_payloads[0]["model"] == "gpt-5.5"


def test_audio_speech_route_returns_binary_audio(monkeypatch):
    async def _fake_synthesize_speech(request):
        return b"audio", "audio/wav", None

    monkeypatch.setattr("main.bridge.synthesize_speech", _fake_synthesize_speech)

    response = client.post(
        "/v1/audio/speech",
        json={"model": "gpt-4o-mini-tts", "input": "hello", "voice": "marin", "response_format": "wav"},
    )

    assert response.status_code == 200
    assert response.content
    assert response.headers["content-type"].startswith("audio/")


def test_speech_prefers_chatgpt_realtime(monkeypatch):
    async def _fake_realtime_speech(request):
        return b"wav-audio", "audio/wav", None

    bridge = ChatGPTBridge()
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(bridge, "_synthesize_speech_via_realtime", _fake_realtime_speech)

    audio_bytes, media_type, error = asyncio.run(
        bridge.synthesize_speech(
            AudioSpeechRequest(model="gpt-4o-mini-tts", input="hello", response_format="wav")
        )
    )

    assert error is None
    assert audio_bytes == b"wav-audio"
    assert media_type == "audio/wav"


def test_realtime_pcm_can_be_wrapped_as_wav():
    bridge = ChatGPTBridge()
    audio_bytes, media_type, error = bridge._convert_pcm_audio(b"\x00\x00" * 240, "wav")

    assert error is None
    assert media_type == "audio/wav"
    assert audio_bytes.startswith(b"RIFF")
    assert b"WAVE" in audio_bytes[:16]


def test_media_routes_report_missing_auth(monkeypatch):
    async def _fake_image_generation(request):
        return None, {"status": 501, "error": "Media endpoints require auth"}

    monkeypatch.setattr("main.bridge.image_generation", _fake_image_generation)

    response = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-2", "prompt": "hello"},
    )

    assert response.status_code == 501
    assert response.json()["error"]["message"].startswith("Media endpoints require auth")


def test_unimplemented_v1_route_explains_or_proxies(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")

    response = client.post("/v1/embeddings", json={"model": "text-embedding-3-small", "input": "hello"})

    assert response.status_code == 501
    data = response.json()
    assert data["error"]["code"] == "unsupported_endpoint"
    assert "Set OPENAI_API_KEY" in data["error"]["message"]
