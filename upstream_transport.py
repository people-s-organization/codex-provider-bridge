"""Bounded, cancellation-safe SSE transport for the subscription upstream.

Generation POSTs are never automatically replayed: once submitted we cannot know
whether the upstream performed work. Clients may explicitly retry surfaced errors.
"""
import asyncio
import json
import logging
import os
import re
import weakref

import httpx

# Upstream 4xx bodies name the offending parameter or input item; clients need that to
# fix a request, while credentials and raw payload echoes must never be forwarded.
_CREDENTIAL = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)")
_DETAIL_LIMIT = 300


def positive_env(name, default):
    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def error_event(status, code, message):
    return {"type": "error", "status": status, "code": code,
            "error": message, "detail": message}


def safe_upstream_detail(body):
    """Extract a bounded, credential-free explanation from an upstream error body."""

    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return ""
    candidates = []
    if isinstance(payload, dict):
        candidates.append(payload.get("detail"))
        candidates.append(payload.get("message"))
        error = payload.get("error")
        if isinstance(error, dict):
            candidates.extend([error.get("message"), error.get("detail")])
        else:
            candidates.append(error)
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        text = "".join(character for character in candidate if character.isprintable()).strip()
        if not text:
            continue
        return _CREDENTIAL.sub("[redacted]", text)[:_DETAIL_LIMIT]
    return ""


class UpstreamTransport:
    def __init__(self):
        self._clients = weakref.WeakKeyDictionary()

    def client(self):
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None or client.is_closed:
            maximum = int(positive_env("UPSTREAM_MAX_CONNECTIONS", 32))
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=positive_env("UPSTREAM_CONNECT_TIMEOUT", 15),
                    read=positive_env("UPSTREAM_IDLE_TIMEOUT", 120),
                    write=positive_env("UPSTREAM_WRITE_TIMEOUT", 30),
                    pool=positive_env("UPSTREAM_POOL_TIMEOUT", 15),
                ),
                limits=httpx.Limits(max_connections=maximum,
                                   max_keepalive_connections=maximum),
                follow_redirects=False,
            )
            self._clients[loop] = client
        return client

    async def aclose(self):
        client = self._clients.pop(asyncio.get_running_loop(), None)
        if client is not None:
            await client.aclose()

    async def events(self, url, headers, payload):
        first_timeout = positive_env("UPSTREAM_FIRST_EVENT_TIMEOUT", 120)
        max_event_bytes = int(positive_env("UPSTREAM_MAX_EVENT_BYTES", 16 * 1024 * 1024))
        try:
            async with self.client().stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    # Never forward raw upstream pages, headers or account details, but do
                    # forward the upstream's own explanation of the rejected request.
                    detail = safe_upstream_detail(await response.aread())
                    event = error_event(response.status_code, "upstream_http_error",
                                        f"Upstream returned HTTP {response.status_code}")
                    if detail:
                        event["detail"] = detail
                        event["upstream_detail"] = detail
                    logging.getLogger(__name__).warning(
                        "upstream_rejected status=%s detail=%s",
                        response.status_code, detail or "(no structured detail)",
                    )
                    retry_after = response.headers.get("retry-after", "")
                    if retry_after.isdigit():
                        event["retry_after"] = retry_after
                    yield event
                    return
                deadline = asyncio.get_running_loop().time() + first_timeout
                first = True
                data = []
                event_name = None
                size = 0
                lines = response.aiter_lines().__aiter__()
                while True:
                    try:
                        if first:
                            remaining = deadline - asyncio.get_running_loop().time()
                            if remaining <= 0:
                                raise TimeoutError
                            line = await asyncio.wait_for(anext(lines), remaining)
                        else:
                            line = await anext(lines)
                    except StopAsyncIteration:
                        if data:
                            yield error_event(502, "upstream_truncated_event", "Upstream ended inside an SSE event")
                        return
                    if line == "":
                        if not data:
                            event_name = None
                            continue
                        raw = "\n".join(data)
                        data, size = [], 0
                        if raw == "[DONE]":
                            return
                        try:
                            event = json.loads(raw)
                            if not isinstance(event, dict):
                                raise ValueError
                        except (ValueError, TypeError):
                            yield error_event(502, "upstream_invalid_event", "Upstream returned malformed SSE JSON")
                            return
                        if "type" not in event and event_name:
                            event["type"] = event_name
                        event_name = None
                        first = False
                        if event.get("type") == "error":
                            yield error_event(502, "upstream_error", "Upstream reported a generation error")
                            return
                        if event.get("type") == "response.failed":
                            event = dict(event)
                            result = dict(event.get("response") or {})
                            result["error"] = {"code": "upstream_generation_failed", "message": "Upstream generation failed"}
                            event["response"] = result
                        yield event
                        if event.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
                            return
                    elif line.startswith("event:"):
                        event_name = line[6:].lstrip(" ")
                    elif line.startswith("data:"):
                        value = line[5:].lstrip(" ")
                        size += len(value.encode("utf-8"))
                        if size > max_event_bytes:
                            yield error_event(502, "upstream_event_too_large", "Upstream SSE event exceeded the configured limit")
                            return
                        data.append(value)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            yield error_event(504, "upstream_timeout", "Upstream timed out; generation was not automatically retried")
        except httpx.RequestError:
            yield error_event(502, "upstream_connection_error", "Upstream connection failed; generation was not automatically retried")
