import json
import time
import uuid
from typing import Any, AsyncGenerator

import httpx

from config import settings
from schemas import ChatCompletionRequest


class ChatGPTBridge:
    def __init__(self):
        self.base_url = settings.chatgpt_base_url.rstrip("/")

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.chatgpt_access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "codex_cli_rs/0.0.0 (Codex Provider Bridge)",
            "originator": "codex_cli_rs",
            "version": "0.0.0",
        }

    def _resolve_reasoning_effort(self, request: ChatCompletionRequest) -> str | None:
        if request.reasoning_effort:
            return request.reasoning_effort

        if request.reasoning and request.reasoning.effort:
            return request.reasoning.effort

        return None

    def _extract_instructions_and_input(self, messages) -> tuple[str | None, list[dict[str, Any]]]:
        instructions = []
        input_items = []

        for message in messages:
            if message.role in {"system", "developer"}:
                if message.content:
                    instructions.append(message.content)
                continue

            role = "assistant" if message.role == "assistant" else "user"
            input_items.append(
                {
                    "role": role,
                    "content": [{"type": "input_text", "text": message.content}],
                }
            )

        compiled_instructions = "\n\n".join(part for part in instructions if part).strip() or None
        return compiled_instructions, input_items

    def _build_payload(self, request: ChatCompletionRequest) -> dict[str, Any]:
        instructions, input_items = self._extract_instructions_and_input(request.messages)
        if not instructions:
            instructions = "You are a helpful assistant."

        payload: dict[str, Any] = {
            "model": request.model,
            "input": input_items,
            "stream": True,
            "store": False,
            "instructions": instructions,
        }

        reasoning_effort = self._resolve_reasoning_effort(request)
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}

        return payload

    async def _codex_event_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncGenerator[dict[str, Any], None]:
        payload = self._build_payload(request)

        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/backend-api/codex/responses",
                headers=self._build_headers(),
                json=payload,
            ) as response:
                if response.status_code != 200:
                    error_detail = await response.aread()
                    yield {
                        "type": "error",
                        "error": "Failed to connect to ChatGPT",
                        "detail": error_detail.decode(),
                    }
                    return

                current_event = None
                async for line in response.aiter_lines():
                    if not line:
                        continue

                    if line.startswith("event: "):
                        current_event = line[7:]
                        continue

                    if not line.startswith("data: "):
                        continue

                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    if "type" not in event and current_event:
                        event["type"] = current_event

                    yield event

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        full_text = []
        response_id = f"chatcmpl-{uuid.uuid4()}"
        created = int(time.time())
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        async for event in self._codex_event_stream(request):
            event_type = event.get("type")

            if event_type == "error":
                return None, event

            if event_type == "response.created":
                response = event.get("response", {})
                response_id = response.get("id", response_id)
                created = response.get("created_at", created)
                continue

            if event_type == "response.output_text.delta":
                delta = event.get("delta", "")
                if delta:
                    full_text.append(delta)
                continue

            if event_type == "response.completed":
                response = event.get("response", {})
                raw_usage = response.get("usage") or {}
                usage = {
                    "prompt_tokens": raw_usage.get("input_tokens", 0),
                    "completion_tokens": raw_usage.get("output_tokens", 0),
                    "total_tokens": raw_usage.get("total_tokens", 0),
                }
                return (
                    {
                        "id": response_id,
                        "created": created,
                        "model": request.model,
                        "content": "".join(full_text),
                        "usage": usage,
                    },
                    None,
                )

        return (
            {
                "id": response_id,
                "created": created,
                "model": request.model,
                "content": "".join(full_text),
                "usage": usage,
            },
            None,
        )

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncGenerator[str, None]:
        response_id = f"chatcmpl-{uuid.uuid4()}"
        created = int(time.time())

        async for event in self._codex_event_stream(request):
            event_type = event.get("type")

            if event_type == "error":
                yield f"data: {json.dumps(event)}\n\n"
                return

            if event_type == "response.created":
                response = event.get("response", {})
                response_id = response.get("id", response_id)
                created = response.get("created_at", created)
                continue

            if event_type == "response.output_text.delta":
                delta = event.get("delta", "")
                if not delta:
                    continue

                chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": delta},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                continue

            if event_type == "response.completed":
                chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
                return


bridge = ChatGPTBridge()
