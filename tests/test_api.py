import asyncio

from fastapi.testclient import TestClient

from bridge import ChatGPTBridge
from main import app
from schemas import AudioSpeechRequest, ImageGenerationRequest


client = TestClient(app)


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
