"""Local-tool unit tests only: no HTTP calls or live model execution."""
import json
import subprocess

import pytest

from scripts.agent_acceptance import BUGGY, TESTS, LocalTools, sanitize


@pytest.fixture
def tools(tmp_path):
    (tmp_path / "calculator.py").write_text(BUGGY, encoding="utf-8")
    (tmp_path / "test_calculator.py").write_text(TESTS, encoding="utf-8")
    return LocalTools(tmp_path)


def test_real_local_fail_fix_pass_and_counters(tools):
    assert tools.execute("read_file", '{"path":"calculator.py"}') == {"content": BUGGY}
    first = tools.execute("run_tests", "{}")
    assert first["returncode"] != 0
    assert "FAIL" in first["stderr"]
    assert not first["timed_out"]
    fixed = "def add(a, b):\n    return a + b\n"
    assert tools.execute("write_file", {"path": "calculator.py", "content": fixed}) == {
        "written_bytes": len(fixed.encode())}
    last = tools.execute("run_tests", {})
    assert last["returncode"] == 0, last
    assert "Ran 3 tests" in last["stderr"]
    assert all(tools.assertions().values())
    assert tools.counters == {"read_file": 1, "write_file": 1, "run_tests": 2}
    assert len(tools.records) == 4
    assert tools.records[1]["result"] == first
    assert tools.records[3]["result"] == last


@pytest.mark.parametrize("path", ["../escape.py", "../../escape.py", "/etc/passwd", "", ".", "bad\x00name", 3])
@pytest.mark.parametrize("tool", ["read_file", "write_file"])
def test_rejects_invalid_paths_without_execution(tools, path, tool):
    args = {"path": path}
    if tool == "write_file":
        args["content"] = "no"
    assert "error" in tools.execute(tool, args)
    assert not any(tools.counters.values())
    assert len(tools.records) == 1


def test_symlink_escape_and_internal_symlink_rejected(tools, tmp_path):
    (tmp_path / "outside").symlink_to(tmp_path.parent, target_is_directory=True)
    (tmp_path / "alias.py").symlink_to(tmp_path / "calculator.py")
    for path in ("outside/escape.py", "alias.py"):
        assert "error" in tools.execute("read_file", {"path": path})
        assert "error" in tools.execute("write_file", {"path": path, "content": "bad"})
    assert not any(tools.counters.values())


@pytest.mark.parametrize("name,args", [
    ("shell", {"command": "echo bad"}),
    ("run_tests", {"command": "echo bad"}),
    ("run_tests", {"path": "test_other.py"}),
    ("run_tests", []),
    ("run_tests", "not JSON"),
    ("read_file", {}),
    ("read_file", {"path": "calculator.py", "extra": True}),
    ("write_file", {"path": "calculator.py"}),
])
def test_strict_tool_schema(tools, name, args):
    assert "error" in tools.execute(name, args)
    assert not any(tools.counters.values())
    assert tools.records[0]["name"] == name
    assert tools.records[0]["arguments"] == args


def test_write_requires_baseline_failure_and_preserves_test_suite(tools):
    assert "error" in tools.execute("write_file", {"path": "calculator.py", "content": "fixed"})
    tools.execute("run_tests", {})
    for path in ("test_calculator.py", "new.py"):
        assert "error" in tools.execute("write_file", {"path": path, "content": "bad"})
    for content in (None, 42, "x" * 65537):
        assert "error" in tools.execute("write_file", {"path": "calculator.py", "content": content})
    assert tools.counters["write_file"] == 0
    assert (tools.root / "test_calculator.py").read_text() == TESTS


def test_test_suite_integrity_checked(tools):
    (tools.root / "test_calculator.py").write_text("# bypass")
    assert "error" in tools.execute("run_tests", {})
    assert tools.counters["run_tests"] == 0


