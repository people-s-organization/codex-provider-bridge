import copy
import json
import os
import re
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def validate_function_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("function name must contain 1-64 letters, digits, underscores or hyphens")
    return name


def validate_parameters(schema):
    if not isinstance(schema, dict):
        raise ValueError("function parameters must be a JSON Schema object")
    # Validate schema structure without imposing strict-mode constraints on ordinary tools.
    def check(node):
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise ValueError("invalid JSON Schema: schema nodes must be objects or booleans")
        types = node.get("type")
        if types is not None:
            values = types if isinstance(types, list) else [types]
            if not values or any(not isinstance(t, str) or t not in {"object", "array", "string", "number", "integer", "boolean", "null"} for t in values):
                raise ValueError("invalid JSON Schema type")
        for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
            if key in node:
                if not isinstance(node[key], dict):
                    raise ValueError(f"invalid JSON Schema {key}")
                for child in node[key].values():
                    check(child)
        for key in ("items", "additionalProperties", "not", "if", "then", "else", "contains", "propertyNames", "unevaluatedProperties"):
            if key in node:
                check(node[key])
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            if key in node:
                if not isinstance(node[key], list) or (key != "prefixItems" and not node[key]):
                    raise ValueError(f"invalid JSON Schema {key}")
                for child in node[key]:
                    check(child)
        if "required" in node:
            required = node["required"]
            if not isinstance(required, list) or any(not isinstance(k, str) for k in required) or len(set(required)) != len(required):
                raise ValueError("invalid JSON Schema required")
        if "$ref" in node and not isinstance(node["$ref"], str):
            raise ValueError("invalid JSON Schema $ref")
        for key in ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties", "minContains", "maxContains"):
            if key in node and (type(node[key]) is not int or node[key] < 0):
                raise ValueError(f"invalid JSON Schema {key}")
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
            if key in node and (type(node[key]) not in (int, float) or (key == "multipleOf" and node[key] <= 0)):
                raise ValueError(f"invalid JSON Schema {key}")
        if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
            raise ValueError("invalid JSON Schema enum")
        if "uniqueItems" in node and not isinstance(node["uniqueItems"], bool):
            raise ValueError("invalid JSON Schema uniqueItems")
        for key in ("pattern", "format", "$id", "$schema", "title", "description"):
            if key in node and not isinstance(node[key], str):
                raise ValueError(f"invalid JSON Schema {key}")
    check(schema)
    if schema.get("type") not in (None, "object"):
        raise ValueError("function parameters must describe an object")


def normalize_tools(tools):
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ValueError("tools must be an array")
    result, names = [], set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("only client-executed function tools are supported; tool type must be function")
        source = tool.get("function", tool)
        if not isinstance(source, dict):
            raise ValueError("tool function must be an object")
        allowed = {"name", "description", "parameters", "strict"}
        if "function" in tool:
            if set(tool) - {"type", "function"} or set(source) - allowed:
                raise ValueError("invalid nested function tool shape")
        elif set(source) - (allowed | {"type"}):
            raise ValueError("invalid function tool shape")
        name = validate_function_name(source.get("name"))
        if name in names:
            raise ValueError(f"duplicate function name: {name}")
        names.add(name)
        definition = {"type": "function", "name": name}
        for key in ("description", "parameters", "strict"):
            if key not in source:
                continue
            value = source[key]
            if key == "description" and not isinstance(value, str):
                raise ValueError("function description must be a string")
            if key == "strict" and value is not None and not isinstance(value, bool):
                raise ValueError("function strict must be a boolean or null")
            if key == "parameters":
                validate_parameters(value)
            definition[key] = copy.deepcopy(value)
        result.append(definition)
    return result


def normalize_choice(choice, legacy=None):
    if choice is not None and legacy is not None:
        raise ValueError("tool_choice and function_call cannot both be specified")
    value = choice if choice is not None else legacy
    if value is None:
        return None
    if isinstance(value, str) and value in {"auto", "none", "required"}:
        return value
    if isinstance(value, dict):
        if legacy is not None and set(value) == {"name"}:
            name = value["name"]
        elif value.get("type") == "function" and set(value) == {"type", "name"}:
            name = value["name"]
        elif value.get("type") == "function" and set(value) == {"type", "function"} and isinstance(value["function"], dict) and set(value["function"]) == {"name"}:
            name = value["function"]["name"]
        else:
            raise ValueError("invalid function tool_choice shape")
        return {"type": "function", "name": validate_function_name(name)}
    raise ValueError("tool_choice must be auto, none, required, or a named function")


