from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, field_validator


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

class Message(BaseModel):
    role: str
    content: str
    name: Optional[str] = None


class ReasoningConfig(BaseModel):
    effort: Optional[str] = None

    @field_validator("effort")
    @classmethod
    def validate_effort(cls, value: Optional[str]) -> Optional[str]:
        return normalize_reasoning_effort(value)


class ChatCompletionRequest(BaseModel):
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

    @field_validator("reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: Optional[str]) -> Optional[str]:
        return normalize_reasoning_effort(value)
