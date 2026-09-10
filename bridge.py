import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import time
import uuid
import wave
from typing import Any, AsyncGenerator

import httpx
import websockets
from dotenv import load_dotenv

from config import settings
from upstream_transport import UpstreamTransport
from model_registry import default_model_id, is_available_model, resolve_model_name
from schemas import (
    AudioSpeechRequest,
    ChatCompletionRequest,
    CompletionRequest,
    ImageGenerationRequest,
    Message,
    ResponsesRequest,
)


class ToolCallAccumulator:
    """Compatibility wrapper for tool calls reconciled by EventAssembly."""

    def __init__(self) -> None:
        from event_assembly import EventAssembly

        self._assembly = EventAssembly()

    def _call_for(self, event: dict[str, Any]) -> dict[str, Any] | None:
        item_id = (event.get("item") or {}).get("id") or event.get("item_id")
        key = self._assembly.ids.get(item_id, event.get("output_index"))
        item = self._assembly.items.get(key, {})
        if item.get("type") != "function_call" or not item.get("call_id") or not item.get("name"):
            return None
        return {
            "id": item["call_id"],
            "type": "function",
            "function": {"name": item["name"], "arguments": item.get("arguments", "")},
        }

    def register(self, item: dict[str, Any]) -> dict[str, Any] | None:
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            return None
        event = {"type": "response.output_item.added", "item": {"type": "function_call", **item, "id": item_id}}
        self._assembly.feed(event)
        return self._call_for(event)

    def append_arguments(self, item_id: str, delta: str) -> dict[str, Any] | None:
        item_id = str(item_id or "").strip()
        if not item_id:
            return None
        event = {"type": "response.function_call_arguments.delta", "item_id": item_id, "delta": delta}
        self._assembly.feed(event)
        return self._call_for(event)

    def index_of(self, item_id: str) -> int:
        item_id = str(item_id or "").strip()
        items = self.as_output_items()
        return next((index for index, item in enumerate(items) if item.get("id") == item_id), len(items))

    def __bool__(self) -> bool:
        return bool(self.as_tool_calls())

    def as_tool_calls(self) -> list[dict[str, Any]]:
        return self._assembly.calls()

    def as_output_items(self) -> list[dict[str, Any]]:
        return [item for item in self._assembly.output() if item.get("type") == "function_call"]

    def handle_event(self, event: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        actions = {
            "response.output_item.added": "added",
            "response.output_item.done": "done",
            "response.function_call_arguments.delta": "arguments",
            "response.function_call_arguments.done": "done",
        }
        event_type = event.get("type")
        if not self._assembly.feed(event):
            return None, ""
        action = actions.get(event_type, "")
        if not action:
            return None, ""
        if event_type.startswith("response.output_item.") and (event.get("item") or {}).get("type") != "function_call":
            return None, ""
        if event_type == "response.function_call_arguments.delta" and not event.get("delta"):
            return None, ""
        return self._call_for(event), action


class ChatGPTBridge:
    def __init__(self):
        self.base_url = settings.chatgpt_base_url.rstrip("/")
        self.openai_base_url = settings.openai_base_url.rstrip("/")
        self._transport = UpstreamTransport()

    async def aclose(self):
        await self._transport.aclose()

    def _build_headers(self) -> dict[str, str]:
        from runtime_credentials import access_token

        token = access_token(settings.chatgpt_access_token)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "codex_cli_rs/0.0.0 (Codex Provider Bridge)",
            "originator": "codex_cli_rs",
            "version": "0.0.0",
        }

    def _build_openai_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Codex Provider Bridge",
        }

    def _chatgpt_account_id(self) -> str | None:
        load_dotenv(".env", override=False)
        configured_account_id = str(os.getenv("CHATGPT_ACCOUNT_ID") or "").strip()
        if configured_account_id:
            return configured_account_id

        auth_file = os.path.expanduser("~/.codex/auth.json")
        try:
            with open(auth_file) as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return None

        account_id = str((data.get("tokens") or {}).get("account_id") or "").strip()
        return account_id or None

    def _build_chatgpt_bearer_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {settings.chatgpt_access_token}",
            "User-Agent": "codex_cli_rs/0.0.0 (Codex Provider Bridge)",
        }
        account_id = self._chatgpt_account_id()
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        return headers

    def _resolve_model_name(self, model_name: str) -> str:
        return resolve_model_name(model_name)

    def _resolve_media_responses_model(self, request: ImageGenerationRequest) -> str:
        """Pick the Responses model that drives the Codex ``image_generation`` tool.

        The image endpoint's own ``model`` field names an image model (``gpt-image-*``)
        which the Codex Responses endpoint rejects outright, so it is only used when it
        happens to be a model this account really exposes.
        """

        load_dotenv(override=False)
        configured_model = str(os.getenv("CHATGPT_MEDIA_MODEL") or "").strip()
        if configured_model:
            return self._resolve_model_name(configured_model)

        default_model = str(os.getenv("CHATGPT_DEFAULT_MODEL") or "").strip()
        if default_model:
            return self._resolve_model_name(default_model)

        requested_model = self._resolve_model_name(request.model)
        if is_available_model(requested_model):
            return requested_model

        detected_default = default_model_id()
        if detected_default:
            return detected_default

        return requested_model

    def _resolve_realtime_model(self, request: AudioSpeechRequest) -> str:
        load_dotenv(".env", override=False)
        configured_model = str(os.getenv("CHATGPT_REALTIME_MODEL") or "").strip()
        if configured_model:
            return self._resolve_model_name(configured_model)
        return self._resolve_model_name(request.model)

    def _resolve_reasoning_effort(self, request: ChatCompletionRequest) -> str | None:
        if request.reasoning_effort:
            return request.reasoning_effort

        if request.reasoning and request.reasoning.effort:
            return request.reasoning.effort

        return None

    def _stringify_content(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            return str(content)

        text_parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
                continue
            if not isinstance(part, dict):
                text_parts.append(str(part))
                continue

            part_type = part.get("type")
            if part_type in {"text", "input_text", "output_text"} and part.get("text") is not None:
                text_parts.append(str(part["text"]))
            elif part_type == "refusal" and part.get("refusal") is not None:
                text_parts.append(str(part["refusal"]))
            elif part_type in {"image_url", "input_image"}:
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                image_url = image_url or part.get("url")
                text_parts.append(f"[image: {image_url or 'attached'}]")
            elif part_type in {"input_audio", "audio"}:
                text_parts.append("[audio input omitted: unsupported by the Codex text bridge]")
            else:
                text_parts.append(json.dumps(part, ensure_ascii=False))

        return "\n".join(part for part in text_parts if part).strip()

    def _content_to_codex_items(self, content: Any, role: str = "user") -> list[dict[str, Any]]:
        """Convert OpenAI content into Codex Responses input content.

        The role decides the part type: the Codex backend only accepts ``output_text``
        and ``refusal`` inside an assistant turn, and ``input_text`` / ``input_image``
        inside a user turn. Sending ``input_text`` for an assistant message makes the
        upstream reject the whole payload with ``Invalid value: 'input_text'``.
        """

        is_assistant = role == "assistant"
        text_type = "output_text" if is_assistant else "input_text"

        if content is None:
            return []
        if isinstance(content, str):
            return [{"type": text_type, "text": content}]
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            return [{"type": text_type, "text": str(content)}]

        items: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, str):
                items.append({"type": text_type, "text": part})
                continue
            if not isinstance(part, dict):
                items.append({"type": text_type, "text": str(part)})
                continue

            part_type = part.get("type")
            if part_type in {"text", "input_text", "output_text"} and part.get("text") is not None:
                items.append({"type": text_type, "text": str(part["text"])})
                continue
            if part_type == "refusal" and part.get("refusal") is not None:
                if is_assistant:
                    items.append({"type": "refusal", "refusal": str(part["refusal"])})
                else:
                    items.append({"type": "input_text", "text": str(part["refusal"])})
                continue
            if part_type in {"image_url", "input_image"}:
                image_url = part.get("image_url")
                detail = part.get("detail")
                if isinstance(image_url, dict):
                    detail = detail or image_url.get("detail")
                    image_url = image_url.get("url")
                image_url = image_url or part.get("url")
                if not image_url:
                    continue
                if is_assistant:
                    items.append({"type": "output_text", "text": f"[image: {image_url}]"})
                else:
                    item = {"type": "input_image", "image_url": image_url}
                    if detail:
                        item["detail"] = detail
                    items.append(item)
                continue
            if part_type in {"input_audio", "audio"}:
                items.append(
                    {
                        "type": text_type,
                        "text": "[audio input omitted: unsupported by the Codex text bridge]",
                    }
                )
                continue

            items.append({"type": text_type, "text": json.dumps(part, ensure_ascii=False)})

        return items or [{"type": text_type, "text": ""}]

    def _normalize_responses_input_items(self, request_input: Any) -> tuple[str | None, list[Any]]:
        """Validate complete replay without demoting roles or losing opaque metadata.

        Returns ``(instructions, items)``. The Codex backend rejects a ``system`` role
        inside ``input`` with HTTP 400 "System messages are not allowed", so system and
        developer turns are hoisted into the dedicated instructions field in order
        instead of being demoted to ``user`` or forwarded as input messages.
        """

        from copy import deepcopy
        from schemas import validate_response_history

        if not isinstance(request_input, list) or any(not isinstance(item, dict) for item in request_input):
            raise ValueError("Responses input must be text or an array of objects")
        validate_response_history(request_input)
        request_input = deepcopy(request_input)
        instructions: list[str] = []
        normalized: list[Any] = []
        for item in request_input:
            role = item.get("role")
            if role in {"system", "developer"} and "content" in item:
                text = self._stringify_content(item.get("content")).strip()
                if text:
                    instructions.append(text)
                continue
            if role not in {"user", "assistant"} or "content" not in item:
                normalized.append(item)
                continue

            normalized_item = dict(item)
            normalized_item["role"] = role
            content = item.get("content")
            native = {"output_text", "refusal"} if role == "assistant" else {"input_text", "input_image"}
            if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") in native for p in content):
                normalized_item["content"] = content
            else:
                parts = content if isinstance(content, list) else [content]
                converted = []
                for part in parts:
                    if isinstance(part, dict) and part.get("type") in native:
                        converted.append(part)
                    elif isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"} and "text" in part:
                        converted.append({**part, "type": "output_text" if role == "assistant" else "input_text"})
                    else:
                        converted.extend(self._content_to_codex_items(part, role))
                normalized_item["content"] = converted
            normalized.append(normalized_item)

        return "\n\n".join(instructions) or None, normalized

    def _tool_result_message(self, output: Any, call_id: str) -> dict[str, Any]:
        """Render a tool result the upstream will accept even without its call."""

        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        text = (text or "").strip() or "(tool returned no output)"
        label = f"call_id={call_id}" if call_id else "unknown call_id"
        return {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": f"[tool result ({label})]\n{text}"}],
        }

    def _assistant_tool_call_items(self, tool_calls: Any) -> list[dict[str, Any]]:
        """Rebuild Responses ``function_call`` items from OpenAI ``tool_calls``.

        Replaying the call is what makes the later ``function_call_output`` valid; the
        Codex backend accepts the pair (verified) but rejects the output on its own.
        Malformed calls are rejected rather than invented or silently omitted.
        """

        from schemas import validate_call

        if not isinstance(tool_calls, list):
            raise ValueError("assistant tool_calls must be an array")
        items = []
        seen = set()
        for call in tool_calls:
            if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
                raise ValueError("assistant tool_calls must contain function objects")
            function = call["function"]
            item = {"type": "function_call", "call_id": call.get("id"), "name": function.get("name"), "arguments": function.get("arguments")}
            validate_call(item)
            if item["call_id"] in seen:
                raise ValueError("duplicate function call id")
            seen.add(item["call_id"])
            items.append(item)
        return items

    def _message_to_response_input_items(
        self, message: Message, known_call_ids: set[str]
    ) -> list[dict[str, Any]]:
        if message.role == "function" or message.function_call is not None:
            raise ValueError("legacy replay requires full history normalization")
        if message.role == "tool":
            output = self._stringify_content(message.content)
            call_id = message.tool_call_id
            if not call_id or call_id not in known_call_ids:
                raise ValueError("orphan or duplicate tool result")
            known_call_ids.remove(call_id)
            return [{"type": "function_call_output", "call_id": call_id, "output": output}]

        role = message.role
        if role not in {"assistant", "user", "system", "developer"}:
            raise ValueError("unsupported message role")
        content = self._content_to_codex_items(message.content, role)
        items: list[dict[str, Any]] = []

        if role == "assistant" and message.tool_calls:
            call_items = self._assistant_tool_call_items(message.tool_calls)
            if any(item["call_id"] in known_call_ids for item in call_items):
                raise ValueError("duplicate function call id")
            if content:
                items.append({"type": "message", "role": role, "content": content})
            items.extend(call_items)
            known_call_ids.update(item["call_id"] for item in call_items)
            return items

        if content:
            items.append({"type": "message", "role": role, "content": content})
        return items

    def _extract_instructions_and_input(self, messages) -> tuple[str | None, list[dict[str, Any]]]:
        from schemas import chat_history_items

        # Instruction turns keep their role and order by becoming instructions, never by
        # being demoted to user and never as system-role input items (rejected upstream).
        return self._normalize_responses_input_items(chat_history_items(messages))

    def _normalize_tool_definitions(self, tools: Any) -> tuple[list[dict[str, Any]], list[str]]:
        """Flatten OpenAI tool definitions into the Codex responses shape.

        Chat Completions nests the definition under ``function``; the Responses API and
        the Codex backend both use the flat ``{type, name, description, parameters}``
        shape. Only client-executed ``function`` tools are supported; all other types
        fail validation explicitly.
        """

        from schemas import normalize_tools

        return normalize_tools(tools), []

    def _normalize_tool_choice(self, tool_choice: Any, function_call: Any = None) -> Any:
        """Map Chat Completions / Responses tool_choice onto the Codex shape."""

        from schemas import normalize_choice

        return normalize_choice(tool_choice, function_call)

    def _apply_tools(
        self,
        payload: dict[str, Any],
        instructions: str,
        tools: Any,
        tool_choice: Any = None,
        function_call: Any = None,
        legacy_functions: Any = None,
        parallel_tool_calls: Any = None,
    ) -> str:
        """Attach forwarded tools to an upstream payload, returning final instructions."""

        if tools is not None and legacy_functions is not None:
            raise ValueError("tools and legacy functions cannot both be specified")
        combined = tools if tools is not None else [{"type": "function", "function": f} for f in (legacy_functions or [])]
        forwarded, _ = self._normalize_tool_definitions(combined)
        choice = self._normalize_tool_choice(tool_choice, function_call)
        if isinstance(choice, dict) and choice["name"] not in {t["name"] for t in forwarded}:
            raise ValueError("tool_choice names an undefined function")
        if choice == "required" and not forwarded:
            raise ValueError("tool_choice required needs at least one function")
        if parallel_tool_calls is not None and not isinstance(parallel_tool_calls, bool):
            raise ValueError("parallel_tool_calls must be a boolean")
        if forwarded:
            payload["tools"] = forwarded
        if choice is not None:
            payload["tool_choice"] = choice
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
        return instructions

    def _unsupported_chat_options_error(self, request: ChatCompletionRequest) -> dict[str, Any] | None:
        if request.n not in {None, 1}:
            return {
                "status": 501,
                "error": "Multiple choices are not supported by the Codex backend bridge",
                "type": "unsupported_feature",
                "param": "n",
                "code": "unsupported_multiple_choices",
                "detail": "The upstream Codex responses channel returns one assistant answer per turn.",
            }
        if request.modalities and any(modality != "text" for modality in request.modalities):
            return {
                "status": 501,
                "error": "Chat Completions audio output is not supported by this bridge",
                "type": "unsupported_feature",
                "param": "modalities",
                "code": "unsupported_chat_audio_output",
                "detail": "Use /v1/audio/speech for text-to-speech output.",
            }
        return None

    def _augment_instructions_for_chat_response_format(
        self,
        instructions: str,
        response_format: dict[str, Any] | None,
    ) -> str:
        if not isinstance(response_format, dict):
            return instructions

        format_type = response_format.get("type")
        if format_type == "json_object":
            return f"{instructions}\n\nReturn only valid JSON with no markdown fences or commentary."

        if format_type == "json_schema":
            json_schema = response_format.get("json_schema") or {}
            return self._augment_instructions_for_schema(
                instructions,
                {
                    "type": "json_schema",
                    "name": json_schema.get("name") or "response",
                    "schema": json_schema.get("schema") or {},
                    "strict": json_schema.get("strict", False),
                },
            )

        return instructions

    def _build_payload(self, request: ChatCompletionRequest) -> dict[str, Any]:
        instructions, input_items = self._extract_instructions_and_input(request.messages)
        if not instructions:
            instructions = "You are a helpful assistant."
        instructions = self._augment_instructions_for_chat_response_format(
            instructions,
            request.response_format,
        )

        tool_payload: dict[str, Any] = {}
        instructions = self._apply_tools(
            tool_payload,
            instructions,
            tools=request.tools,
            tool_choice=request.tool_choice,
            function_call=request.function_call,
            legacy_functions=request.functions,
            parallel_tool_calls=request.parallel_tool_calls,
        )

        payload: dict[str, Any] = {
            "model": self._resolve_model_name(request.model),
            "input": input_items,
            "stream": True,
            "store": False,
            "instructions": instructions,
        }
        payload.update(tool_payload)

        reasoning_effort = self._resolve_reasoning_effort(request)
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        # Client output limits are intentionally not forwarded: the ChatGPT/Codex
        # responses endpoint rejects max_output_tokens, max_tokens and
        # max_completion_tokens with HTTP 400 "Unsupported parameter".

        return payload

    def _augment_instructions_for_schema(
        self,
        instructions: str | None,
        text_format: dict[str, Any] | None,
    ) -> str:
        base = (instructions or "You are a helpful assistant.").strip()
        if not isinstance(text_format, dict) or text_format.get("type") != "json_schema":
            return base

        schema_name = str(text_format.get("name") or "response")
        schema = text_format.get("schema") or {}
        strict = bool(text_format.get("strict", False))
        schema_instruction = (
            "Return only valid JSON with no markdown fences or commentary. "
            f"The JSON must satisfy schema '{schema_name}'. "
            f"Strict mode is {'on' if strict else 'off'}.\n"
            f"JSON schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}"
        )
        if not base:
            return schema_instruction
        return f"{base}\n\n{schema_instruction}"

    def _build_responses_payload(self, request: ResponsesRequest) -> dict[str, Any]:
        request_input: Any
        replayed_instructions: str | None = None
        if isinstance(request.input, str):
            request_input = [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": request.input}],
                }
            ]
        else:
            replayed_instructions, request_input = self._normalize_responses_input_items(request.input)

        instruction_parts = [part for part in (request.instructions, replayed_instructions) if part]
        instructions = self._augment_instructions_for_schema(
            "\n\n".join(instruction_parts) or None,
            request.text.get("format") if isinstance(request.text, dict) else None,
        )
        tool_payload: dict[str, Any] = {}
        instructions = self._apply_tools(
            tool_payload,
            instructions,
            tools=request.tools,
            tool_choice=request.tool_choice,
            parallel_tool_calls=request.parallel_tool_calls,
        )

        payload: dict[str, Any] = {
            "model": self._resolve_model_name(request.model),
            "input": request_input,
            "stream": True,
            "store": False,
            "instructions": instructions,
        }
        payload.update(tool_payload)
        if request.reasoning and request.reasoning.effort:
            payload["reasoning"] = {"effort": request.reasoning.effort}
        # max_output_tokens is rejected upstream ("Unsupported parameter"), so the
        # request's limit is deliberately dropped here.
        return payload

    def _usage_from_response(self, response: dict[str, Any]) -> dict[str, int]:
        raw_usage = response.get("usage") or {}
        return {
            "prompt_tokens": raw_usage.get("input_tokens", raw_usage.get("prompt_tokens", 0)),
            "completion_tokens": raw_usage.get("output_tokens", raw_usage.get("completion_tokens", 0)),
            "total_tokens": raw_usage.get("total_tokens", 0),
        }

    def _stream_error_event(self, error: dict[str, Any]) -> dict[str, Any]:
        event = dict(error)
        event["error_type"] = error.get("type")
        event["type"] = "error"
        return event

    def _chat_stream_chunk(
        self,
        response_id: str,
        created: int,
        model: str,
        delta: dict[str, Any],
        finish_reason: str | None = None,
        include_usage: bool = False,
    ) -> dict[str, Any]:
        chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
        if include_usage:
            chunk["usage"] = None
        return chunk

    async def _codex_event_stream_from_payload(
        self, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        async for event in self._transport.events(
            f"{self.base_url}/backend-api/codex/responses",
            self._build_headers(), payload,
        ):
            yield event

    async def _codex_event_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncGenerator[dict[str, Any], None]:
        unsupported_error = self._unsupported_chat_options_error(request)
        if unsupported_error:
            yield self._stream_error_event(unsupported_error)
            return

        async for event in self._codex_event_stream_from_payload(self._build_payload(request)):
            yield event

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        from event_adapters import chat_completion

        return await chat_completion(self, request)

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncGenerator[str, None]:
        from event_adapters import chat_completion_stream

        async for chunk in chat_completion_stream(self, request):
            yield chunk

    async def responses(self, request: ResponsesRequest) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        from event_adapters import responses

        return await responses(self, request)

    async def responses_stream(self, request: ResponsesRequest) -> AsyncGenerator[str, None]:
        from event_adapters import responses_stream

        async for chunk in responses_stream(self, request):
            yield chunk

    def _completion_prompts(self, prompt: Any) -> list[str]:
        if prompt is None:
            return [""]
        if isinstance(prompt, str):
            return [prompt]
        if isinstance(prompt, list) and all(isinstance(item, str) for item in prompt):
            return prompt or [""]
        return [json.dumps(prompt, ensure_ascii=False)]

    def _completion_to_chat_request(self, request: CompletionRequest, prompt: str) -> ChatCompletionRequest:
        max_tokens = request.max_tokens
        return ChatCompletionRequest(
            model=request.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=request.temperature,
            top_p=request.top_p,
            n=1,
            stream=False,
            stop=request.stop,
            max_tokens=max_tokens,
            presence_penalty=request.presence_penalty,
            frequency_penalty=request.frequency_penalty,
            logit_bias=request.logit_bias,
            user=request.user,
        )

    def _unsupported_completion_options_error(self, request: CompletionRequest) -> dict[str, Any] | None:
        if request.n not in {None, 1}:
            return {
                "status": 501,
                "error": "Multiple legacy completion choices are not supported",
                "type": "unsupported_feature",
                "param": "n",
                "code": "unsupported_multiple_choices",
                "detail": "The bridge maps legacy /v1/completions to one chat turn per prompt.",
            }
        if request.best_of not in {None, 1}:
            return {
                "status": 501,
                "error": "best_of is not supported by the bridge",
                "type": "unsupported_feature",
                "param": "best_of",
                "code": "unsupported_best_of",
                "detail": "The upstream Codex responses channel does not expose server-side best_of sampling.",
            }
        if request.logprobs is not None:
            return {
                "status": 501,
                "error": "logprobs are not supported by the Codex backend bridge",
                "type": "unsupported_feature",
                "param": "logprobs",
                "code": "unsupported_logprobs",
                "detail": "The upstream Codex responses channel does not return token log probabilities.",
            }
        return None

    async def completion(self, request: CompletionRequest) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        unsupported_error = self._unsupported_completion_options_error(request)
        if unsupported_error:
            return None, unsupported_error

        completion_id = f"cmpl-{uuid.uuid4()}"
        created = int(time.time())
        choices: list[dict[str, Any]] = []
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        for index, prompt in enumerate(self._completion_prompts(request.prompt)):
            result, error = await self.chat_completion(self._completion_to_chat_request(request, prompt))
            if error:
                return None, error
            text = result["content"] if result else ""
            if request.echo:
                text = f"{prompt}{text}"
            choices.append(
                {
                    "text": text,
                    "index": index,
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            )
            usage = (result or {}).get("usage") or {}
            for key in total_usage:
                total_usage[key] += usage.get(key, 0)

        return (
            {
                "id": completion_id,
                "object": "text_completion",
                "created": created,
                "model": request.model,
                "choices": choices,
                "usage": total_usage,
            },
            None,
        )

    async def completion_stream(self, request: CompletionRequest) -> AsyncGenerator[str, None]:
        unsupported_error = self._unsupported_completion_options_error(request)
        if unsupported_error:
            yield f"data: {json.dumps({'error': unsupported_error})}\n\n"
            return

        prompt = self._completion_prompts(request.prompt)[0]
        chat_request = self._completion_to_chat_request(request, prompt)
        completion_id = f"cmpl-{uuid.uuid4()}"
        created = int(time.time())

        async for chunk_line in self.chat_completion_stream(chat_request):
            if chunk_line.strip() == "data: [DONE]":
                yield chunk_line
                return
            if not chunk_line.startswith("data: "):
                continue
            try:
                chat_chunk = json.loads(chunk_line[6:])
            except json.JSONDecodeError:
                continue
            if "error" in chat_chunk:
                yield chunk_line
                return

            choices = chat_chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            text = delta.get("content") or ""
            finish_reason = choices[0].get("finish_reason")
            if not text and finish_reason is None:
                continue
            completion_chunk = {
                "id": completion_id,
                "object": "text_completion",
                "created": created,
                "model": request.model,
                "choices": [
                    {
                        "text": text,
                        "index": 0,
                        "logprobs": None,
                        "finish_reason": finish_reason,
                    }
                ],
            }
            yield f"data: {json.dumps(completion_chunk)}\n\n"

    def _missing_openai_api_key_error(self) -> dict[str, Any]:
        return {
            "status": 501,
            "error": "Media endpoints require OPENAI_API_KEY",
            "detail": (
                "Text-to-speech is currently served by the OpenAI Speech API. "
                "Set OPENAI_API_KEY to enable real speech generation."
            ),
        }

    def _missing_media_auth_error(self) -> dict[str, Any]:
        return {
            "status": 501,
            "error": "Media endpoints require auth",
            "detail": (
                "Image generation needs CHATGPT_ACCESS_TOKEN for the Codex image_generation "
                "tool, or OPENAI_API_KEY for the OpenAI Images API fallback."
            ),
        }

    def _image_output_format(self, request: ImageGenerationRequest) -> str:
        output_format = (request.output_format or "png").strip().lower()
        return output_format or "png"

    def _image_prompt_with_options(self, request: ImageGenerationRequest) -> str:
        options = []
        if request.size:
            options.append(f"size: {request.size}")
        if request.quality:
            options.append(f"quality: {request.quality}")
        if request.background:
            options.append(f"background: {request.background}")

        if not options:
            return request.prompt

        return f"{request.prompt}\n\nImage options: {', '.join(options)}."

    def _build_codex_image_payload(self, request: ImageGenerationRequest) -> dict[str, Any]:
        output_format = self._image_output_format(request)
        return {
            "model": self._resolve_media_responses_model(request),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": self._image_prompt_with_options(request),
                        }
                    ],
                }
            ],
            "stream": True,
            "store": False,
            "instructions": (
                "Use the image_generation tool to generate exactly one image for the user's "
                "request. Do not answer with text unless image generation fails."
            ),
            "tools": [
                {
                    "type": "image_generation",
                    "output_format": output_format,
                }
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }

    async def _image_generation_via_codex(
        self, request: ImageGenerationRequest
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        n = 1 if request.n is None else request.n
        if n < 1:
            return None, {
                "status": 400,
                "error": "Invalid image count",
                "detail": "n must be at least 1",
            }

        output_format = self._image_output_format(request)
        created = int(time.time())
        data: list[dict[str, Any]] = []
        response_id = None
        response_model = None
        text_output: list[str] = []

        for _ in range(n):
            image_item: dict[str, Any] | None = None
            payload = self._build_codex_image_payload(request)

            async for event in self._codex_event_stream_from_payload(payload):
                event_type = event.get("type")

                if event_type == "error":
                    return None, event

                if event_type == "response.created":
                    response = event.get("response", {})
                    response_id = response.get("id", response_id)
                    response_model = response.get("model", response_model)
                    created = response.get("created_at", created)
                    continue

                if event_type == "response.output_text.delta":
                    delta = event.get("delta", "")
                    if delta:
                        text_output.append(delta)
                    continue

                if event_type == "response.output_item.done":
                    item = event.get("item") or {}
                    if item.get("type") == "image_generation_call":
                        image_item = item
                    continue

                if event_type == "response.completed":
                    response = event.get("response", {})
                    response_id = response.get("id", response_id)
                    response_model = response.get("model", response_model)
                    for item in response.get("output") or []:
                        if item.get("type") == "image_generation_call":
                            image_item = item

            if not image_item or not image_item.get("result"):
                return None, {
                    "status": 502,
                    "error": "Codex image generation did not return an image",
                    "detail": "".join(text_output).strip() or "No image_generation_call result was received.",
                }

            datum: dict[str, Any] = {
                "b64_json": image_item["result"],
            }
            if image_item.get("revised_prompt"):
                datum["revised_prompt"] = image_item["revised_prompt"]
            if request.response_format == "url":
                datum["url"] = f"data:image/{output_format};base64,{image_item['result']}"
            data.append(datum)

        result: dict[str, Any] = {
            "created": created,
            "data": data,
        }
        if response_id:
            result["id"] = response_id
        result["requested_model"] = request.model
        result["model"] = response_model
        result["image_model"] = None  # The tool does not disclose its underlying model ID.
        result["warnings"] = [
            "Image generation used the Codex image_generation tool; model identifies "
            "the driver, not a verified image model. requested_model is only the request label."
        ]
        if response_model:
            result["codex_model"] = response_model
        return result, None

    async def image_generation(
        self, request: ImageGenerationRequest
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if settings.chatgpt_access_token:
            codex_result, codex_error = await self._image_generation_via_codex(request)
            if codex_result is not None or not settings.openai_api_key:
                return codex_result, codex_error

        if not settings.openai_api_key:
            return None, self._missing_media_auth_error()

        payload: dict[str, Any] = {
            "model": request.model,
            "prompt": request.prompt,
        }
        if request.n is not None:
            payload["n"] = request.n
        if request.size:
            payload["size"] = request.size
        if request.quality:
            payload["quality"] = request.quality
        if request.background:
            payload["background"] = request.background
        if request.output_format:
            payload["output_format"] = request.output_format
        if request.response_format and request.model.startswith("dall-e"):
            payload["response_format"] = request.response_format

        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{self.openai_base_url}/v1/images/generations",
                headers=self._build_openai_headers(),
                json=payload,
            )

        if response.status_code >= 400:
            return None, {
                "status": response.status_code,
                "error": "OpenAI image generation failed",
                "detail": response.text,
            }

        return response.json(), None

    def _audio_media_type(self, response_format: str | None) -> str:
        normalized = (response_format or "mp3").strip().lower()
        return {
            "mp3": "audio/mpeg",
            "mpeg": "audio/mpeg",
            "opus": "audio/opus",
            "aac": "audio/aac",
            "flac": "audio/flac",
            "wav": "audio/wav",
            "pcm": "audio/pcm",
        }.get(normalized, "application/octet-stream")

    def _wav_from_pcm(self, pcm_audio: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_audio)
        return buffer.getvalue()

    def _convert_pcm_audio(
        self,
        pcm_audio: bytes,
        response_format: str,
        sample_rate: int = 24000,
        channels: int = 1,
    ) -> tuple[bytes | None, str | None, dict[str, Any] | None]:
        normalized = (response_format or "mp3").strip().lower()
        if normalized in {"pcm", "s16le"}:
            return pcm_audio, "audio/pcm", None
        if normalized == "wav":
            return self._wav_from_pcm(pcm_audio, sample_rate, channels), "audio/wav", None

        ffmpeg_format = {
            "mp3": "mp3",
            "mpeg": "mp3",
            "aac": "adts",
            "flac": "flac",
            "opus": "opus",
        }.get(normalized)
        if not ffmpeg_format:
            return None, None, {
                "status": 400,
                "error": "Unsupported realtime speech format",
                "detail": "ChatGPT realtime speech supports wav, pcm, mp3, aac, flac, and opus.",
            }

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None, None, {
                "status": 501,
                "error": "ffmpeg is required for compressed realtime speech",
                "detail": "Install ffmpeg or request response_format=wav/pcm.",
            }

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-i",
            "pipe:0",
        ]
        if normalized == "opus":
            command.extend(["-c:a", "libopus"])
        command.extend(["-f", ffmpeg_format, "pipe:1"])

        try:
            completed = subprocess.run(
                command,
                input=pcm_audio,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as error:
            return None, None, {
                "status": 502,
                "error": "Realtime speech conversion failed",
                "detail": str(error),
            }

        if completed.returncode != 0:
            return None, None, {
                "status": 502,
                "error": "Realtime speech conversion failed",
                "detail": completed.stderr.decode(errors="replace"),
            }

        return completed.stdout, self._audio_media_type(normalized), None

    def _realtime_speech_instructions(self, request: AudioSpeechRequest) -> str:
        instructions = (
            request.instructions
            or "Read the user text aloud exactly. Do not add extra words or commentary."
        ).strip()
        if request.speed and request.speed != 1.0:
            instructions = f"{instructions}\nSpeak at approximately {request.speed:.1f}x speed."
        return instructions

    async def _synthesize_speech_via_realtime(
        self, request: AudioSpeechRequest
    ) -> tuple[bytes | None, str | None, dict[str, Any] | None]:
        response_format = request.response_format or request.format or "mp3"
        realtime_model = self._resolve_realtime_model(request)
        url = f"https://api.openai.com/v1/realtime?model={realtime_model}".replace(
            "https://", "wss://", 1
        )
        voice = (request.voice or "marin").strip().lower()
        audio_chunks: list[bytes] = []
        sample_rate = 24000
        channels = 1

        try:
            async with websockets.connect(
                url,
                additional_headers=self._build_chatgpt_bearer_headers(),
                user_agent_header=None,
                proxy=None,
                open_timeout=10,
                max_size=8 * 1024 * 1024,
            ) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "session.update",
                            "session": {
                                "type": "realtime",
                                "instructions": self._realtime_speech_instructions(request),
                                "output_modalities": ["audio"],
                                "audio": {
                                    "input": {
                                        "format": {"type": "audio/pcm", "rate": sample_rate},
                                    },
                                    "output": {
                                        "format": {"type": "audio/pcm", "rate": sample_rate},
                                        "voice": voice,
                                    },
                                },
                            },
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": request.input}],
                            },
                        }
                    )
                )
                await websocket.send(json.dumps({"type": "response.create"}))

                async with asyncio.timeout(120):
                    async for raw_event in websocket:
                        event = json.loads(raw_event)
                        event_type = event.get("type")

                        if event_type == "error":
                            return None, None, {
                                "status": 502,
                                "error": "Realtime speech failed",
                                "detail": event,
                            }

                        if event_type in {
                            "response.output_audio.delta",
                            "response.audio.delta",
                            "conversation.output_audio.delta",
                        } and event.get("delta"):
                            sample_rate = int(event.get("sample_rate") or sample_rate)
                            channels = int(event.get("channels") or event.get("num_channels") or channels)
                            audio_chunks.append(base64.b64decode(event["delta"]))
                            continue

                        if event_type in {"response.done", "response.cancelled"}:
                            break
        except TimeoutError:
            return None, None, {
                "status": 504,
                "error": "Realtime speech timed out",
                "detail": "Timed out waiting for realtime audio output.",
            }
        except Exception as error:
            return None, None, {
                "status": 502,
                "error": "Realtime speech connection failed",
                "detail": str(error),
            }

        pcm_audio = b"".join(audio_chunks)
        if not pcm_audio:
            return None, None, {
                "status": 502,
                "error": "Realtime speech returned no audio",
                "detail": "No response.output_audio.delta events were received.",
            }

        return self._convert_pcm_audio(pcm_audio, response_format, sample_rate, channels)

    async def synthesize_speech(
        self, request: AudioSpeechRequest
    ) -> tuple[bytes | None, str | None, dict[str, Any] | None]:
        if settings.chatgpt_access_token:
            realtime_audio, realtime_media_type, realtime_error = await self._synthesize_speech_via_realtime(request)
            if realtime_audio is not None or not settings.openai_api_key:
                return realtime_audio, realtime_media_type, realtime_error

        if not settings.openai_api_key:
            return None, None, self._missing_openai_api_key_error()

        response_format = request.response_format or request.format or "mp3"
        payload: dict[str, Any] = {
            "model": request.model,
            "input": request.input,
            "voice": request.voice or "marin",
            "response_format": response_format,
        }
        if request.instructions:
            payload["instructions"] = request.instructions
        if request.speed is not None:
            payload["speed"] = request.speed

        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{self.openai_base_url}/v1/audio/speech",
                headers=self._build_openai_headers(),
                json=payload,
            )

        if response.status_code >= 400:
            return None, None, {
                "status": response.status_code,
                "error": "OpenAI speech generation failed",
                "detail": response.text,
            }

        return response.content, response.headers.get("content-type") or self._audio_media_type(response_format), None


bridge = ChatGPTBridge()
