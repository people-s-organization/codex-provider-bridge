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
    """Collects streamed Codex ``function_call`` items into OpenAI-shaped tool calls.

    Upstream announces each call with ``response.output_item.added`` (carrying the id,
    call_id and name), streams the arguments as ``response.function_call_arguments.delta``
    and finishes with ``response.output_item.done`` holding the complete arguments.
    """

    def __init__(self) -> None:
        self._calls: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def register(self, item: dict[str, Any]) -> dict[str, Any] | None:
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            return None

        call = self._calls.get(item_id)
        if call is None:
            call = {
                "id": str(item.get("call_id") or "").strip() or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": str(item.get("name") or "").strip(),
                    "arguments": "",
                },
            }
            self._calls[item_id] = call
            self._order.append(item_id)
        else:
            if item.get("call_id"):
                call["id"] = str(item["call_id"])
            if item.get("name"):
                call["function"]["name"] = str(item["name"])

        arguments = item.get("arguments")
        if isinstance(arguments, str) and arguments:
            call["function"]["arguments"] = arguments

        return call

    def append_arguments(self, item_id: str, delta: str) -> dict[str, Any] | None:
        item_id = str(item_id or "").strip()
        if not item_id:
            return None

        call = self._calls.get(item_id) or self.register({"id": item_id})
        if call is None:
            return None

        call["function"]["arguments"] += delta
        return call

    def index_of(self, item_id: str) -> int:
        item_id = str(item_id or "").strip()
        return self._order.index(item_id) if item_id in self._order else len(self._order)

    def __bool__(self) -> bool:
        return bool(self._order)

    def as_tool_calls(self) -> list[dict[str, Any]]:
        return [self._calls[item_id] for item_id in self._order]

    def as_output_items(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item_id,
                "type": "function_call",
                "status": "completed",
                "call_id": self._calls[item_id]["id"],
                "name": self._calls[item_id]["function"]["name"],
                "arguments": self._calls[item_id]["function"]["arguments"],
            }
            for item_id in self._order
        ]

    def handle_event(self, event: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """Apply one upstream event.

        Returns the affected call (if any) and a short action marker:
        ``added``, ``arguments``, ``done`` or an empty string when nothing happened.
        """

        event_type = event.get("type")

        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") != "function_call":
                return None, ""
            return self.register(item), "added"

        if event_type == "response.function_call_arguments.delta":
            delta = event.get("delta") or ""
            if not delta:
                return None, ""
            return self.append_arguments(str(event.get("item_id") or ""), str(delta)), "arguments"

        if event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") != "function_call":
                return None, ""
            return self.register(item), "done"

        return None, ""


class ChatGPTBridge:
    def __init__(self):
        self.base_url = settings.chatgpt_base_url.rstrip("/")
        self.openai_base_url = settings.openai_base_url.rstrip("/")

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.chatgpt_access_token}",
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

    def _normalize_responses_input_items(self, request_input: Any) -> Any:
        """Fix content part types and drop unpaired tool outputs on ``/v1/responses`` input.

        Two upstream rules are enforced here:

        * message content is retyped per role (assistant turns only accept ``output_text``
          / ``refusal``, user turns only accept ``input_text`` / ``input_image``);
        * a ``function_call_output`` is only sent when a ``function_call`` with the same
          ``call_id`` appeared earlier in the same input, because the backend rejects an
          orphan output with ``No tool call found for function call output``.

        Unpaired outputs degrade to a plain user text turn; other item types pass through
        untouched.
        """

        if not isinstance(request_input, list):
            return request_input

        normalized: list[Any] = []
        known_call_ids: set[str] = set()

        for item in request_input:
            if not isinstance(item, dict):
                normalized.append(item)
                continue

            item_type = str(item.get("type") or "").strip()
            if item_type == "function_call":
                call_id = str(item.get("call_id") or item.get("id") or "").strip()
                if call_id:
                    known_call_ids.add(call_id)
                normalized.append(item)
                continue

            if item_type == "function_call_output":
                call_id = str(item.get("call_id") or "").strip()
                if call_id and call_id in known_call_ids:
                    normalized.append(item)
                else:
                    normalized.append(self._tool_result_message(item.get("output"), call_id))
                continue

            role = str(item.get("role") or "").strip().lower()
            if role not in {"user", "assistant", "system", "developer"} or "content" not in item:
                normalized.append(item)
                continue

            if role in {"system", "developer"}:
                role = "user"

            normalized_item = dict(item)
            normalized_item["role"] = role
            normalized_item["content"] = self._content_to_codex_items(item.get("content"), role)
            normalized.append(normalized_item)

        return normalized

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
        Calls without an id cannot be paired, so they are not emitted.
        """

        if not isinstance(tool_calls, list):
            return []

        items: list[dict[str, Any]] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue

            call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
            if not call_id:
                continue

            function = tool_call.get("function")
            if not isinstance(function, dict):
                function = {}

            name = str(function.get("name") or tool_call.get("name") or "").strip() or "unknown_function"
            arguments = function.get("arguments", tool_call.get("arguments"))
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments, ensure_ascii=False)

            items.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments if isinstance(arguments, str) else "",
                }
            )

        return items

    def _message_to_response_input_items(
        self, message: Message, known_call_ids: set[str]
    ) -> list[dict[str, Any]]:
        if message.role == "tool":
            output = self._stringify_content(message.content)
            call_id = str(message.tool_call_id or "").strip()
            if call_id and call_id in known_call_ids:
                return [
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output or "(tool returned no output)",
                    }
                ]
            return [self._tool_result_message(output, call_id)]

        role = "assistant" if message.role == "assistant" else "user"
        content = self._content_to_codex_items(message.content, role)
        items: list[dict[str, Any]] = []

        if role == "assistant" and message.tool_calls:
            call_items = self._assistant_tool_call_items(message.tool_calls)
            if not call_items:
                content.append(
                    {
                        "type": "output_text",
                        "text": (
                            "[assistant tool_calls omitted: OpenAI tool calling is not exposed "
                            "by the Codex backend bridge]"
                        ),
                    }
                )
            if content:
                items.append({"type": "message", "role": role, "content": content})
            items.extend(call_items)
            known_call_ids.update(item["call_id"] for item in call_items)
            return items

        if content:
            items.append({"type": "message", "role": role, "content": content})
        return items

    def _extract_instructions_and_input(self, messages) -> tuple[str | None, list[dict[str, Any]]]:
        instructions = []
        input_items: list[dict[str, Any]] = []
        known_call_ids: set[str] = set()

        for message in messages:
            if message.role in {"system", "developer"}:
                instruction = self._stringify_content(message.content)
                if instruction:
                    instructions.append(instruction)
                continue

            input_items.extend(self._message_to_response_input_items(message, known_call_ids))

        compiled_instructions = "\n\n".join(part for part in instructions if part).strip() or None
        return compiled_instructions, input_items

    def _normalize_tool_definitions(self, tools: Any) -> tuple[list[dict[str, Any]], list[str]]:
        """Flatten OpenAI tool definitions into the Codex responses shape.

        Chat Completions nests the definition under ``function``; the Responses API and
        the Codex backend both use the flat ``{type, name, description, parameters}``
        shape. Only ``function`` tools are forwarded; other tool types are reported back
        so callers can tell the model they were dropped.
        """

        forwarded: list[dict[str, Any]] = []
        dropped_types: list[str] = []

        for tool in tools or []:
            if not isinstance(tool, dict):
                continue

            tool_type = str(tool.get("type") or "function").strip() or "function"
            if tool_type != "function":
                if tool_type not in dropped_types:
                    dropped_types.append(tool_type)
                continue

            function = tool.get("function")
            source = function if isinstance(function, dict) else tool

            name = str(source.get("name") or "").strip()
            if not name:
                continue

            definition: dict[str, Any] = {"type": "function", "name": name}
            description = source.get("description")
            if description:
                definition["description"] = str(description)
            parameters = source.get("parameters")
            if isinstance(parameters, dict):
                definition["parameters"] = parameters
            if source.get("strict") is not None:
                definition["strict"] = bool(source.get("strict"))

            forwarded.append(definition)

        return forwarded, dropped_types

    def _normalize_tool_choice(self, tool_choice: Any, function_call: Any = None) -> Any:
        """Map Chat Completions / Responses tool_choice onto the Codex shape."""

        choice = tool_choice if tool_choice is not None else function_call
        if choice is None:
            return None

        if isinstance(choice, str):
            value = choice.strip().lower()
            return value if value in {"auto", "none", "required"} else None

        if isinstance(choice, dict):
            function = choice.get("function")
            name = ""
            if isinstance(function, dict):
                name = str(function.get("name") or "").strip()
            name = name or str(choice.get("name") or "").strip()
            if name:
                return {"type": "function", "name": name}

        return None

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

        combined = list(tools or []) + list(legacy_functions or [])
        forwarded, dropped_types = self._normalize_tool_definitions(combined)

        if forwarded:
            payload["tools"] = forwarded
            normalized_choice = self._normalize_tool_choice(tool_choice, function_call)
            if normalized_choice is not None:
                payload["tool_choice"] = normalized_choice
            if parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = bool(parallel_tool_calls)
            return instructions

        if dropped_types:
            note = (
                "Compatibility note: only function tools can be forwarded, so these tool "
                f"types were dropped: {', '.join(dropped_types)}. Answer directly and do "
                "not claim to have executed any tool."
            )
            return f"{instructions}\n\n{note}" if instructions else note

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
        if request.tools or request.functions:
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
        if isinstance(request.input, str):
            request_input = [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": request.input}],
                }
            ]
        else:
            request_input = self._normalize_responses_input_items(request.input)

        instructions = self._augment_instructions_for_schema(
            request.instructions,
            request.text.get("format") if isinstance(request.text, dict) else None,
        )
        tool_payload: dict[str, Any] = {}
        if request.tools:
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
        full_text = []
        tool_calls = ToolCallAccumulator()
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

            if event_type in {
                "response.output_item.added",
                "response.output_item.done",
                "response.function_call_arguments.delta",
            }:
                tool_calls.handle_event(event)
                continue

            if event_type == "response.completed":
                response = event.get("response", {})
                usage = self._usage_from_response(response)
                return (
                    {
                        "id": response_id,
                        "created": created,
                        "model": request.model,
                        "content": "".join(full_text),
                        "tool_calls": tool_calls.as_tool_calls(),
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
                "tool_calls": tool_calls.as_tool_calls(),
                "usage": usage,
            },
            None,
        )

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncGenerator[str, None]:
        response_id = f"chatcmpl-{uuid.uuid4()}"
        created = int(time.time())
        include_usage = bool((request.stream_options or {}).get("include_usage"))
        sent_role = False
        tool_calls = ToolCallAccumulator()

        async for event in self._codex_event_stream(request):
            event_type = event.get("type")

            if event_type == "error":
                yield f"data: {json.dumps({'error': event})}\n\n"
                return

            if event_type == "response.created":
                response = event.get("response", {})
                response_id = response.get("id", response_id)
                created = response.get("created_at", created)
                chunk = self._chat_stream_chunk(
                    response_id=response_id,
                    created=created,
                    model=request.model,
                    delta={"role": "assistant"},
                    include_usage=include_usage,
                )
                yield f"data: {json.dumps(chunk)}\n\n"
                sent_role = True
                continue

            if event_type == "response.output_text.delta":
                delta = event.get("delta", "")
                if not delta:
                    continue

                if not sent_role:
                    role_chunk = self._chat_stream_chunk(
                        response_id=response_id,
                        created=created,
                        model=request.model,
                        delta={"role": "assistant"},
                        include_usage=include_usage,
                    )
                    yield f"data: {json.dumps(role_chunk)}\n\n"
                    sent_role = True

                chunk = self._chat_stream_chunk(
                    response_id=response_id,
                    created=created,
                    model=request.model,
                    delta={"content": delta},
                    include_usage=include_usage,
                )
                yield f"data: {json.dumps(chunk)}\n\n"
                continue

            if event_type in {
                "response.output_item.added",
                "response.output_item.done",
                "response.function_call_arguments.delta",
            }:
                call, action = tool_calls.handle_event(event)
                if call is None or action == "done":
                    continue

                if not sent_role:
                    role_chunk = self._chat_stream_chunk(
                        response_id=response_id,
                        created=created,
                        model=request.model,
                        delta={"role": "assistant"},
                        include_usage=include_usage,
                    )
                    yield f"data: {json.dumps(role_chunk)}\n\n"
                    sent_role = True

                index = tool_calls.index_of(str(event.get("item_id") or (event.get("item") or {}).get("id") or ""))
                if action == "added":
                    tool_delta = {
                        "index": index,
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["function"]["name"], "arguments": ""},
                    }
                else:
                    tool_delta = {
                        "index": index,
                        "function": {"arguments": str(event.get("delta") or "")},
                    }

                chunk = self._chat_stream_chunk(
                    response_id=response_id,
                    created=created,
                    model=request.model,
                    delta={"tool_calls": [tool_delta]},
                    include_usage=include_usage,
                )
                yield f"data: {json.dumps(chunk)}\n\n"
                continue

            if event_type == "response.completed":
                response = event.get("response", {})
                usage = self._usage_from_response(response)
                if not sent_role:
                    role_chunk = self._chat_stream_chunk(
                        response_id=response_id,
                        created=created,
                        model=request.model,
                        delta={"role": "assistant"},
                        include_usage=include_usage,
                    )
                    yield f"data: {json.dumps(role_chunk)}\n\n"
                chunk = self._chat_stream_chunk(
                    response_id=response_id,
                    created=created,
                    model=request.model,
                    delta={},
                    finish_reason="tool_calls" if tool_calls else "stop",
                    include_usage=include_usage,
                )
                yield f"data: {json.dumps(chunk)}\n\n"
                if include_usage:
                    usage_chunk = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": request.model,
                        "choices": [],
                        "usage": usage,
                    }
                    yield f"data: {json.dumps(usage_chunk)}\n\n"
                yield "data: [DONE]\n\n"
                return

    async def responses(self, request: ResponsesRequest) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        response_id = f"resp_{uuid.uuid4().hex}"
        created = int(time.time())
        full_text: list[str] = []
        tool_calls = ToolCallAccumulator()
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

        async for event in self._codex_event_stream_from_payload(self._build_responses_payload(request)):
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

            if event_type in {
                "response.output_item.added",
                "response.output_item.done",
                "response.function_call_arguments.delta",
            }:
                tool_calls.handle_event(event)
                continue

            if event_type == "response.completed":
                response = event.get("response", {})
                raw_usage = response.get("usage") or {}
                usage = {
                    "input_tokens": raw_usage.get("input_tokens", 0),
                    "output_tokens": raw_usage.get("output_tokens", 0),
                    "total_tokens": raw_usage.get("total_tokens", 0),
                }

        output_text = "".join(full_text)
        output: list[dict[str, Any]] = []
        if output_text:
            output.append(
                {
                    "id": f"msg_{uuid.uuid4().hex}",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": output_text,
                            "annotations": [],
                        }
                    ],
                }
            )
        output.extend(tool_calls.as_output_items())

        return (
            {
                "id": response_id,
                "object": "response",
                "created_at": created,
                "status": "completed",
                "model": request.model,
                "output_text": output_text,
                "output": output,
                "usage": usage,
            },
            None,
        )

    async def responses_stream(self, request: ResponsesRequest) -> AsyncGenerator[str, None]:
        async for event in self._codex_event_stream_from_payload(self._build_responses_payload(request)):
            event_type = event.get("type") or "response.event"
            if event_type == "error":
                yield f"event: error\ndata: {json.dumps(event)}\n\n"
                return
            yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"

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
        result["model"] = request.model
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
