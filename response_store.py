"""Bridge-side storage backing Responses ``store`` and ``previous_response_id``.

The ChatGPT/Codex subscription channel is stateless: it rejects the ``store`` parameter
and cannot resolve ``previous_response_id``. To behave like the real Responses API the
bridge keeps its own bounded, expiring, in-process record of the responses it produced
together with the conversation items that led to them. Nothing is written to disk, no
record survives a restart, and a stored record is only readable through this process.
"""
import os
import threading
import time
from collections import OrderedDict
from typing import Any


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class ResponseStore:
    """Least-recently-used, time-expiring store of bridge-produced responses."""

    def __init__(self, max_entries: int | None = None, ttl_seconds: float | None = None) -> None:
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._max_entries = (
            max_entries
            if isinstance(max_entries, int) and max_entries > 0
            else _positive_int("BRIDGE_RESPONSE_STORE_MAX", 256)
        )
        self._ttl_seconds = (
            float(ttl_seconds)
            if isinstance(ttl_seconds, (int, float)) and ttl_seconds > 0
            else _positive_float("BRIDGE_RESPONSE_STORE_TTL", 3600.0)
        )

    def _now(self) -> float:
        return time.monotonic()

    def _expired(self, entry: dict[str, Any]) -> bool:
        return entry["expires_at"] <= self._now()

    def _purge(self) -> None:
        for response_id in [key for key, entry in self._entries.items() if self._expired(entry)]:
            self._entries.pop(response_id, None)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def store(
        self,
        response: dict[str, Any],
        conversation: list[Any] | None = None,
    ) -> str | None:
        """Record a response and the items that led to it. Returns the stored id."""

        response_id = str(response.get("id") or "").strip()
        if not response_id:
            return None
        entry = {
            "response": response,
            "conversation": list(conversation or []),
            "expires_at": self._now() + self._ttl_seconds,
        }
        with self._lock:
            self._entries[response_id] = entry
            self._entries.move_to_end(response_id)
            self._purge()
        return response_id

    def get(self, response_id: str) -> dict[str, Any] | None:
        response_id = str(response_id or "").strip()
        if not response_id:
            return None
        with self._lock:
            entry = self._entries.get(response_id)
            if entry is None:
                return None
            if self._expired(entry):
                self._entries.pop(response_id, None)
                return None
            self._entries.move_to_end(response_id)
            return entry["response"]

    def conversation(self, response_id: str) -> list[Any] | None:
        """Return the ordered input items a follow-up turn must replay, or None."""

        response_id = str(response_id or "").strip()
        if not response_id:
            return None
        with self._lock:
            entry = self._entries.get(response_id)
            if entry is None:
                return None
            if self._expired(entry):
                self._entries.pop(response_id, None)
                return None
            self._entries.move_to_end(response_id)
            return list(entry["conversation"])

    def delete(self, response_id: str) -> bool:
        with self._lock:
            return self._entries.pop(str(response_id or "").strip(), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._entries)


response_store = ResponseStore()
