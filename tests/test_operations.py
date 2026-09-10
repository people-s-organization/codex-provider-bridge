import asyncio
import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from config import Settings, settings


@pytest.fixture(autouse=True)
def operational_settings(monkeypatch):
    monkeypatch.setattr(settings, "bridge_api_key", "")
    monkeypatch.setattr(settings, "max_request_bytes", 1024)
    monkeypatch.setattr(settings, "max_concurrent_requests", 32)


def test_auth_protects_routes_and_docs_but_not_minimal_health(monkeypatch):
    monkeypatch.setattr(settings, "bridge_api_key", "test-secret")
    client = TestClient(main.app)
    for path in ["/v1/models", "/models", "/docs", "/", "/routes"]:
        response = client.get(path)
        assert response.status_code == 401
        assert "test-secret" not in response.text
        assert response.headers["x-request-id"]
    assert client.get("/v1/models", headers={"Authorization": "Bearer test-secret"}).status_code == 200
    health = client.get("/health").json()
    assert set(health) == {"status", "service", "deployment_commit", "started_at"}


def test_public_bind_requires_auth_or_opt_in():
    for host in ["0.0.0.0", "::", "192.0.2.1", "example.com"]:
        cfg = Settings(_env_file=None, host=host, bridge_api_key="", allow_unauthenticated_public=False)
        with pytest.raises(ValueError, match="Public binding"):
            cfg.validate_network_binding()
        cfg.bridge_api_key = "test"
        cfg.validate_network_binding()
        cfg.bridge_api_key = ""
        cfg.allow_unauthenticated_public = True
        cfg.validate_network_binding()
    for host in ["127.0.0.1", "127.0.0.2", "::1", "localhost"]:
        Settings(_env_file=None, host=host).validate_network_binding()


def test_request_id_generated_not_reflected():
    client = TestClient(main.app)
    first = client.get("/health", headers={"X-Request-ID": "untrusted"})
    second = client.get("/health")
    assert len(first.headers["x-request-id"]) == 32
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_content_length_limit_before_validation():
    response = TestClient(main.app).post("/v1/responses", content="x" * 1025)
    assert response.status_code == 413
    assert response.headers["x-request-id"]


def test_chunked_body_limit():
    async def run():
        async def body():
            yield b"x" * 600
            yield b"x" * 600
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post("/v1/responses", content=body())
            assert response.status_code == 413
    asyncio.run(run())


