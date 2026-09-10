"""Tool-schema compatibility with real harness traffic (DeepSeek Harness / pi-ai).

pi-ai serialises every harness tool as a flat ``{"type": "function", ...}`` definition
and sends ``strict: false`` unless a tool explicitly asks for JSON-schema constrained
sampling. The backend rejects ``strict: true`` unless every object node forbids extra
properties and requires every declared property, so the bridge downgrades instead of
failing the whole request.
"""
import pytest

from bridge import ChatGPTBridge
from schemas import ChatCompletionRequest

OBJECT = {"type": "object", "properties": {}, "required": []}


def chat(tools, **kwargs):
    return ChatCompletionRequest(
        model="gpt-fixture-a", messages=[{"role": "user", "content": "hi"}], tools=tools, **kwargs
    )


def nested_todo_tool(strict=None):
    tool = {
        "type": "function",
        "name": "todo_write",
        "description": "write todos",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
    }
    if strict is not None:
        tool["strict"] = strict
    return tool


def test_harness_style_tools_are_forwarded_unchanged():
    payload = ChatGPTBridge()._build_payload(chat([nested_todo_tool(strict=False)]))
    assert payload["tools"] == [nested_todo_tool(strict=False)]


def test_strict_is_kept_when_the_schema_already_qualifies():
    request = chat([nested_todo_tool(strict=True)])
    payload = ChatGPTBridge()._build_payload(request)
    assert payload["tools"][0]["strict"] is True
    assert request.compatibility_warnings() == []


def test_strict_is_dropped_with_a_warning_when_the_schema_cannot_be_strict():
    tool = nested_todo_tool(strict=True)
    # A property missing from required makes the backend reject the entire request.
    tool["parameters"]["properties"]["note"] = {"type": "string"}
    request = chat([tool])
    payload = ChatGPTBridge()._build_payload(request)

    assert payload["tools"][0]["name"] == "todo_write"
    assert "strict" not in payload["tools"][0]
    assert payload["tools"][0]["parameters"]["properties"]["note"] == {"type": "string"}
    warnings = request.compatibility_warnings()
    assert len(warnings) == 1 and "strict mode was dropped for tool 'todo_write'" in warnings[0]


def test_strict_is_dropped_when_a_nested_object_allows_extra_properties():
    tool = nested_todo_tool(strict=True)
    tool["parameters"]["properties"]["todos"]["items"]["additionalProperties"] = True
    request = chat([tool])
    payload = ChatGPTBridge()._build_payload(request)
    assert "strict" not in payload["tools"][0]
    assert any("strict mode was dropped" in warning for warning in request.compatibility_warnings())


def test_responses_tools_get_the_same_strict_treatment():
    from schemas import ResponsesRequest

    tool = {k: v for k, v in nested_todo_tool(strict=True).items() if k != "type"}
    tool["parameters"] = dict(tool["parameters"])
    tool["parameters"]["required"] = []
    request = ResponsesRequest(model="gpt-fixture-a", input="hi", tools=[{"type": "function", **tool}])
    payload = ChatGPTBridge()._build_responses_payload(request)
    assert "strict" not in payload["tools"][0]
    assert any("strict mode was dropped" in warning for warning in request.compatibility_warnings())


@pytest.mark.parametrize("keywords", [
    {"oneOf": [{"type": "string"}, {"type": "number"}]},
    {"const": "fixed"},
    {"enum": ["a", "b"]},
    {"type": ["string", "null"]},
    {"default": "d", "examples": ["a"], "title": "X"},
])
def test_schema_keywords_the_harness_emits_are_forwarded(keywords):
    tool = {
        "type": "function",
        "name": "annotated",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"field": keywords},
            "required": ["field"],
        },
    }
    payload = ChatGPTBridge()._build_payload(chat([tool]))
    assert payload["tools"][0]["parameters"]["properties"]["field"] == keywords


def test_a_full_harness_sized_tool_list_is_forwarded():
    names = [
        "bash", "todo_write", "read", "write", "edit", "glob", "grep", "web_search",
        "web_fetch", "present", "ask_user_question", "create_goal", "get_goal", "update_goal",
        "subagent", "subagent_fork", "send_message", "list_agents", "interrupt_agent",
        "job_list", "job_output", "job_kill", "skill", "workflow", "ralph", "read_image",
        "exit_plan_mode",
    ]
    tools = [
        {"type": "function", "name": name, "description": name,
         "parameters": {"type": "object", "properties": {"input": {"type": "string"}},
                        "required": ["input"], "additionalProperties": False},
         "strict": False}
        for name in names
    ]
    payload = ChatGPTBridge()._build_payload(chat(tools))
    assert [tool["name"] for tool in payload["tools"]] == names
    assert all(tool["strict"] is False for tool in payload["tools"])
