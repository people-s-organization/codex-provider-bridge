"""Reconcile Responses events without conflating item IDs and tool call IDs."""
from copy import deepcopy
from typing import Any


class EventAssembly:
    TERMINAL = {"response.completed", "response.failed", "response.incomplete"}

    def __init__(self):
        self.items: dict[Any, dict] = {}
        self.ids: dict[str, Any] = {}
        self.seen = set()
        self.response = {}
        self.terminal = None
        self.error = None

    @staticmethod
    def failure(message, code="upstream_stream_error"):
        return {"type": "error", "status": 502, "error": message, "code": code}

    def _key(self, event, item=None):
        item = item or {}
        identity = item.get("id") or event.get("item_id")
        index = event.get("output_index")
        if identity in self.ids:
            key = self.ids[identity]
            if isinstance(index, int) and key != index and index not in self.items:
                self.items[index] = {**self.items.pop(key, {}), **self.items.get(index, {})}
                self.ids = {i: index if k == key else k for i, k in self.ids.items()}
                key = index
        elif isinstance(index, int):
            existing_id = self.items.get(index, {}).get("id")
            key = f"id:{identity}" if identity and existing_id and existing_id != identity else index
        elif identity:
            key = f"id:{identity}"
        else:
            key = "text:default"
        if identity:
            self.ids[identity] = key
        return key

    def _merge(self, key, incoming, added=False):
        old = self.items.setdefault(key, {})
        incoming = deepcopy(incoming)
        # An empty added snapshot must not erase previously received arguments/content.
        if added and incoming.get("arguments") == "" and old.get("arguments"):
            incoming.pop("arguments")
        if added and incoming.get("content") == [] and old.get("content"):
            incoming.pop("content")
        old.update(incoming)
        return old

    def feed(self, event):
        seq = event.get("sequence_number")
        if seq is not None:
            if seq in self.seen:
                return False
            self.seen.add(seq)
        kind = event.get("type", "")
        for field in ("content_index", "summary_index"):
            index = event.get(field, 0)
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 4096:
                self.error = self.failure("Invalid upstream content index", "invalid_upstream_event")
                return True
        if kind == "error":
            self.error = event
            return True
        if isinstance(event.get("response"), dict):
            self.response.update(deepcopy(event["response"]))
        if kind in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item") or {}
            self._merge(self._key(event, item), item, added=kind.endswith(".added"))
        elif kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            item = self.items.setdefault(self._key(event), {"type": "function_call"})
            item.setdefault("type", "function_call")
            if event.get("item_id"):
                item.setdefault("id", event["item_id"])
            if kind.endswith(".delta"):
                item["arguments"] = item.get("arguments", "") + (event.get("delta") or "")
            elif isinstance(event.get("arguments"), str):
                item["arguments"] = event["arguments"]
        elif kind in {"response.output_text.delta", "response.output_text.done", "response.content_part.added", "response.content_part.done"}:
            item = self.items.setdefault(self._key(event), {"type": "message", "role": "assistant"})
            item.setdefault("type", "message")
            if event.get("item_id"):
                item.setdefault("id", event["item_id"])
            item.setdefault("role", "assistant")
            parts = item.setdefault("content", [])
            index = event.get("content_index", 0)
            while len(parts) <= index:
                parts.append({})
            if "part" in event:
                part = deepcopy(event["part"])
                if part.get("text") == "" and parts[index].get("text"):
                    part.pop("text")
                parts[index].update(part)
            else:
                part = parts[index]
                part.setdefault("type", "output_text")
                part.setdefault("annotations", [])
                if kind.endswith(".delta"):
                    part["text"] = part.get("text", "") + (event.get("delta") or "")
                else:
                    part["text"] = event.get("text", part.get("text", ""))
        elif kind in {"response.refusal.delta", "response.refusal.done"}:
            item = self.items.setdefault(self._key(event), {"type": "message", "role": "assistant"})
            if event.get("item_id"):
                item.setdefault("id", event["item_id"])
            parts = item.setdefault("content", [])
            index = event.get("content_index", 0)
            while len(parts) <= index:
                parts.append({})
            part = parts[index]
            part["type"] = "refusal"
            if kind.endswith(".delta"):
                part["refusal"] = part.get("refusal", "") + (event.get("delta") or "")
            else:
                part["refusal"] = event.get("refusal", part.get("refusal", ""))
        elif kind in {"response.reasoning_summary_part.added", "response.reasoning_summary_part.done", "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done"}:
            item = self.items.setdefault(self._key(event), {"type": "reasoning"})
            if event.get("item_id"):
                item.setdefault("id", event["item_id"])
            parts = item.setdefault("summary", [])
            index = event.get("summary_index", 0)
            while len(parts) <= index:
                parts.append({"type": "summary_text", "text": ""})
            if "part" in event:
                part = deepcopy(event["part"])
                if part.get("text") == "" and parts[index].get("text"):
                    part.pop("text")
                parts[index].update(part)
            elif kind.endswith(".delta"):
                parts[index]["text"] += event.get("delta") or ""
            else:
                parts[index]["text"] = event.get("text", parts[index]["text"])
        elif kind == "response.output_text.annotation.added":
            item = self.items.setdefault(self._key(event), {"type": "message", "role": "assistant"})
            parts = item.setdefault("content", [])
            index = event.get("content_index", 0)
            while len(parts) <= index:
                parts.append({"type": "output_text", "text": "", "annotations": []})
            annotations = parts[index].setdefault("annotations", [])
            annotation = deepcopy(event.get("annotation") or {})
            if annotation not in annotations:
                annotations.append(annotation)
        if kind in self.TERMINAL:
            self.terminal = kind
            output = (event.get("response") or {}).get("output")
            if output:
                # Terminal output is authoritative, including ordering and metadata.
                self.items = {i: deepcopy(item) for i, item in enumerate(output)}
                self.ids = {item["id"]: i for i, item in self.items.items() if item.get("id")}
            status = self.response.get("status")
            if kind == "response.failed" or status == "failed":
                self.error = self.failure(self.response.get("error") or "Upstream response failed", "upstream_response_failed")
            self.response["status"] = (event.get("response") or {}).get("status") or kind.rsplit(".", 1)[-1]
            for item in self.items.values():
                if item.get("type") in {"message", "function_call"}:
                    if item.get("status") == "in_progress":
                        item["status"] = self.response["status"]
            self.response["output"] = self.output()
            if not self.error and any(item.get("type") == "function_call" and (not item.get("call_id") or not item.get("name")) for item in self.items.values()):
                self.error = self.failure("Upstream function call is missing call_id or name", "invalid_upstream_tool_call")
        return True

    def output(self):
        keys = sorted(self.items, key=lambda k: (0, k) if isinstance(k, int) else (1, list(self.items).index(k)))
        return [deepcopy(self.items[k]) for k in keys]

    def text(self):
        return "".join(part.get("text", "") for item in self.output() if item.get("type") == "message" for part in item.get("content", []) if part.get("type") == "output_text")

    def calls(self):
        return [{"id": item["call_id"], "type": "function", "function": {"name": item["name"], "arguments": item.get("arguments", "")}} for item in self.output() if item.get("type") == "function_call" and item.get("call_id") and item.get("name")]

    def chat_error(self, legacy=False):
        if self.error:
            return self.error
        if any(part.get("type") == "refusal" for item in self.output() for part in item.get("content", [])):
            return self.failure("Upstream refused the request", "upstream_refusal")
        calls = [item for item in self.output() if item.get("type") == "function_call"]
        if legacy and len(calls) > 1:
            return self.failure("Legacy functions cannot represent multiple function calls", "unsupported_multiple_legacy_calls")
        if self.terminal:
            if any(not item.get("call_id") or not item.get("name") for item in calls):
                return self.failure("Upstream function call is missing call_id or name", "invalid_upstream_tool_call")
            if self.response.get("status") == "incomplete" or self.terminal == "response.incomplete":
                reason = (self.response.get("incomplete_details") or {}).get("reason")
                if reason not in {"max_output_tokens", "max_tokens", "length"}:
                    return self.failure("Upstream response incomplete: " + str(reason or "unknown"), "upstream_response_incomplete")
        return None

    def finish_reason(self, legacy=False):
        if self.terminal == "response.incomplete" or self.response.get("status") == "incomplete":
            return "length"
        return ("function_call" if legacy else "tool_calls") if self.calls() else "stop"
