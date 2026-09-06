"""Single-turn, subscription-authenticated Codex CLI transport."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any

from contracts import ProviderErrorClass, ProviderUsage

from .providers import ProviderError, ProviderRequest, ProviderResponse, ProviderResult


_ENV_KEYS = (
    "HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "CODEX_HOME",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
)
_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "memories",
    "multi_agent", "browser_use", "computer_use", "image_generation", "view_image",
    "workspace_dependencies", "goals", "sleep_tool", "skill_mcp_dependency_install",
    "skill_search", "chronicle", "code_mode_host",
)
_MAX_CAPTURE_BYTES = 4 * 1024 * 1024


def _error(kind: ProviderErrorClass, reason: str) -> ProviderResult:
    return ProviderResult(ok=False, error=ProviderError(error_class=kind, message=reason))


def _execution_error(detail: str) -> ProviderResult:
    # Inspect diagnostics for classification only; never return prompts or CLI output.
    lowered = detail.lower()
    if any(marker in lowered for marker in (
        "quota exhausted", "insufficient_quota", "quota exceeded", "usage limit",
        "usage_limit_reached",
    )):
        return _error(ProviderErrorClass.BILLING, "codex_subscription_quota_exhausted")
    if any(marker in lowered for marker in (
        "not logged in", "authentication", "unauthorized", "invalid_api_key",
        "login required", "sign in", "token_expired", "refresh_token",
    )):
        return _error(ProviderErrorClass.BAD_REQUEST, "codex_authentication_failed")
    return _error(ProviderErrorClass.PROVIDER_UNAVAILABLE, "codex_execution_failed")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("invalid_json_constant")


def _json_object(text: str) -> dict[str, Any]:
    value = json.loads(text, object_pairs_hook=_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("json_object_required")
    pending: list[tuple[Any, int]] = [(value, 1)]
    while pending:
        container, depth = pending.pop()
        if depth > 64:
            raise ValueError("json_depth_exceeded")
        children = container.values() if isinstance(container, dict) else container
        pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return value


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    # Do not gate this on poll(): the leader may have exited, leaving children alive.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _exchange(process: subprocess.Popen[bytes], prompt: bytes, timeout: float) -> tuple[bytes, bytes, int]:
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    offset = 0
    captured = 0
    with selectors.DefaultSelector() as selector:
        for stream, name, event in (
            (process.stdin, "stdin", selectors.EVENT_WRITE),
            (process.stdout, "stdout", selectors.EVENT_READ),
            (process.stderr, "stderr", selectors.EVENT_READ),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, event, name)
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("codex", timeout)
            if process.poll() is not None:
                _kill_group(process)
            for key, _ in selector.select(min(remaining, 0.05)):
                if key.data == "stdin":
                    offset = _write_prompt(key.fd, prompt, offset)
                    if offset == len(prompt):
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                captured += len(chunk)
                if captured > _MAX_CAPTURE_BYTES:
                    raise ValueError("codex_output_limit_exceeded")
                buffers[key.data].extend(chunk)
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), process.wait()


def _write_prompt(fd: int, prompt: bytes, offset: int) -> int:
    try:
        return offset + os.write(fd, prompt[offset:offset + 65536])
    except BrokenPipeError:
        return len(prompt)


def _review_events(stdout: str) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    before_turn = False
    diagnostics = 0
    disabled_tool_notice = (
        "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
        "enable `features.code_mode_host` and install `codex-code-mode-host`."
    )
    for line in stdout.splitlines():
        event = _json_object(line)
        kind, item = event.get("type"), event.get("item")
        if kind == "thread.started":
            before_turn = True
        elif kind == "turn.started":
            before_turn = False
        elif before_turn and kind == "item.completed" and isinstance(item, dict) and item.get("type") == "error" and item.get("message") == disabled_tool_notice:
            # A pinned CLI emits this when tools are deliberately disabled.
            # Unknown errors and every in-turn error still fail validation.
            diagnostics += 1
            continue
        events.append(event)
    return events, diagnostics


class CodexProvider:
    provider = "codex"

    def __init__(
        self,
        *,
        model: str = "gpt-5.6-sol",
        reasoning_effort: str = "high",
        timeout_sec: float = 600,
    ) -> None:
        if model not in {"gpt-5.6-sol", "gpt-5.6-terra"}:
            raise ValueError("codex_model_not_allowed")
        if reasoning_effort not in {"medium", "high"}:
            raise ValueError("codex_reasoning_effort_not_allowed")
        if isinstance(timeout_sec, bool) or not math.isfinite(timeout_sec) or timeout_sec <= 0:
            raise ValueError("codex_timeout_invalid")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_sec = timeout_sec

    def _argv(self, binary: str, instructions: Path) -> list[str]:
        argv = [
            binary, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only", "--model", self.model,
        ]
        settings = {
            "forced_login_method": "chatgpt", "model_provider": "openai",
            "model_reasoning_effort": self.reasoning_effort, "approval_policy": "never",
            "web_search": "disabled", "project_doc_max_bytes": 0,
            "model_instructions_file": str(instructions),
        }
        for key, value in settings.items():
            argv.extend(["-c", f"{key}={json.dumps(value)}"])
        for feature in _DISABLED_FEATURES:
            argv.extend(["-c", f"features.{feature}=false"])
        return [*argv, "--json", "-"]

    def complete(self, request: ProviderRequest) -> ProviderResult:
        if request.max_input_tokens <= 0 or request.max_output_tokens <= 0:
            return _error(ProviderErrorClass.BAD_REQUEST, "codex_token_budget_invalid")
        binary = os.environ.get("RESEARKA_V2_CODEX_BIN") or shutil.which("codex")
        if not binary:
            return _error(ProviderErrorClass.PROVIDER_UNAVAILABLE, "codex_binary_unavailable")
        env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
        if home := os.environ.get("RESEARKA_V2_CODEX_HOME"):
            env["CODEX_HOME"] = home
        try:
            with tempfile.TemporaryDirectory(prefix="researka-codex-") as directory:
                root = Path(directory)
                cwd = root / "cwd"
                cwd.mkdir()
                instructions = root / "instructions.md"
                instructions.write_text(
                    request.system_prompt + "\n\nReturn exactly one JSON object, without prose or "
                    "Markdown fences. Do not use tools. Use no more than "
                    f"{request.max_output_tokens} output tokens, including reasoning.\n",
                    encoding="utf-8",
                )
                stdout, stderr, code = self._run(self._argv(binary, instructions), cwd, env, request.user_prompt)
            if code:
                return _execution_error((stdout + b"\n" + stderr).decode("utf-8", errors="replace"))
            return self._parse(stdout.decode("utf-8"), request)
        except subprocess.TimeoutExpired:
            return _error(ProviderErrorClass.TIMEOUT, "codex_timeout")
        except (ValueError, RecursionError) as exc:
            reason = str(exc)
            reason = reason if re.fullmatch(r"[a-z_]{1,64}", reason) else type(exc).__name__
            return _error(ProviderErrorClass.BAD_REQUEST, f"codex_invalid_response:{reason}")
        except OSError:
            return _error(ProviderErrorClass.PROVIDER_UNAVAILABLE, "codex_transport_unavailable")

    def _run(self, argv: list[str], cwd: Path, env: dict[str, str], prompt: str) -> tuple[bytes, bytes, int]:
        process = subprocess.Popen(  # nosec B603 - fixed flags, administrator-selected executable, no shell
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=env, start_new_session=True,
        )
        try:
            return _exchange(process, prompt.encode("utf-8"), self.timeout_sec)
        finally:
            _kill_group(process)
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def _parse(self, stdout: str, request: ProviderRequest) -> ProviderResult:
        started = completed = thread_started = False
        finals: list[str] = []
        usage: dict[str, Any] = {}
        finished_items: set[str] = set()
        pending_items: set[str] = set()
        events, diagnostics = _review_events(stdout)
        for event in events:
            kind = event.get("type")
            if kind in ("error", "turn.failed"):
                return _execution_error(json.dumps(event))
            if completed:
                raise ValueError("events_after_completion")
            if kind == "thread.started" and not started and not thread_started:
                thread_started = True
            elif kind == "turn.started" and not started:
                started = True
            elif kind in ("item.started", "item.updated", "item.completed") and started:
                text = self._item(event, finished_items, pending_items)
                if text is not None:
                    finals.append(text)
            elif kind == "turn.completed" and started and not pending_items:
                usage = self._usage(event.get("usage"), request)
                completed = True
            else:
                raise ValueError("invalid_event_sequence")
        if not completed or len(finals) != 1:
            raise ValueError("incomplete_turn")
        _json_object(finals[0])
        return ProviderResult(ok=True, response=ProviderResponse(
            text=finals[0], provider=self.provider, model=self.model,
            usage=ProviderUsage(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"], cost_usd=0.0),
            metadata={"reasoning_effort": self.reasoning_effort, "transport": "codex_cli",
                      "billing": "codex_subscription", "api_spend_usd": 0.0,
                      "startup_diagnostic_count": diagnostics},
        ))

    @staticmethod
    def _item(event: dict[str, Any], finished: set[str], pending: set[str]) -> str | None:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") not in ("agent_message", "reasoning"):
            raise ValueError("codex_tool_or_unknown_item")
        identity = item.get("id")
        if not isinstance(identity, str) or not identity or identity in finished:
            raise ValueError("invalid_item_identity")
        if event["type"] != "item.completed":
            pending.add(identity)
            return None
        pending.discard(identity)
        finished.add(identity)
        if item["type"] == "agent_message":
            if not isinstance(item.get("text"), str):
                raise ValueError("invalid_final_text")
            return str(item["text"])
        return None

    @staticmethod
    def _usage(value: Any, request: ProviderRequest) -> dict[str, int]:
        if not isinstance(value, dict) or not {"input_tokens", "output_tokens"} <= value.keys():
            raise ValueError("missing_usage")
        if any(type(count) is not int or count < 0 for count in value.values()):
            raise ValueError("invalid_usage")
        if value.get("cached_input_tokens", 0) > value["input_tokens"]:
            raise ValueError("invalid_cached_usage")
        if value["input_tokens"] > request.max_input_tokens or value["output_tokens"] > request.max_output_tokens:
            raise ValueError("usage_budget_exceeded")
        return value