def validate_call(call):
    call_id = call.get("call_id")
    if not isinstance(call_id, str) or not call_id.strip():
        raise ValueError("function call requires a non-empty call_id")
    validate_function_name(call.get("name"))
    arguments = call.get("arguments")
    if not isinstance(arguments, str):
        raise ValueError("function call arguments must be a JSON object string")
    try:
        parsed = json.loads(arguments, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError):
        raise ValueError("function call arguments must be a JSON object string") from None
    if not isinstance(parsed, dict):
        raise ValueError("function call arguments must be a JSON object string")


def validate_response_history(items):
    pending, seen = set(), set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("history items must be objects")
        kind = item.get("type")
        if "role" in item and item["role"] not in {"system", "developer", "user", "assistant"}:
            raise ValueError("unsupported Responses message role")
        if kind in {None, "message"} and "role" not in item:
            raise ValueError("message history requires a role")
        if kind == "function_call":
            validate_call(item)
            call_id = item["call_id"]
            if call_id in seen:
                raise ValueError("duplicate function call id")
            pending.add(call_id)
            seen.add(call_id)
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ValueError("orphan or duplicate function call output")
            if "output" not in item or not isinstance(item["output"], (str, list)):
                raise ValueError("function call output must be a string or content array")
            pending.remove(call_id)
    if pending:
        raise ValueError("function calls require matching outputs before a new model turn")


REASONING_EFFORT_ALIASES = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "x-high": "xhigh",
    "extra high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
}


