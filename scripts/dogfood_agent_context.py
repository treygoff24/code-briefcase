#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def run_cli(
    args: list[str], *, input_payload: dict | None = None, cwd: Path | None = None
) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "code_briefcase.cli", *args],
        input=json.dumps(input_payload) if input_payload is not None else None,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=30,
    )
    if result.returncode != 0:
        return {"ok": False, "stderr": result.stderr, "stdout": result.stdout}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"text": result.stdout}
    return {"ok": True, "payload": payload}


def daemon_context(project: Path) -> dict:
    from code_briefcase.daemon import query_daemon

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "code_briefcase.cli",
            "daemon",
            "start",
            "--project",
            str(project),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        last_error: Any = None
        for _ in range(30):
            try:
                result = query_daemon(
                    project,
                    {
                        "cmd": "context",
                        "entry": "main",
                        "language": "python",
                        "depth": 1,
                    },
                )
                if result.get("status") == "ok" and "main" in json.dumps(result):
                    return {"ok": True, "payload": result}
                last_error = result
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.1)
        return {"ok": False, "error": last_error}
    finally:
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
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.poll() is None:
            proc.terminate()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tldr-agent-context-") as tmp:
        project = Path(tmp)
        source = project / "app.py"
        source.write_text(
            "def helper(value: int) -> int:\n"
            "    return value + 1\n\n"
            "def main() -> int:\n"
            "    return helper(41)\n"
            + "\n".join(f"FILLER_{i} = {i}" for i in range(300))
        )

        claude_read = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "app.py"},
            "cwd": str(project),
        }
        codex_apply_patch = {
            "event": "preToolUse",
            "toolName": "apply_patch",
            "toolInput": {
                "command": "*** Begin Patch\n*** Update File: app.py\n@@\n def main() -> int:\n*** End Patch"
            },
            "cwd": str(project),
        }
        codex_post = {
            "event": "postToolUse",
            "toolName": "apply_patch",
            "toolInput": codex_apply_patch["toolInput"],
            "cwd": str(project),
        }

        results = {
            "pack": run_cli(
                [
                    "pack",
                    "main",
                    "--project",
                    str(project),
                    "--file",
                    "app.py",
                    "--json",
                ]
            ),
            "claude_pre_read": run_cli(
                ["hooks", "run", "pre-read", "--client", "claude"],
                input_payload=claude_read,
            ),
            "codex_apply_patch_pre_edit": run_cli(
                ["hooks", "run", "pre-edit", "--client", "codex"],
                input_payload=codex_apply_patch,
            ),
            "codex_post_edit": run_cli(
                ["hooks", "run", "post-edit", "--client", "codex"],
                input_payload=codex_post,
            ),
            "post_edit": run_cli(
                ["hooks", "run", "post-edit", "--client", "claude"],
                input_payload={**claude_read, "tool_name": "Edit"},
            ),
            "daemon_context": daemon_context(project),
            "global_config_written": False,
        }

        summary = {
            key: (
                ("ok" if value.get("ok") else "failed")
                if isinstance(value, dict)
                else value
            )
            for key, value in results.items()
        }
        print(json.dumps(summary, indent=2))
        return (
            0
            if all(value == "ok" or value is False for value in summary.values())
            else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())