def test_fixed_command_timeout_and_scrubbed_environment(tools, monkeypatch):
    observed = {}

    def timeout(command, **kwargs):
        observed.update(command=command, **kwargs)
        raise subprocess.TimeoutExpired(command, 15, output=b"partial", stderr=b"slow")

    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-secret")
    monkeypatch.setattr(subprocess, "run", timeout)
    result = tools.execute("run_tests", {})
    assert result == {"returncode": None, "stdout": "partial", "stderr": "slow", "timed_out": True}
    assert observed["shell"] is False
    assert observed["command"][1:] == ["-I", "-B", "-m", "unittest", "discover", "-s",
                                       str(tools.root), "-p", "test_calculator.py", "-v"]
    assert "OPENAI_API_KEY" not in observed["env"]
    assert observed["cwd"] == tools.root
    assert tools.counters["run_tests"] == 1
    assert not tools.assertions()["tests_first_failed"]
    assert "error" in tools.execute("write_file", {"path": "calculator.py", "content": "fixed"})


def test_read_limit_and_missing_file(tools):
    (tools.root / "large.txt").write_text("x" * 65537)
    for path in ("large.txt", "missing.txt"):
        assert "error" in tools.execute("read_file", {"path": path})
    assert tools.counters["read_file"] == 0


def test_report_sanitization_is_recursive():
    report = {"secret": [{"arguments": "contains secret", "result": {"stderr": "secret"}}]}
    clean = sanitize(report, ["secret", ""])
    assert "secret" not in json.dumps(clean)
    assert clean["[REDACTED]"][0]["arguments"] == "contains [REDACTED]"


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("max_rounds,final_text,success", [(2, "Fixed and verified.", False),
                                                            (3, "Fixed and verified.", True),
                                                            (3, "", False)])
def test_agent_requires_model_final_answer_after_real_tools(monkeypatch, api, max_rounds, final_text, success):
    from types import SimpleNamespace
    from scripts import agent_acceptance as acceptance

    requests = []
    scripted = [
        [("read", "read_file", {"path": "calculator.py"}), ("baseline", "run_tests", {})],
        [("write", "write_file", {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"}),
         ("verify", "run_tests", {})],
    ]

    def request(self, route, payload=None, stream=False, api="chat"):
        self.requests += 1
        requests.append((route, json.loads(json.dumps(payload))))
        if route == "/models":
            return {"data": [{"id": "discovered-test-model"}]}
        turn = len(requests) - 2
        calls = scripted[turn] if turn < len(scripted) else []
        if turn == 2:
            history = payload["messages" if api == "chat" else "input"]
            last = history[-1]
            assert last["tool_call_id" if api == "chat" else "call_id"] == "verify"
            assert json.loads(last["content" if api == "chat" else "output"])["returncode"] == 0
        if api == "chat":
            return {"choices": [{"message": {"role": "assistant", "content": final_text if not calls else None,
                    "tool_calls": [{"id": ident, "type": "function", "function": {
                        "name": name, "arguments": json.dumps(args)}} for ident, name, args in calls] or None}}]}
        return {"output": [{"type": "function_call", "call_id": ident,
                             "name": name, "arguments": json.dumps(args)} for ident, name, args in calls]
                if calls else [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": final_text}]}]}

    monkeypatch.setattr(acceptance.HTTPClient, "request", request)
    report = acceptance.run(SimpleNamespace(endpoint="http://unused.invalid", key="", timeout=1,
                                            model=None, api=api, stream=False, max_rounds=max_rounds))
    assert report["success"] is success, report
    assert all(report["assertions"].values()), report
    assert report["execution_counters"] == {"read_file": 1, "write_file": 1, "run_tests": 2}
    assert len(requests) == max_rounds + 1
    assert requests[0] == ("/models", None)
    if success:
        assert report["final_tool_results_consumed"] is True
        assert report["final_answer"] == final_text
    elif max_rounds == 2:
        assert report["error"] == "Maximum agent rounds exhausted"
    else:
        assert report["error"] == "Model returned no final answer after tool results"
