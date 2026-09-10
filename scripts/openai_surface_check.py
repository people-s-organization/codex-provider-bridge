#!/usr/bin/env python3
"""Live OpenAI-surface conformance check for a running Codex Provider Bridge.

Every check talks to the real service (default http://127.0.0.1:8790) and asserts the
status code and response shape an OpenAI-compatible client depends on. Nothing is
mocked: a check that needs generation really generates.

Usage:
    .venv/bin/python scripts/openai_surface_check.py [--endpoint URL] [--key KEY]
                                                     [--model ID] [--json]

Exit code 0 when no check fails (skipped checks are not passes). Optional/deployment-dependent checks
(media credentials, auth) are reported as SKIP instead of failing.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


class Checker:
    def __init__(self, endpoint, key, timeout=120.0):
        self.endpoint = endpoint.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.results = []

    def request(self, method, path, body=None, raw=False, headers=None):
        url = self.endpoint + path
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.key:
            request.add_header("Authorization", "Bearer " + self.key)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8", "replace")
                return response.status, dict(response.headers), payload
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            return 0, {}, "connection failed: " + type(exc.reason).__name__

    def json_request(self, method, path, body=None, headers=None):
        status, response_headers, payload = self.request(method, path, body, headers=headers)
        try:
            return status, response_headers, json.loads(payload)
        except ValueError:
            return status, response_headers, None

    def check(self, name, condition, detail=""):
        self.results.append({"check": name, "ok": bool(condition), "detail": detail})
        return bool(condition)

    def skip(self, name, reason):
        self.results.append({"check": name, "ok": None, "detail": reason})

    # -- checks ---------------------------------------------------------------
    def discover_model(self, requested):
        status, _, payload = self.json_request("GET", "/v1/models")
        if status != 200 or not isinstance(payload, dict):
            self.check("GET /v1/models returns 200", False, f"status={status}")
            return None
        data = payload.get("data") or []
        self.check(
            "GET /v1/models returns an OpenAI list",
            payload.get("object") == "list" and isinstance(data, list) and data,
            f"object={payload.get('object')} count={len(data)}",
        )
        ids = [item.get("id") for item in data if isinstance(item, dict)]
        self.check(
            "model objects carry id/object/created/owned_by",
            all({"id", "object", "created", "owned_by"} <= set(item) for item in data if isinstance(item, dict)),
            f"sample={ids[:3]}",
        )
        if requested:
            return requested
        return ids[0] if ids else None

    def check_model_endpoints(self, model):
        if not model:
            self.skip("GET /v1/models/{id}", "no discovered model")
            return
        status, _, payload = self.json_request("GET", f"/v1/models/{model}")
        self.check(
            f"GET /v1/models/{model} returns the model object",
            status == 200 and isinstance(payload, dict) and payload.get("id") == model,
            f"status={status}",
        )
        status, _, payload = self.json_request("GET", "/v1/models/gpt-does-not-exist-xyz")
        self.check(
            "unknown model returns 404 model_not_found",
            status == 404 and isinstance(payload, dict) and payload.get("error", {}).get("code") == "model_not_found",
            f"status={status} body={json.dumps(payload)[:120]}",
        )

    def check_chat(self, model):
        status, _, payload = self.json_request(
            "POST", "/v1/chat/completions",
            {"model": model, "messages": [{"role": "user", "content": "Reply with the single word ok."}]},
        )
        ok = (
            status == 200
            and isinstance(payload, dict)
            and payload.get("object") == "chat.completion"
            and isinstance(payload.get("choices"), list)
            and payload["choices"]
            and payload["choices"][0].get("message", {}).get("role") == "assistant"
            and payload["choices"][0].get("finish_reason") in {"stop", "length", "tool_calls"}
            and isinstance(payload.get("usage"), dict)
        )
        self.check("POST /v1/chat/completions returns an OpenAI completion", ok, f"status={status}")
        return payload

    def check_validation_errors(self, model):
        status, _, payload = self.json_request(
            "POST", "/v1/chat/completions",
            {"model": model, "messages": [{"role": "user", "content": "hi"}], "tool_choice": "bogus"},
        )
        error = (payload or {}).get("error") or {}
        self.check(
            "invalid request body returns 400 with a located reason",
            status == 400 and error.get("type") == "invalid_request_error" and error.get("param") == "tool_choice",
            f"status={status} error={json.dumps(error)[:120]}",
        )
        status, _, payload = self.request("POST", "/v1/chat/completions")
        self.check("request without a body is a 4xx error, not a crash", 400 <= status < 500, f"status={status}")

        status, _, payload = self.json_request(
            "POST", "/v1/chat/completions",
            {"model": "gpt-does-not-exist-xyz", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.check(
            "unknown model on chat returns 404 model_not_found",
            status == 404 and ((payload or {}).get("error") or {}).get("code") == "model_not_found",
            f"status={status}",
        )

    def check_unknown_route(self):
        status, _, payload = self.json_request("POST", "/v1/definitely-not-a-route", {})
        self.check("unknown /v1 route returns 404", status == 404, f"status={status} body={json.dumps(payload)[:100]}")

    def check_streaming(self, model):
        status, headers, payload = self.request(
            "POST", "/v1/chat/completions",
            {"model": model, "messages": [{"role": "user", "content": "Count 1 to 3."}], "stream": True},
        )
        content_type = (headers.get("Content-Type") or headers.get("content-type") or "")
        lines = [line[6:] for line in payload.splitlines() if line.startswith("data: ")]
        self.check(
            "streaming chat is text/event-stream and terminates with [DONE]",
            status == 200 and "text/event-stream" in content_type and lines and lines[-1] == "[DONE]",
            f"status={status} content_type={content_type} last={lines[-1] if lines else None}",
        )
        chunks = []
        for line in lines[:-1]:
            try:
                chunks.append(json.loads(line))
            except ValueError:
                continue
        self.check(
            "streaming chunks use chat.completion.chunk with a delta",
            bool(chunks) and all(chunk.get("object") == "chat.completion.chunk" for chunk in chunks)
            and any("delta" in chunk["choices"][0] for chunk in chunks if chunk.get("choices")),
            f"chunks={len(chunks)}",
        )

    def check_responses_store(self, model):
        status, _, payload = self.json_request(
            "POST", "/v1/responses", {"model": model, "input": "Reply with the single word ok."}
        )
        response_id = (payload or {}).get("id")
        ok = status == 200 and isinstance(payload, dict) and payload.get("object") == "response" and response_id
        self.check("POST /v1/responses returns a response object", ok, f"status={status} id={response_id}")
        if not ok:
            self.skip("GET/DELETE /v1/responses/{id}", "no stored response id")
            return

        status, _, stored = self.json_request("GET", f"/v1/responses/{response_id}")
        self.check("GET /v1/responses/{id} returns the stored response", status == 200 and (stored or {}).get("id") == response_id, f"status={status}")

        status, _, chained = self.json_request(
            "POST", "/v1/responses", {"model": model, "input": "And again.", "previous_response_id": response_id}
        )
        self.check("previous_response_id chains onto a stored response", status == 200 and (chained or {}).get("id"), f"status={status}")

        status, _, deleted = self.json_request("DELETE", f"/v1/responses/{response_id}")
        self.check(
            "DELETE /v1/responses/{id} reports deletion",
            status == 200 and (deleted or {}).get("deleted") is True,
            f"status={status} body={json.dumps(deleted)[:100]}",
        )
        status, _, missing = self.json_request("GET", f"/v1/responses/{response_id}")
        self.check(
            "deleted response is 404 response_not_found",
            status == 404 and ((missing or {}).get("error") or {}).get("code") == "response_not_found",
            f"status={status}",
        )
        status, _, unknown = self.json_request(
            "POST", "/v1/responses", {"model": model, "input": "hi", "previous_response_id": "resp_missing_xyz"}
        )
        self.check(
            "unknown previous_response_id is 404 previous_response_not_found",
            status == 404 and ((unknown or {}).get("error") or {}).get("code") == "previous_response_not_found",
            f"status={status}",
        )

    def check_unsupported_endpoints(self):
        status, _, payload = self.json_request("POST", "/v1/embeddings", {"model": "x", "input": "hi"})
        if status in {200, 201}:
            self.skip("POST /v1/embeddings", "OPENAI_API_KEY proxy is configured")
        else:
            self.check(
                "unavailable capability returns an honest unsupported_endpoint error",
                status in {400, 404, 501} and ((payload or {}).get("error") or {}).get("code") in {"unsupported_endpoint", "invalid_request_error"},
                f"status={status} body={json.dumps(payload)[:120]}",
            )

    def check_cors(self):
        status, headers, _ = self.request(
            "OPTIONS", "/v1/chat/completions",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
        )
        allow = headers.get("Access-Control-Allow-Origin") or headers.get("access-control-allow-origin")
        self.check("CORS preflight succeeds for browser clients", status in {200, 204} and bool(allow), f"status={status} allow_origin={allow}")

    def check_health(self):
        status, _, payload = self.json_request("GET", "/health")
        self.check(
            "GET /health reports ok with a deployment commit",
            status == 200 and (payload or {}).get("status") == "ok" and (payload or {}).get("deployment_commit"),
            f"status={status}",
        )

    def check_auth(self):
        if not self.key:
            self.skip("bridge API key enforcement", "no --key supplied")
            return
        status, _, payload = self.json_request("GET", "/v1/models")
        self.check("configured bridge key is accepted", status == 200, f"status={status}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.getenv("BRIDGE_ENDPOINT", "http://127.0.0.1:8790"))
    parser.add_argument("--key", default=os.getenv("BRIDGE_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("BRIDGE_CHECK_MODEL"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json", action="store_true", help="emit the raw report")
    args = parser.parse_args()

    checker = Checker(args.endpoint, args.key, args.timeout)
    model = checker.discover_model(args.model)
    checker.check_model_endpoints(model)
    if model:
        checker.check_chat(model)
        checker.check_validation_errors(model)
        checker.check_streaming(model)
        checker.check_responses_store(model)
    else:
        checker.skip("generation checks", "no model available")
    checker.check_unknown_route()
    checker.check_unsupported_endpoints()
    checker.check_cors()
    checker.check_health()
    checker.check_auth()

    failed = [result for result in checker.results if result["ok"] is False]
    if args.json:
        print(json.dumps({"endpoint": args.endpoint, "model": model, "results": checker.results, "failed": len(failed)}, indent=2))
    else:
        for result in checker.results:
            mark = "PASS" if result["ok"] else "SKIP" if result["ok"] is None else "FAIL"
            print(f"[{mark}] {result['check']}" + (f"  ({result['detail']})" if result["detail"] and mark != "PASS" else ""))
        passed = sum(result["ok"] is True for result in checker.results)
        skipped = sum(result["ok"] is None for result in checker.results)
        print(f"\n{passed} passed, {len(failed)} failed, {skipped} skipped ({len(checker.results)} total)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
