#!/usr/bin/env python3
"""Manual perception eval for pre-edit hook framing.

Generates the rendered pre-edit and post-edit hook JSON a model would see
for a representative Edit, writes both to reports/, and prints copy/paste
delegate commands for running the perception check against Codex and Qwen.

The delegate calls are intentionally NOT executed here — they need a live
delegate CLI, real API credentials, and parsing for "yes"/"no" answers,
none of which belong in a script that may run from CI or a hook. Run the
printed commands manually and confirm both models answer "NO".

Usage:
    python3 scripts/perception_eval.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_briefcase.hooks.edit import build_pre_edit_response  # noqa: E402
from code_briefcase.hooks.post_edit import build_post_edit_response  # noqa: E402
from code_briefcase.hooks.runtime import (  # noqa: E402
    parse_hook_event,
    render_hook_response,
)

TARGET = Path("code_briefcase/hooks/edit.py")
PROMPT_TEMPLATE = (
    "You are reviewing hook output shown to an agent right after a tool runs. "
    "Given ONLY the JSON below, did the system block, modify, or revert the "
    "agent's pending edit? Answer YES or NO on the first line, then one "
    "sentence of rationale.\n\n{payload}"
)


def _render_pre_edit(target: Path) -> dict:
    marker = "def build_pre_edit_response"
    source = target.read_text(encoding="utf-8")
    if marker not in source:
        raise SystemExit(f"error: could not find {marker!r} in {target}")
    event = parse_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(TARGET),
                "old_string": marker,
                "new_string": marker + "  # perception-eval probe",
            },
            "cwd": str(REPO_ROOT),
        },
        client="claude",
    )
    execution = build_pre_edit_response(event)
    if execution.status != "ok":
        raise SystemExit(
            f"error: expected ok pre-edit response, got {execution.status} "
            f"({execution.noop_reason})"
        )
    return render_hook_response(
        execution.response, client="claude", event_name="PreToolUse"
    )


def _render_post_edit_clean(target: Path) -> dict:
    """Render the clean post-edit confirmation as a model would see it."""
    # Build via the real hook so the framing exactly matches what models see.
    # Diagnostics are not mocked here — for most real files there will be none,
    # which is the case we care about (clean-edit confirmation framing).
    event = parse_hook_event(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(TARGET)},
            "cwd": str(REPO_ROOT),
        },
        client="claude",
    )
    execution = build_post_edit_response(event)
    return render_hook_response(
        execution.response, client="claude", event_name="PostToolUse"
    )


def main() -> int:
    target = REPO_ROOT / TARGET
    if not target.is_file():
        print(f"error: target file missing: {target}", file=sys.stderr)
        return 1

    pre_rendered = _render_pre_edit(target)
    post_rendered = _render_post_edit_clean(target)

    reports_dir = REPO_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    pre_path = reports_dir / f"perception-eval-pre-edit-{today}.json"
    post_path = reports_dir / f"perception-eval-post-edit-{today}.json"
    pre_path.write_text(json.dumps(pre_rendered, indent=2) + "\n", encoding="utf-8")
    post_path.write_text(json.dumps(post_rendered, indent=2) + "\n", encoding="utf-8")

    pre_prompt = PROMPT_TEMPLATE.format(payload=json.dumps(pre_rendered))
    post_prompt = PROMPT_TEMPLATE.format(payload=json.dumps(post_rendered))

    print(f"Wrote pre-edit JSON to:  {pre_path}")
    print(f"Wrote post-edit JSON to: {post_path}")
    print()
    print("Manual delegate checks (run from repo root). Expected answer: NO for both.")
    print()
    for label, prompt in (("pre-edit", pre_prompt), ("post-edit", post_prompt)):
        print(f"# --- {label} ---")
        print(f"  delegate codex safe --prompt-file - <<'EOF'\n{prompt}\nEOF")
        print()
        print(f"  delegate droid qwen37 safe --prompt-file - <<'EOF'\n{prompt}\nEOF")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
