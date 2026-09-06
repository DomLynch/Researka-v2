"""Exercise the real process boundary with an offline executable, never Codex."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from contracts import ProviderErrorClass
from runtime_core.codex_provider import CodexProvider
from runtime_core.providers import ProviderRequest


def _events(text='{"recommendation":"accept"}', usage=None):
    return [
        {"type": "thread.started", "thread_id": "offline-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "thinking"}},
        {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": usage if usage is not None else {
            "input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 8,
        }},
    ]


_FAKE_CLI = r'''
import json, os, pathlib, signal, sys, time
root = pathlib.Path(__file__).parent
config = json.loads((root / "fixture.json").read_text())
settings = dict(arg.split("=", 1) for i, arg in enumerate(sys.argv) if i and sys.argv[i-1] == "-c")
instructions = pathlib.Path(json.loads(settings["model_instructions_file"]))
stdin = sys.stdin.read()
(root / "receipt.json").write_text(json.dumps({
    "argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
    "cwd_files": os.listdir(), "instructions": instructions.read_text(),
    "instructions_path": str(instructions), "stdin": stdin, "pid": os.getpid(),
}))
if config.get("child"):
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if not config.get("inherit_pipes"):
            for fd in (0, 1, 2):
                os.close(fd)
        (root / "child.pid").write_text(str(os.getpid()))
        while True:
            time.sleep(0.02)
            with (root / "heartbeat").open("a") as stream:
                stream.write(".")
    deadline = time.monotonic() + 2
    while not (root / "heartbeat").exists() and time.monotonic() < deadline:
        time.sleep(0.005)
if config.get("flood"):
    while True:
        os.write(1, b"x" * 65536)
if config.get("sleep"):
    time.sleep(config["sleep"])
sys.stdout.write(config.get("stdout", ""))
sys.stderr.write(config.get("stderr", ""))
sys.exit(config.get("returncode", 0))
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text(f"#!{Path(sys.executable).resolve()}\n" + _FAKE_CLI)
    binary.chmod(0o700)
    monkeypatch.setenv("RESEARKA_V2_CODEX_BIN", str(binary))
    monkeypatch.setenv("LANG", "C.UTF-8")

    def configure(events=None, **options):
        options.setdefault("stdout", "\n".join(json.dumps(event) for event in (
            _events() if events is None else events
        )) + "\n")
        (tmp_path / "fixture.json").write_text(json.dumps(options))
        return tmp_path

    configure()
    return configure


def _request(**overrides):
    return ProviderRequest(system_prompt="SYSTEM CANARY", user_prompt="USER CANARY", prompt_version="offline", **overrides)


@pytest.mark.parametrize("position,kind,message,accepted", [
    (1, "item.completed", "known", True),
    (1, "item.completed", "unknown configuration error", False),
    (2, "item.completed", "known", False),
    (1, "error", "known", False),
])
def test_pinned_cli_startup_notice_is_not_a_failed_model_turn(fake_cli, position, kind, message, accepted):
    if message == "known":
        message = (
            "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
            "enable `features.code_mode_host` and install `codex-code-mode-host`."
        )
    events = _events()
    events.insert(position, {"type": kind, "item": {"id": "startup", "type": "error", "message": message}})
    fake_cli(events)
    result = CodexProvider().complete(_request())
    assert result.ok is accepted
    if accepted:
        assert result.response.metadata["startup_diagnostic_count"] == 1


def test_startup_notice_alone_does_not_prove_a_review(fake_cli):
    fake_cli([
        {"type": "thread.started", "thread_id": "offline-thread"},
        {"type": "item.completed", "item": {"id": "startup", "type": "error", "message": (
            "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
            "enable `features.code_mode_host` and install `codex-code-mode-host`."
        )}},
    ])
    assert not CodexProvider().complete(_request()).ok


def test_large_existing_manuscript_fits_review_budget(fake_cli):
    fake_cli(_events(usage={"input_tokens": 137611, "output_tokens": 3475, "reasoning_output_tokens": 2057}))
    assert CodexProvider().complete(_request(max_input_tokens=200000, max_output_tokens=12000)).ok
    result = CodexProvider().complete(_request(max_input_tokens=120000, max_output_tokens=12000))
    assert result.error.message == "codex_invalid_response:usage_budget_exceeded"


def test_real_subprocess_argv_environment_and_receipt(fake_cli, monkeypatch, tmp_path):
    for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "MIMO_API_KEY", "DATABASE_URL", "SECRET_CANARY", "BASH_ENV"):
        monkeypatch.setenv(key, "must-not-reach-child")
    whitelist = {"HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "CODEX_HOME",
                 "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
    home = tmp_path / "codex-home"
    monkeypatch.setenv("RESEARKA_V2_CODEX_HOME", str(home))
    real_popen = subprocess.Popen

    def checked_popen(*args, **kwargs):
        assert set(kwargs["env"]) <= whitelist
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("runtime_core.codex_provider.subprocess.Popen", checked_popen)
    fake_cli()
    result = CodexProvider().complete(_request())
    assert result.ok and result.response is not None
    assert result.response.provider == "codex"
    assert result.response.model == "gpt-5.6-sol"
    assert result.response.usage.model_dump() == {"input_tokens": 12, "output_tokens": 8, "cost_usd": 0.0}
    assert result.response.metadata == {"reasoning_effort": "high", "transport": "codex_cli",
                                        "billing": "codex_subscription", "api_spend_usd": 0.0,
                                        "startup_diagnostic_count": 0}
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    # macOS CoreFoundation adds this inside the Python fixture after exec.
    assert set(receipt["env"]) - {"__CF_USER_TEXT_ENCODING"} <= whitelist
    assert receipt["env"]["CODEX_HOME"] == str(home)
    assert receipt["env"]["HOME"] == os.environ["HOME"]
    assert receipt["cwd_files"] == []
    assert receipt["stdin"] == "USER CANARY"
    assert receipt["instructions"].startswith("SYSTEM CANARY")
    assert "1200 output tokens" in receipt["instructions"]
    assert not Path(receipt["cwd"]).exists()
    assert not Path(receipt["instructions_path"]).exists()
    argv = receipt["argv"]
    assert argv[:10] == ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                         "--skip-git-repo-check", "--sandbox", "read-only", "--model", "gpt-5.6-sol", "-c"]
    assert argv[-2:] == ["--json", "-"]
    settings = {value.split("=", 1)[0]: json.loads(value.split("=", 1)[1])
                for i, value in enumerate(argv) if i and argv[i-1] == "-c"}
    disabled = {key.removeprefix("features.") for key, value in settings.items() if key.startswith("features.") and value is False}
    assert {key: value for key, value in settings.items() if not key.startswith("features.")} == {"forced_login_method": "chatgpt", "model_provider": "openai",
                        "model_reasoning_effort": "high", "approval_policy": "never", "web_search": "disabled",
                        "project_doc_max_bytes": 0, "model_instructions_file": receipt["instructions_path"]}
    assert "--disable" not in argv
    assert disabled == {"shell_tool", "unified_exec", "apps", "plugins", "hooks", "memories", "multi_agent",
                        "browser_use", "computer_use", "image_generation", "view_image", "workspace_dependencies",
                        "goals", "sleep_tool", "skill_mcp_dependency_install", "skill_search", "chronicle", "code_mode_host"}


def test_terra_medium_and_constructor_timeout_override(fake_cli):
    fake_cli(sleep=1.1)
    provider = CodexProvider(model="gpt-5.6-terra", reasoning_effort="medium", timeout_sec=3)
    result = provider.complete(_request(timeout_sec=1))
    assert result.ok and result.response.model == "gpt-5.6-terra"
    assert result.response.metadata["reasoning_effort"] == "medium"
    assert CodexProvider().timeout_sec == 600


@pytest.mark.parametrize("kwargs", [
    {"model": "gpt-5.6-sol-latest"}, {"model": "other"}, {"reasoning_effort": "invalid"},
    {"timeout_sec": 0}, {"timeout_sec": -1}, {"timeout_sec": float("nan")},
    {"timeout_sec": float("inf")}, {"timeout_sec": True},
])
def test_invalid_constructor(kwargs):
    with pytest.raises(ValueError):
        CodexProvider(**kwargs)


@pytest.mark.parametrize("text", ["", "[]", "null", "true", "prose {}", "{} prose", "```json\n{}\n```",
                                   '{"value": NaN}', '{"value": Infinity}', '{"a":1,"a":2}', '{}\n{}'])
def test_final_must_be_strict_json_object(fake_cli, text):
    fake_cli(_events(text))
    result = CodexProvider().complete(_request())
    assert not result.ok and result.error.error_class is ProviderErrorClass.BAD_REQUEST


@pytest.mark.parametrize("kind", ["command_execution", "mcp_tool_call", "web_search", "file_change",
                                    "image_generation", "unknown"])
@pytest.mark.parametrize("event_type", ["item.started", "item.updated", "item.completed"])
def test_no_tool_item_even_with_valid_final(fake_cli, kind, event_type):
    events = _events()
    events.insert(2, {"type": event_type, "item": {"id": "tool", "type": kind}})
    fake_cli(events)
    result = CodexProvider().complete(_request())
    assert not result.ok and result.error.error_class is ProviderErrorClass.BAD_REQUEST


@pytest.mark.parametrize("usage", [
    {}, {"input_tokens": 1}, {"input_tokens": -1, "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": True}, {"input_tokens": "1", "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": 1.0}, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": -1},
    {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 2},
    {"input_tokens": 12001, "output_tokens": 1}, {"input_tokens": 1, "output_tokens": 1201},
])
def test_usage_validation_and_budget(fake_cli, usage):
    fake_cli(_events(usage=usage))
    result = CodexProvider().complete(_request())
    assert not result.ok and result.error.error_class is ProviderErrorClass.BAD_REQUEST


def test_zero_and_exact_boundary_usage(fake_cli):
    for usage in ({"input_tokens": 0, "output_tokens": 0}, {"input_tokens": 12, "output_tokens": 8}):
        fake_cli(_events(usage=usage))
        assert CodexProvider().complete(_request(max_input_tokens=12, max_output_tokens=8)).ok


@pytest.mark.parametrize("event_type", ["error", "turn.failed"])
@pytest.mark.parametrize("detail,expected", [
    ("You've hit your usage limit SECRET_CANARY", ProviderErrorClass.BILLING),
    ("insufficient_quota SECRET_CANARY", ProviderErrorClass.BILLING),
    ("quota exhausted SECRET_CANARY", ProviderErrorClass.BILLING),
    ("Not logged in SECRET_CANARY", ProviderErrorClass.BAD_REQUEST),
    ("Network unavailable SECRET_CANARY", ProviderErrorClass.PROVIDER_UNAVAILABLE),
])
def test_failed_events_never_succeed_even_after_completion(fake_cli, event_type, detail, expected):
    fake_cli([*_events(), {"type": event_type, "error": {"message": detail}}])
    result = CodexProvider().complete(_request())
    assert not result.ok and result.error.error_class is expected
    assert "SECRET_CANARY" not in result.model_dump_json()


@pytest.mark.parametrize("detail,expected", [
    ("quota exhausted", ProviderErrorClass.BILLING), ("Authentication failed", ProviderErrorClass.BAD_REQUEST),
    ("other failure", ProviderErrorClass.PROVIDER_UNAVAILABLE),
])
def test_nonzero_exit_never_succeeds_or_leaks_output(fake_cli, detail, expected):
    fake_cli(returncode=1, stderr=detail + " SECRET_CANARY")
    result = CodexProvider().complete(_request())
    assert not result.ok and result.error.error_class is expected
    assert "SECRET_CANARY" not in result.model_dump_json()


@pytest.mark.parametrize("stdout", ["", "not-json", "[]", '{"type":"turn.started","type":"turn.completed"}',
                                     '\n', '{"type":"unknown"}', '{"type":"turn.completed","usage":{}}'])
def test_malformed_jsonl(fake_cli, stdout):
    fake_cli(stdout=stdout)
    result = CodexProvider().complete(_request())
    assert not result.ok and result.error.error_class is ProviderErrorClass.BAD_REQUEST


@pytest.mark.parametrize("case", ["missing-start", "missing-end", "two-turns", "duplicate-final", "pending-item", "item-before-turn"])
def test_exactly_one_complete_turn(fake_cli, case):
    events = _events()
    if case == "missing-start":
        del events[1]
    elif case == "missing-end":
        events.pop()
    elif case == "two-turns":
        events.extend(_events())
    elif case == "duplicate-final":
        events.insert(-1, {"type": "item.completed", "item": {"id": "b", "type": "agent_message", "text": "{}"}})
    elif case == "pending-item":
        events.insert(-1, {"type": "item.started", "item": {"id": "p", "type": "reasoning"}})
    else:
        events.insert(0, events.pop(2))
    fake_cli(events)
    result = CodexProvider().complete(_request())
    assert not result.ok and result.error.error_class is ProviderErrorClass.BAD_REQUEST


def test_executable_lookup_and_missing_binary(fake_cli, monkeypatch, tmp_path):
    monkeypatch.delenv("RESEARKA_V2_CODEX_BIN")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert CodexProvider().complete(_request()).ok
    monkeypatch.setenv("PATH", str(tmp_path / "absent"))
    assert CodexProvider().complete(_request()).error.error_class is ProviderErrorClass.PROVIDER_UNAVAILABLE
    monkeypatch.setenv("RESEARKA_V2_CODEX_BIN", str(tmp_path / "absent"))
    assert CodexProvider().complete(_request()).error.error_class is ProviderErrorClass.PROVIDER_UNAVAILABLE


def _assert_child_stopped(root):
    assert (root / "child.pid").exists()
    heartbeat = root / "heartbeat"
    time.sleep(0.1)
    size = heartbeat.stat().st_size
    time.sleep(0.15)
    assert heartbeat.stat().st_size == size, "orphan child still running"


@pytest.mark.parametrize("parent_sleeps", [False, True])
def test_process_group_cleanup_including_orphan_after_parent_exit(fake_cli, parent_sleeps):
    root = fake_cli(child=True, sleep=10 if parent_sleeps else 0)
    start = time.monotonic()
    result = CodexProvider(timeout_sec=1 if parent_sleeps else 3).complete(_request())
    assert time.monotonic() - start < 5
    if parent_sleeps:
        assert result.error.error_class is ProviderErrorClass.TIMEOUT
    else:
        assert result.ok
    _assert_child_stopped(root)


def test_interruption_kills_process_group(fake_cli):
    root = fake_cli(child=True, sleep=10)
    stop = threading.Event()

    def interrupt_when_ready():
        deadline = time.monotonic() + 4
        while not (root / "heartbeat").exists() and time.monotonic() < deadline:
            if stop.wait(0.01):
                return
        os.kill(os.getpid(), signal.SIGINT)

    interrupter = threading.Thread(target=interrupt_when_ready)
    interrupter.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            CodexProvider(timeout_sec=5).complete(_request())
    finally:
        stop.set()
        interrupter.join()
    _assert_child_stopped(root)


def test_output_capture_is_bounded(fake_cli):
    fake_cli(flood=True)
    start = time.monotonic()
    result = CodexProvider(timeout_sec=5).complete(_request())
    assert time.monotonic() - start < 2
    assert result.error.error_class is ProviderErrorClass.BAD_REQUEST


@pytest.mark.parametrize("events", [
    [{"type": []}], [{"type": {}}],
    [{"type": "turn.started"}, {"type": "item.completed", "item": {"id": "a", "type": []}}],
    [{"type": "turn.started"}, {"type": "item.completed", "item": {"id": [], "type": "agent_message"}}],
    [{"type": "turn.started"}, {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": {}}}],
])
def test_malformed_event_shapes_fail_closed(fake_cli, events):
    fake_cli(events)
    assert CodexProvider().complete(_request()).error.error_class is ProviderErrorClass.BAD_REQUEST


def test_deeply_nested_final_fails_closed(fake_cli):
    fake_cli(_events('{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}'))
    assert CodexProvider().complete(_request()).error.error_class is ProviderErrorClass.BAD_REQUEST


def test_live_orphan_holding_pipes_cannot_extend_deadline(fake_cli):
    root = fake_cli(child=True, inherit_pipes=True)
    start = time.monotonic()
    assert CodexProvider(timeout_sec=3).complete(_request()).ok
    assert time.monotonic() - start < 3
    _assert_child_stopped(root)


def test_real_stdin_larger_than_pipe_capacity_is_not_truncated(fake_cli, tmp_path):
    text = "evidence\n" * 20000
    fake_cli()
    request = _request()
    request.user_prompt = text
    assert CodexProvider(timeout_sec=3).complete(request).ok
    assert json.loads((tmp_path / "receipt.json").read_text())["stdin"] == text


def test_error_result_reaps_children_and_cleans_tempdir(fake_cli):
    root = fake_cli(child=True, stdout="invalid-json SECRET_CANARY")
    result = CodexProvider(timeout_sec=3).complete(_request())
    assert result.error.error_class is ProviderErrorClass.BAD_REQUEST
    assert "SECRET_CANARY" not in result.model_dump_json()
    receipt = json.loads((root / "receipt.json").read_text())
    assert not Path(receipt["instructions_path"]).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(receipt["pid"], 0)
    _assert_child_stopped(root)
