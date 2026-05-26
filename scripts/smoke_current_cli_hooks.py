#!/usr/bin/env python3
"""Smoke current agent hook integration without touching user configs."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def file_fingerprint(path: Path) -> tuple[bool, str | None, int | None]:
    path = path.expanduser()
    if not path.exists():
        return False, None, None
    data = path.read_bytes()
    return True, hashlib.sha256(data).hexdigest(), path.stat().st_mtime_ns


def run(
    args: list[str], *, input_json: dict[str, Any] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        input=json.dumps(input_json) if input_json is not None else None,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def tldr(args: list[str], *, input_json: dict[str, Any] | None = None) -> str:
    return run(
        [sys.executable, "-m", "code_briefcase.cli", *args], input_json=input_json
    ).stdout


def project_daemon_pids(project: Path) -> list[int]:
    paths = {str(project), str(project.resolve())}
    result = subprocess.run(
        ["ps", "ax", "-o", "pid=,command="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        if "code_briefcase.cli daemon start --project" not in line:
            continue
        if not any(path in line for path in paths):
            continue
        pid_text = line.strip().split(None, 1)[0]
        pids.append(int(pid_text))
    return pids


def stop_project_daemon(project: Path) -> None:
    for _ in range(5):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "code_briefcase.cli",
                "daemon",
                "stop",
                "--project",
                str(project),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        time.sleep(0.2)
        if not project_daemon_pids(project):
            return

    for pid in project_daemon_pids(project):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(10):
        time.sleep(0.1)
        if not project_daemon_pids(project):
            return

    for pid in project_daemon_pids(project):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    leftovers = project_daemon_pids(project)
    if leftovers:
        raise AssertionError(f"failed to clean up smoke daemon(s): {leftovers}")


def assert_user_configs_unchanged(
    before: dict[Path, tuple[bool, str | None, int | None]],
) -> None:
    after = {path: file_fingerprint(path) for path in before}
    if after != before:
        changed = [str(path) for path in before if before[path] != after[path]]
        raise AssertionError(f"smoke touched user config(s): {changed}")


def main() -> int:
    user_configs = {
        Path("~/.claude/settings.json").expanduser(): file_fingerprint(
            Path("~/.claude/settings.json")
        ),
        Path("~/.codex/hooks.json").expanduser(): file_fingerprint(
            Path("~/.codex/hooks.json")
        ),
        Path("~/.factory/settings.json").expanduser(): file_fingerprint(
            Path("~/.factory/settings.json")
        ),
        Path(
            "~/.config/opencode/plugins/code-briefcase-hooks.js"
        ).expanduser(): file_fingerprint(
            Path("~/.config/opencode/plugins/code-briefcase-hooks.js")
        ),
    }
    summary: dict[str, Any] = {}

    try:
        for name, command in {
            "claude_version": ["claude", "--version"],
            "codex_version": ["codex", "--version"],
            "droid_version": ["droid", "--version"],
            "opencode_version": ["opencode", "--version"],
            "cursor_agent_version": ["cursor-agent", "--version"],
        }.items():
            try:
                summary[name] = run(command).stdout.strip()
            except Exception as exc:
                summary[name] = f"unavailable: {exc}"

        with tempfile.TemporaryDirectory(prefix="tldr-hook-smoke-") as tmp:
            tmp_path = Path(tmp)
            project = tmp_path / "project"
            project.mkdir()
            project = project.resolve()
            try:
                source = project / "app.py"
                source.write_text("def main():\n    return 1\n" + "x = 1\n" * 400)

                claude_config = tmp_path / "claude-settings.json"
                codex_config = tmp_path / "codex-hooks.json"
                droid_config = tmp_path / "droid-settings.json"
                opencode_plugin = tmp_path / "tldr-hooks.js"
                tldr(["hooks", "install", "claude", "--config", str(claude_config)])
                tldr(["hooks", "install", "codex", "--config", str(codex_config)])
                tldr(
                    [
                        "hooks",
                        "install",
                        "droid",
                        "--config",
                        str(droid_config),
                        "--enable-prompt-guard",
                        "--enable-tool-guard",
                        "--enable-compact-context",
                    ]
                )
                tldr(
                    [
                        "hooks",
                        "install",
                        "opencode",
                        "--config",
                        str(opencode_plugin),
                        "--enable-tool-guard",
                        "--enable-compact-context",
                    ]
                )

                claude_config_payload = json.loads(claude_config.read_text())
                codex_config_payload = json.loads(codex_config.read_text())
                droid_config_payload = json.loads(droid_config.read_text())
                opencode_source = opencode_plugin.read_text()
                assert "hooks run pre-read" in json.dumps(claude_config_payload)
                assert "hooks run pre-read" not in json.dumps(codex_config_payload)
                assert (
                    codex_config_payload["hooks"]["PreToolUse"][0]["matcher"]
                    == "apply_patch|Edit|Write"
                )
                assert (
                    droid_config_payload["hooks"]["SessionStart"][0]["matcher"]
                    == "startup|resume|clear|compact"
                )
                assert "UserPromptSubmit" in droid_config_payload["hooks"]
                assert "PreCompact" in droid_config_payload["hooks"]
                assert "export const TLDRHooks" in opencode_source
                assert "CODE_BRIEFCASE_TIMEOUT_MS = 1500" in opencode_source
                assert '"permission.asked"' in opencode_source
                assert '"experimental.session.compacting"' in opencode_source
                summary["temp_install"] = "ok"

                claude_pre_read = json.loads(
                    tldr(
                        ["hooks", "run", "pre-read", "--client", "claude"],
                        input_json={
                            "hook_event_name": "PreToolUse",
                            "tool_name": "Read",
                            "tool_input": {"file_path": str(source)},
                            "cwd": str(project),
                        },
                    )
                )
                assert (
                    claude_pre_read["hookSpecificOutput"]["hookEventName"]
                    == "PreToolUse"
                )
                assert (
                    claude_pre_read["hookSpecificOutput"]["permissionDecision"]
                    == "allow"
                )
                summary["claude_pre_read"] = "ok"

                codex_session_start = json.loads(
                    tldr(
                        ["hooks", "run", "session-start", "--client", "codex"],
                        input_json={
                            "hook_event_name": "SessionStart",
                            "source": "startup",
                            "cwd": str(project),
                        },
                    )
                )
                assert (
                    codex_session_start["hookSpecificOutput"]["hookEventName"]
                    == "SessionStart"
                )
                assert "additionalContext" in codex_session_start["hookSpecificOutput"]
                assert "systemMessage" not in codex_session_start
                summary["codex_session_start"] = "ok"

                codex_pre_edit = json.loads(
                    tldr(
                        ["hooks", "run", "pre-edit", "--client", "codex"],
                        input_json={
                            "hook_event_name": "PreToolUse",
                            "tool_name": "apply_patch",
                            "tool_input": {
                                "command": "*** Begin Patch\n*** Update File: app.py\n@@\n def main():\n*** End Patch"
                            },
                            "cwd": str(project),
                        },
                    )
                )
                # Codex apply_patch pre-edit is suppressed: Codex already shows
                # the diff inline, so an extra nav map only adds confusion.
                # Post-edit diagnostics still run; this just means {} from the hook.
                assert codex_pre_edit == {}
                summary["codex_apply_patch_pre_edit"] = "ok"

                codex_prompt_block = json.loads(
                    tldr(
                        ["hooks", "run", "user-prompt-submit", "--client", "codex"],
                        input_json={
                            "hook_event_name": "UserPromptSubmit",
                            "prompt": "Use this key: sk-" + "A" * 48,
                            "cwd": str(project),
                        },
                    )
                )
                assert codex_prompt_block["decision"] == "block"
                assert "sk-" not in codex_prompt_block.get("reason", "")
                summary["codex_prompt_guard"] = "ok"

                droid_permission_deny = json.loads(
                    tldr(
                        ["hooks", "run", "pre-tool", "--client", "droid"],
                        input_json={
                            "hook_event_name": "PreToolUse",
                            "tool_name": "Execute",
                            "tool_input": {"command": "sudo rm -rf /"},
                            "cwd": str(project),
                        },
                    )
                )
                assert (
                    droid_permission_deny["hookSpecificOutput"]["hookEventName"]
                    == "PreToolUse"
                )
                assert (
                    droid_permission_deny["hookSpecificOutput"]["permissionDecision"]
                    == "deny"
                )
                summary["droid_permission_guard"] = "ok"

                opencode_session_start = json.loads(
                    tldr(
                        ["hooks", "run", "session-start", "--client", "opencode"],
                        input_json={
                            "hook_event_name": "SessionStart",
                            "cwd": str(project),
                        },
                    )
                )
                assert (
                    opencode_session_start["hookSpecificOutput"]["hookEventName"]
                    == "SessionStart"
                )
                summary["opencode_adapter_json"] = "ok"
            finally:
                stop_project_daemon(project)
    finally:
        assert_user_configs_unchanged(user_configs)

    summary["user_configs_unchanged"] = True
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