def normalize_reasoning_effort(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None

    normalized = value.strip().lower()
    if not normalized:
        return None

    normalized = " ".join(normalized.replace("_", " ").replace("-", " ").split())
    canonical = REASONING_EFFORT_ALIASES.get(normalized)
    if canonical:
        return canonical

    raise ValueError("reasoning_effort must be one of: low, medium, high, xhigh (extra high)")

class OpenAICompatModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class GenerationRequest(OpenAICompatModel):
    def compatibility_warnings(self) -> List[str]:
        ignored = {"max_tokens", "max_completion_tokens", "max_output_tokens", "truncation", "temperature", "top_p", "stop", "presence_penalty", "frequency_penalty", "logit_bias", "seed", "user", "service_tier", "metadata", "logprobs", "top_logprobs", "best_of", "suffix", "echo"}
        return [f"{key} is ignored by the Codex bridge" for key in sorted(self.model_fields_set & ignored) if getattr(self, key, None) is not None]

    @model_validator(mode="after")
    def validate_compatibility(self):
        if getattr(self, "previous_response_id", None) is not None:
            raise ValueError("previous_response_id is unavailable; supply complete history")
        if getattr(self, "store", None) is True:
            raise ValueError("store=true is unavailable; the bridge does not persist responses")
        if os.getenv("BRIDGE_STRICT_COMPATIBILITY", "").strip().lower() in {"1", "true", "yes", "on"}:
            warnings = self.compatibility_warnings()
            if warnings:
                raise ValueError("; ".join(warnings))
        tools = getattr(self, "tools", None)
        functions = getattr(self, "functions", None)
        if tools is not None and functions is not None:
            raise ValueError("tools and legacy functions cannot both be specified")
        definitions = normalize_tools(tools if tools is not None else [{"type": "function", "function": f} for f in (functions or [])])
        choice = normalize_choice(getattr(self, "tool_choice", None), getattr(self, "function_call", None))
        if isinstance(choice, dict) and choice["name"] not in {t["name"] for t in definitions}:
            raise ValueError("tool_choice names an undefined function")
        if choice == "required" and not definitions:
            raise ValueError("tool_choice required needs at least one function")
        if hasattr(self, "messages"):
            validate_response_history(chat_history_items(self.messages))
        elif isinstance(getattr(self, "input", None), list):
            validate_response_history(self.input)
        return self


class ExplicitModelRequest(OpenAICompatModel):
    """Request that must name a model itself: the bridge never guesses one."""

    @field_validator("model", check_fields=False)
    @classmethod
    def validate_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model must be a non-empty model id")
        return normalized


MessageContent = Optional[Union[str, Dict[str, Any], List[Union[str, Dict[str, Any]]]]]


class Message(OpenAICompatModel):
    role: str
    content: MessageContent = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    function_call: Optional[Dict[str, Any]] = None
    refusal: Optional[str] = None
    audio: Optional[Dict[str, Any]] = None


class ReasoningConfig(OpenAICompatModel):
    effort: Optional[str] = None

    @field_validator("effort")
    @classmethod
    def validate_effort(cls, value: Optional[str]) -> Optional[str]:
        return normalize_reasoning_effort(value)


def chat_history_items(messages):
    """Validate replay and allocate legacy pairing IDs only in the internal history."""
    items = []
    legacy_pending = {}
    occupied = {c.get("id") for m in messages for c in (m.tool_calls or []) if isinstance(c, dict) and isinstance(c.get("id"), str)}
    for index, message in enumerate(messages):
        message_start = len(items)
        if message.role not in {"system", "developer", "user", "assistant", "tool", "function"}:
            raise ValueError("unsupported message role")
        if message.role != "assistant" and (message.tool_calls is not None or message.function_call is not None):
            raise ValueError("only assistant messages may contain function calls")
        if message.tool_calls is not None and message.function_call is not None:
            raise ValueError("assistant tool_calls and function_call cannot both be specified")
        if message.role == "assistant":
            for call in message.tool_calls or []:
                # Clients routinely replay history without repeating the constant
                # "type": "function"; only an explicit different type is an error.
                if (call.get("type") or "function") != "function" or not isinstance(call.get("function"), dict):
                    raise ValueError("assistant tool_calls must contain function objects")
                function = call["function"]
                item = {"type": "function_call", "call_id": call.get("id"), "name": function.get("name"), "arguments": function.get("arguments")}
                validate_call(item)
                items.append(item)
            if message.function_call is not None:
                function = message.function_call
                name = validate_function_name(function.get("name"))
                if name in legacy_pending:
                    raise ValueError("ambiguous legacy function calls with the same name")
                call_id = f"bridge_legacy_{index}"
                while call_id in occupied:
                    call_id += "_"
                occupied.add(call_id)
                legacy_pending[name] = call_id
                item = {"type": "function_call", "call_id": call_id, "name": name, "arguments": function.get("arguments")}
                validate_call(item)
                items.append(item)
        elif message.role == "function":
            name = validate_function_name(message.name)
            if name not in legacy_pending:
                raise ValueError("orphan legacy function result")
            items.append({"type": "function_call_output", "call_id": legacy_pending.pop(name), "output": message.content if isinstance(message.content, str) else json.dumps(message.content)})
        elif message.role == "tool":
            items.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": message.content if isinstance(message.content, str) else json.dumps(message.content)})
        if message.role not in {"tool", "function"}:
            item = message.model_dump(exclude_none=True)
            for key in ("tool_calls", "function_call"):
                item.pop(key, None)
            if message.content is not None or set(item) - {"role", "name"}:
                item["type"] = "message"
                items.insert(message_start, item)
    return items


class ChatCompletionRequest(GenerationRequest):
    model: str
    messages: List[Message]
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    reasoning_effort: Optional[str] = None
    reasoning: Optional[ReasoningConfig] = None
    max_completion_tokens: Optional[int] = None
    response_format: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    functions: Optional[List[Dict[str, Any]]] = None
    function_call: Optional[Union[str, Dict[str, Any]]] = None
    parallel_tool_calls: Optional[bool] = None
    modalities: Optional[List[str]] = None
    audio: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    store: Optional[bool] = None
    seed: Optional[int] = None
    service_tier: Optional[str] = None
    stream_options: Optional[Dict[str, Any]] = None

    @field_validator("reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: Optional[str]) -> Optional[str]:
        return normalize_reasoning_effort(value)


class ResponsesRequest(GenerationRequest):
    model: str
    input: Union[str, List[Dict[str, Any]]]
    stream: Optional[bool] = False
    store: Optional[bool] = False
    instructions: Optional[str] = None
    text: Optional[Dict[str, Any]] = None
    reasoning: Optional[ReasoningConfig] = None
    max_output_tokens: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    parallel_tool_calls: Optional[bool] = None
    previous_response_id: Optional[str] = None
    truncation: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    user: Optional[str] = None


class ImageGenerationRequest(ExplicitModelRequest):
    model: str = Field(min_length=1)
    prompt: str
    n: Optional[int] = 1
    size: Optional[str] = "1024x1024"
    quality: Optional[str] = "auto"
    background: Optional[str] = None
    output_format: Optional[str] = None
    response_format: Optional[str] = "b64_json"
    metadata: Optional[Dict[str, Any]] = None
    moderation: Optional[str] = None
    output_compression: Optional[int] = None
    partial_images: Optional[int] = None
    style: Optional[str] = None
    user: Optional[str] = None


class AudioSpeechRequest(ExplicitModelRequest):
    model: str = Field(min_length=1)
    input: str
    voice: Optional[str] = "marin"
    instructions: Optional[str] = None
    response_format: Optional[str] = None
    format: Optional[str] = "mp3"
    speed: Optional[float] = 1.0
    metadata: Optional[Dict[str, Any]] = None


class CompletionRequest(GenerationRequest):
    model: str
    prompt: Optional[Union[str, List[str], List[int], List[List[int]]]] = ""
    suffix: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stream: Optional[bool] = False
    logprobs: Optional[int] = None
    echo: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    best_of: Optional[int] = 1
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