def test_concurrency_includes_stream_lifetime_and_releases(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_requests", 1)

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def stream(request):
            started.set()
            yield 'data: {"ok": true}\n\n'
            await release.wait()
        monkeypatch.setattr(main.bridge, "responses_stream", stream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            running = asyncio.create_task(client.post("/v1/responses", json={"model": "test", "input": "hi", "stream": True}))
            await started.wait()
            assert (await client.get("/v1/models")).status_code == 429
            assert (await client.get("/health")).status_code == 200
            release.set()
            assert (await running).status_code == 200
            assert (await client.get("/v1/models")).status_code == 200
    asyncio.run(run())


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_upstream_error_status_preserved_without_details(status):
    response = main.upstream_error_to_response({"status": status, "error": "secret-token", "detail": "private prompt", "code": "secret", "param": "secret"})
    assert response.status_code == status
    assert b"secret" not in response.body
    assert b"private" not in response.body


def test_unexpected_error_sanitized(monkeypatch):
    async def fail(request):
        raise RuntimeError("secret-token private-prompt")
    monkeypatch.setattr(main.bridge, "responses", fail)
    response = TestClient(main.app).post("/responses", json={"model": "test", "input": "hi"})
    assert response.status_code == 500
    assert "secret" not in response.text
    assert response.headers["x-request-id"]


def test_validation_does_not_echo_input():
    response = TestClient(main.app).post("/responses", content='{"secret-token":', headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert "secret-token" not in response.text


def test_timeout_sanitized_retains_gateway_status(monkeypatch):
    async def fail(request):
        raise httpx.ReadTimeout("secret-token")
    monkeypatch.setattr(main.bridge, "responses", fail)
    response = TestClient(main.app).post("/responses", json={"model": "test", "input": "hi"})
    assert response.status_code == 504
    assert "secret" not in response.text


def test_legacy_function_result_rendering(monkeypatch):
    async def result(request):
        return {"id": "test", "created": 1, "model": "test", "content": "", "usage": {}, "function_call": {"name": "f", "arguments": "{}"}, "finish_reason": "function_call"}, None
    monkeypatch.setattr(main.bridge, "chat_completion", result)
    response = TestClient(main.app).post("/v1/chat/completions", json={"model": "test", "messages": [{"role": "user", "content": "hi"}]})
    choice = response.json()["choices"][0]
    assert choice["message"]["function_call"] == {"name": "f", "arguments": "{}"}
    assert choice["finish_reason"] == "function_call"


def test_retry_after_only_safe_numeric():
    assert main.upstream_error_to_response({"status": 429, "retry_after": "12"}).headers["retry-after"] == "12"
    assert "retry-after" not in main.upstream_error_to_response({"status": 429, "retry_after": "secret\r\n"}).headers


def test_compatibility_warning_header(monkeypatch):
    async def result(request):
        return {"output_text": "ok"}, None
    monkeypatch.setattr(main.bridge, "responses", result)
    monkeypatch.delenv("BRIDGE_STRICT_COMPATIBILITY", raising=False)
    response = TestClient(main.app).post("/responses", json={"model": "test", "input": "hi", "temperature": 0.7})
    assert response.status_code == 200
    assert "temperature" in response.headers["x-bridge-compatibility-warnings"]


@pytest.mark.parametrize("fail_tests,fail_health", [(False, False), (True, False), (False, True)])
def test_deploy_tests_before_restart_and_rolls_back(tmp_path, fail_tests, fail_health):
    script = tmp_path / "deploy.sh"
    script.write_text((Path(__file__).parents[1] / "deploy.sh").read_text())
    (tmp_path / ".git").mkdir()
    commands = tmp_path / "bin"
    commands.mkdir()
    log = tmp_path / "commands.log"
    fake = '''#!/usr/bin/env python3
import os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['LOG'], 'a') as f: f.write(name + ' ' + ' '.join(args) + '\\n')
if name == 'git':
    if args[:2] == ['rev-parse', '--verify']: print(('a' if args[-1].startswith('a' * 40) else 'b') * 40)
    elif args[:2] == ['rev-parse', 'HEAD']: print('a' * 40)
    elif args[0] == 'symbolic-ref': print('main')
elif name == 'systemctl':
    if 'show' in args: print(os.environ['ROOT'])
elif name == 'python':
    if '-m' in args: sys.exit(int(os.environ['FAIL_TESTS']))
    if len(args) > 2 and args[2] == 'b' * 40: sys.exit(int(os.environ['FAIL_HEALTH']))
    print('a' * 40 if len(args) > 2 and args[2] == 'identify' else 'old-start')
'''
    for name in ["git", "systemctl", "python", "sleep"]:
        executable = commands / name
        executable.write_text(fake)
        executable.chmod(0o755)
    env = dict(os.environ, PATH=f"{commands}:{os.environ['PATH']}", LOG=str(log), ROOT=str(tmp_path), BRIDGE_PYTHON=str(commands / "python"), FAIL_TESTS=str(int(fail_tests)), FAIL_HEALTH=str(int(fail_health)))
    result = subprocess.run(["bash", str(script), "target"], env=env, capture_output=True, text=True)
    calls = log.read_text()
    restart = "systemctl --user restart"
    assert "python -m pytest -q" in calls
    if fail_tests:
        assert result.returncode != 0
        assert restart not in calls
        assert "git switch main --quiet" in calls
    elif fail_health:
        assert result.returncode != 0
        assert calls.count(restart) == 2
        assert "git switch main --quiet" in calls
    else:
        assert result.returncode == 0
        assert calls.count(restart) == 1
        assert calls.index("python -m pytest -q") < calls.index(restart)
