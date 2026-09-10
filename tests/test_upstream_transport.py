import asyncio
import json

import httpx

from upstream_transport import UpstreamTransport


def run_response(response):
    async def run():
        transport = UpstreamTransport()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
            transport._clients[asyncio.get_running_loop()] = client
            return [event async for event in transport.events('https://example.test/responses', {}, {})]
    return asyncio.run(run())


def test_multiline_sse_and_terminal():
    events = run_response(httpx.Response(200, text='event: response.completed\ndata: {"response":\ndata: {"output":[]}}\n\n'))
    assert events == [{'type': 'response.completed', 'response': {'output': []}}]


def test_http_error_retains_status_without_secret():
    events = run_response(httpx.Response(429, text='secret access token', headers={'retry-after':'2'}))
    assert events[0]['status'] == 429
    assert events[0]['retry_after'] == '2'
    assert 'secret' not in json.dumps(events)


def test_invalid_json_not_silently_ignored():
    assert run_response(httpx.Response(200, text='data: broken\n\n'))[0]['code'] == 'upstream_invalid_event'


def test_partial_event_errors():
    assert run_response(httpx.Response(200, text='data: {"type":"response.completed"}'))[0]['code'] == 'upstream_truncated_event'


def test_event_size_limit(monkeypatch):
    monkeypatch.setenv('UPSTREAM_MAX_EVENT_BYTES', '4')
    assert run_response(httpx.Response(200, text='data: 12345\n\n'))[0]['code'] == 'upstream_event_too_large'


def test_connection_failure_no_retry():
    async def run():
        calls = []
        def fail(request):
            calls.append(request)
            raise httpx.ConnectError('secret', request=request)
        transport = UpstreamTransport()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            transport._clients[asyncio.get_running_loop()] = client
            events = [e async for e in transport.events('https://example.test', {}, {})]
        assert len(calls) == 1
        assert events[0]['status'] == 502
        assert 'secret' not in json.dumps(events)
    asyncio.run(run())


def test_upstream_explanation_is_surfaced():
    events = run_response(httpx.Response(400, json={"detail": "Unsupported parameter: max_output_tokens"}))
    assert events[0]["status"] == 400
    assert events[0]["detail"] == "Unsupported parameter: max_output_tokens"
    assert events[0]["upstream_detail"] == "Unsupported parameter: max_output_tokens"


def test_upstream_credentials_and_raw_pages_are_not_forwarded():
    leaked = run_response(httpx.Response(400, json={"detail": "token sk-abcdefghijklmnop rejected"}))
    assert "sk-abcdefghijklmnop" not in json.dumps(leaked)
    assert "[redacted]" in leaked[0]["detail"]

    page = run_response(httpx.Response(403, text="<html>Access denied for account 12345</html>"))
    assert page[0]["detail"] == "Upstream returned HTTP 403"
    assert "12345" not in json.dumps(page)


def test_upstream_detail_is_bounded():
    events = run_response(httpx.Response(400, json={"message": "x" * 5000}))
    assert len(events[0]["detail"]) == 300
