#!/usr/bin/env python3
"""Bounded real-HTTP tool-agent acceptance test (standard library only).

Run locally against an already-running service; never starts a server. Chat is
primary; --api responses and --stream exercise the corresponding wire formats.
Only model-requested tools run, including the initial failing test. The temporary
workspace is deleted after the JSON report has been constructed. This is a local
acceptance harness, not a sandbox for executing an untrusted model's Python code.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


BUGGY = "def add(a, b):\n    return a - b\n"
TESTS = '''import unittest
from calculator import add

class CalculatorTests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_negative(self):
        self.assertEqual(add(-2, -3), -5)

    def test_zero(self):
        self.assertEqual(add(7, 0), 7)

if __name__ == "__main__":
    unittest.main()
'''
INSTRUCTIONS = """You are fixing a tiny Python calculator in an isolated workspace.
Use only the supplied tools. First read calculator.py and test_calculator.py and
run_tests to observe the original failing tests. Then fix calculator.py using
write_file, and run_tests again until they pass. Do not change or bypass tests,
access the network, run other commands, or access paths outside the workspace.
Return a short summary only after actual tests pass. All paths are relative.
"""
TOOL_SPECS = [
    {"name": "read_file", "description": "Read a UTF-8 file inside the workspace.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"], "additionalProperties": False}},
    {"name": "write_file", "description": "Replace calculator.py after observing failing tests.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"], "additionalProperties": False}},
    {"name": "run_tests", "description": "Run the fixed Python unittest command; no arguments.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]


class LocalTools:
    """Confined file tools and a fixed test runner; no shell-command interface."""

    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        self.counters = {"read_file": 0, "write_file": 0, "run_tests": 0}
        self.records = []
        self.test_results = []

    def path(self, value):
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("path must be a nonempty string")
        candidate = Path(value)
        if candidate.is_absolute():
            raise ValueError("absolute paths are forbidden")
        resolved = (self.root / candidate).resolve()
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise ValueError("path must stay inside workspace")
        # Reject even internal symlinks, including parent components.
        current = self.root
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("symlink paths are forbidden")
        return resolved

    def execute(self, name, raw_arguments):
        record = {"name": name, "arguments": raw_arguments}
        self.records.append(record)
        try:
            args = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            expected = {"read_file": {"path"}, "write_file": {"path", "content"}, "run_tests": set()}
            if name not in expected:
                raise ValueError("unknown tool")
            if not isinstance(args, dict) or set(args) != expected[name]:
                raise ValueError("arguments do not match tool schema")
            if name == "read_file":
                target = self.path(args["path"])
                if not target.is_file() or target.stat().st_size > 65536:
                    raise ValueError("file missing, not regular, or too large")
                self.counters[name] += 1
                result = {"content": target.read_text(encoding="utf-8")}
            elif name == "write_file":
                target = self.path(args["path"])
                if target != self.root / "calculator.py":
                    raise ValueError("only calculator.py may be modified")
                content = args["content"]
                if not isinstance(content, str) or len(content.encode("utf-8")) > 65536:
                    raise ValueError("content must be a UTF-8 string of at most 65536 bytes")
                if not self.test_results or self.test_results[0]["returncode"] in (None, 0):
                    raise ValueError("run initial failing tests before editing")
                self.counters[name] += 1
                target.write_text(content, encoding="utf-8")
                result = {"written_bytes": len(content.encode("utf-8"))}
            else:
                # Ensure the original test suite has not been replaced indirectly.
                target = self.path("test_calculator.py")
                if target.read_text(encoding="utf-8") != TESTS:
                    raise ValueError("test suite integrity check failed")
                self.path("calculator.py")
                command = [sys.executable, "-I", "-B", "-m", "unittest", "discover",
                           "-s", str(self.root), "-p", "test_calculator.py", "-v"]
                # Do not expose service credentials to the calculator process.
                env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR") if k in os.environ}
                env.update({"HOME": str(self.root), "TMPDIR": str(self.root)})
                self.counters[name] += 1
                try:
                    proc = subprocess.run(command, cwd=self.root, env=env, shell=False,
                                          capture_output=True, text=True, timeout=15)
                    result = {"returncode": proc.returncode, "stdout": proc.stdout[-16000:],
                              "stderr": proc.stderr[-16000:], "timed_out": False}
                except subprocess.TimeoutExpired as exc:
                    def text(value):
                        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
                    result = {"returncode": None, "stdout": text(exc.stdout)[-16000:],
                              "stderr": text(exc.stderr)[-16000:], "timed_out": True}
                self.test_results.append(result)
            record["result"] = result
        except (ValueError, OSError, UnicodeError) as exc:
            result = {"error": str(exc)}
            record["result"] = result
        return result

    def assertions(self):
        first = self.test_results[0] if self.test_results else {}
        last = self.test_results[-1] if self.test_results else {}
        return {
            "file_changed": self.path("calculator.py").read_text(encoding="utf-8") != BUGGY,
            "tests_first_failed": first.get("returncode") not in (None, 0),
            "tests_later_passed": len(self.test_results) >= 2 and last.get("returncode") == 0,
            "actual_read_execution": self.counters["read_file"] > 0,
            "actual_write_execution": self.counters["write_file"] > 0,
            "actual_test_executions": self.counters["run_tests"] >= 2,
            "tests_unchanged": self.path("test_calculator.py").read_text(encoding="utf-8") == TESTS,
        }


def sse_events(response):
    """Decode SSE data frames, including multi-line frames and final EOF."""
    data = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                data = []
                if payload == "[DONE]":
                    return
                yield json.loads(payload)
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data and "\n".join(data) != "[DONE]":
        yield json.loads("\n".join(data))


class HTTPClient:
    def __init__(self, endpoint, key, timeout=120):
        self.base = endpoint.rstrip("/")
        if not self.base.endswith("/v1"):
            self.base += "/v1"
        self.key, self.timeout = key, timeout
        self.requests = 0

    def request(self, route, payload=None, stream=False, api="chat"):
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.base + route, headers=headers,
                                     data=None if payload is None else json.dumps(payload).encode())
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                if not stream:
                    return json.load(response)
                if api == "responses":
                    for event in sse_events(response):
                        if event.get("type") == "response.completed":
                            return event["response"]
                        if event.get("type") in ("error", "response.failed", "response.incomplete"):
                            raise RuntimeError("Responses stream reported failure")
                    raise RuntimeError("Responses stream ended without response.completed")
                message = {"role": "assistant", "content": ""}
                calls = {}
                finished = False
                for event in sse_events(response):
                    if "error" in event:
                        raise RuntimeError("Chat stream reported failure")
                    for choice in event.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        if choice.get("finish_reason") is not None:
                            finished = True
                        delta = choice.get("delta", {})
                        message["content"] += delta.get("content") or ""
                        for fragment in delta.get("tool_calls", []):
                            call = calls.setdefault(fragment["index"], {
                                "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if fragment.get("id"):
                                call["id"] += fragment["id"]
                            for key in ("name", "arguments"):
                                call["function"][key] += fragment.get("function", {}).get(key) or ""
                if not finished:
                    raise RuntimeError("Chat stream ended without finish_reason")
                if calls:
                    message["tool_calls"] = [calls[i] for i in sorted(calls)]
                return {"choices": [{"message": message}]}
        except urllib.error.HTTPError as exc:
            # Never print response bodies, request headers, or credential-bearing URLs.
            raise RuntimeError("HTTP request failed with status " + str(exc.code)) from None
        except urllib.error.URLError:
            raise RuntimeError("HTTP connection failed") from None


def run(args):
    client = HTTPClient(args.endpoint, args.key, args.timeout)
    report = {"api": args.api, "stream": args.stream, "success": False,
              "rounds": 0, "tool_requests": [], "execution_counters": {}, "assertions": {}}
    try:
        discovery = client.request("/models")
        models = [item["id"] for item in discovery.get("data", [])
                  if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]]
        if not models:
            raise RuntimeError("Model discovery returned no model IDs")
        model = args.model or models[0]
        if model not in models:
            raise RuntimeError("Requested model was not in actual /v1/models discovery")
        report.update({"discovered_models": models, "model": model})
        with tempfile.TemporaryDirectory(prefix="agent-acceptance-") as tmp:
            root = Path(tmp)
            (root / "calculator.py").write_text(BUGGY, encoding="utf-8")
            (root / "test_calculator.py").write_text(TESTS, encoding="utf-8")
            tools = LocalTools(root)
            history = [{"role": "user", "content": INSTRUCTIONS}]
            try:
                for turn in range(args.max_rounds):
                    report["rounds"] = turn + 1
                    definitions = ([{"type": "function", "function": spec} for spec in TOOL_SPECS]
                                   if args.api == "chat" else [{"type": "function", **spec} for spec in TOOL_SPECS])
                    payload = {"model": model, "tools": definitions, "stream": args.stream,
                               "messages" if args.api == "chat" else "input": history}
                    output = client.request("/chat/completions" if args.api == "chat" else "/responses",
                                            payload, args.stream, args.api)
                    if args.api == "chat":
                        message = output["choices"][0]["message"]
                        history.append(message)
                        calls = [(c["id"], c["function"]["name"], c["function"]["arguments"])
                                 for c in (message.get("tool_calls") or [])]
                        final_text = message.get("content") or ""
                    else:
                        history.extend(output.get("output", []))
                        calls = [(c["call_id"], c["name"], c["arguments"])
                                 for c in output.get("output", []) if c.get("type") == "function_call"]
                        final_text = "\n".join(
                            part.get("text", "") for item in output.get("output", [])
                            if item.get("type") == "message" and item.get("role") == "assistant"
                            for part in item.get("content", []) if part.get("type") == "output_text")
                    if len(calls) > 16:
                        raise RuntimeError("Model exceeded per-round tool call limit (16)")
                    for call_id, name, arguments in calls:
                        result = tools.execute(name, arguments)
                        tools.records[-1]["call_id"] = call_id
                        history.append(({"role": "tool", "tool_call_id": call_id,
                                         "content": json.dumps(result)} if args.api == "chat" else
                                        {"type": "function_call_output", "call_id": call_id,
                                         "output": json.dumps(result)}))
                    checks = tools.assertions()
                    # Tool results must be sent back and consumed by a subsequent
                    # model response, not merely executed locally before success.
                    if not calls:
                        if not all(checks.values()):
                            raise RuntimeError("Model stopped before acceptance assertions passed")
                        if not isinstance(final_text, str) or not final_text.strip():
                            raise RuntimeError("Model returned no final answer after tool results")
                        report["final_answer"] = final_text
                        report["final_tool_results_consumed"] = True
                        report["success"] = True
                        break
                if not report["success"]:
                    raise RuntimeError("Maximum agent rounds exhausted")
            finally:
                report["tool_requests"] = tools.records
                report["execution_counters"] = tools.counters
                report["assertions"] = tools.assertions()
                # Workspace paths are ephemeral and not useful report identifiers.
                report = sanitize(report, [str(root)])
    except Exception as exc:
        report["error"] = str(exc) if isinstance(exc, RuntimeError) else "Acceptance failed: " + type(exc).__name__
    report["http_requests"] = client.requests
    return sanitize(report, [args.key, args.endpoint])


def sanitize(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, dict):
        return {sanitize(k, secrets): sanitize(v, secrets) for k, v in value.items()}
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.getenv("AGENT_ACCEPTANCE_ENDPOINT", os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8790")))
    parser.add_argument("--key", default=os.getenv("AGENT_ACCEPTANCE_API_KEY", os.getenv("OPENAI_API_KEY", "")))
    parser.add_argument("--model", default=os.getenv("AGENT_ACCEPTANCE_MODEL", os.getenv("OPENAI_MODEL")))
    parser.add_argument("--api", choices=("chat", "responses"), default="chat")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--max-rounds", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not 1 <= args.max_rounds <= 100 or not 0 < args.timeout <= 600:
        parser.error("max-rounds must be 1..100 and timeout must be >0 and <=600 seconds")
    report = run(args)
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
