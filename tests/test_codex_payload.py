import pytest

from bridge import ChatGPTBridge
from schemas import ChatCompletionRequest, ResponsesRequest


def _chat_payload(messages):
    bridge = ChatGPTBridge()
    return bridge._build_payload(
        ChatCompletionRequest(model="gpt-fixture-a", messages=messages)
    )


def _parts(payload, index):
    return payload["input"][index]["content"]


def test_assistant_turns_use_output_text():
    payload = _chat_payload(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "again"},
        ]
    )

    assert _parts(payload, 0) == [{"type": "input_text", "text": "hello"}]
    assert _parts(payload, 1) == [{"type": "output_text", "text": "hi there"}]
    assert _parts(payload, 2) == [{"type": "input_text", "text": "again"}]


def test_assistant_content_parts_are_retyped_by_role():
    payload = _chat_payload(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "plain"},
                    {"type": "input_text", "text": "wrongly typed"},
                    {"type": "output_text", "text": "already right"},
                    {"type": "refusal", "refusal": "cannot help"},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "output_text", "text": "should become input"},
                    {"type": "refusal", "refusal": "becomes text"},
                ],
            },
        ]
    )

    assert _parts(payload, 0) == [
        {"type": "output_text", "text": "plain"},
        {"type": "output_text", "text": "wrongly typed"},
        {"type": "output_text", "text": "already right"},
        {"type": "refusal", "refusal": "cannot help"},
    ]
    assert _parts(payload, 1) == [
        {"type": "input_text", "text": "should become input"},
        {"type": "input_text", "text": "becomes text"},
    ]


def test_assistant_tool_calls_are_replayed_as_function_calls():
    payload = _chat_payload(
        [
            {"role": "user", "content": "weather in Paris?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_abc", "content": "18C"},
            {"role": "user", "content": "Reply with one word: the temperature."},
        ]
    )

    assert payload["input"][1] == {
        "type": "function_call",
        "call_id": "call_abc",
        "name": "get_weather",
        "arguments": '{"city":"Paris"}',
    }
    assert payload["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_abc",
        "output": "18C",
    }


@pytest.mark.parametrize("message", [
    {"role": "assistant", "content": None, "tool_calls": [{"type": "function"}]},
    {"role": "tool", "tool_call_id": "call_missing", "content": "42"},
    {"role": "tool", "content": "42"},
])
def test_malformed_or_orphan_tool_history_is_rejected(message):
    with pytest.raises(ValueError):
        _chat_payload([{"role": "user", "content": "hello"}, message])


def test_image_parts_are_only_valid_for_user_turns():
    payload = _chat_payload(
        [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]},
            {"role": "assistant", "content": [{"type": "image_url", "image_url": "https://x/z.png"}]},
        ]
    )

    assert _parts(payload, 0) == [{"type": "input_image", "image_url": "https://x/y.png"}]
    assert _parts(payload, 1) == [{"type": "output_text", "text": "[image: https://x/z.png]"}]


def test_system_and_developer_messages_become_ordered_instructions():
    bridge = ChatGPTBridge()
    payload = bridge._build_payload(
        ChatCompletionRequest(
            model="gpt-fixture-a",
            messages=[
                {"role": "system", "content": "be brief"},
                {"role": "developer", "content": "answer in English"},
                {"role": "user", "content": "hello"},
            ],
        )
    )

    # The backend rejects system role items in input ("System messages are not allowed").
    assert payload["instructions"] == "be brief\n\nanswer in English"
    assert payload["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    ]
    assert all(item.get("role") not in {"system", "developer"} for item in payload["input"])


def test_responses_input_items_are_normalized_per_role():
    bridge = ChatGPTBridge()
    payload = bridge._build_responses_payload(
        ResponsesRequest(
            model="gpt-fixture-a",
            input=[
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
                {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_1", "output": "42"},
            ],
        )
    )

    assert payload["input"][0]["content"] == [{"type": "input_text", "text": "hi"}]
    assert payload["input"][1]["content"] == [{"type": "output_text", "text": "hello"}]
    assert payload["input"][2]["type"] == "function_call"
    assert payload["input"][3]["output"] == "42"


def test_responses_paired_tool_items_pass_through():
    bridge = ChatGPTBridge()
    call = {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{}"}
    output = {"type": "function_call_output", "call_id": "call_1", "output": "18C"}
    payload = bridge._build_responses_payload(
        ResponsesRequest(
            model="gpt-fixture-a",
            input=[
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                call,
                output,
            ],
        )
    )

    assert payload["input"][1] == call
    assert payload["input"][2] == output


def test_responses_string_input_stays_a_user_message():
    bridge = ChatGPTBridge()
    payload = bridge._build_responses_payload(ResponsesRequest(model="gpt-fixture-a", input="hi"))

    assert payload["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]
