from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, field_validator


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


class ChatCompletionRequest(OpenAICompatModel):
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


class ResponsesRequest(OpenAICompatModel):
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


class ImageGenerationRequest(OpenAICompatModel):
    model: str = "gpt-image-2"
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


class AudioSpeechRequest(OpenAICompatModel):
    model: str = "gpt-4o-mini-tts"
    input: str
    voice: Optional[str] = "marin"
    instructions: Optional[str] = None
    response_format: Optional[str] = None
    format: Optional[str] = "mp3"
    speed: Optional[float] = 1.0
    metadata: Optional[Dict[str, Any]] = None


class CompletionRequest(OpenAICompatModel):
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
