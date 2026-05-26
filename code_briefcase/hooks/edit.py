from __future__ import annotations

import re
from pathlib import Path

from code_briefcase.hooks.file_context import build_file_context_for_path
from code_briefcase.hooks.outcome import (
    HookExecutionResult,
    event_relative_path,
    ok,
    skipped,
)
from code_briefcase.hooks.path_policy import resolve_event_path
from code_briefcase.hooks.runtime import HookEvent, HookResponse

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "Update", "apply_patch"}

# Clients for which pre-edit context is suppressed entirely.
# Codex is handled by the apply_patch skip above; this set is for future
# tuning if real-world data demands suppressing additional clients.
CLIENTS_PRE_EDIT_CONTEXT_SUPPRESSED: frozenset[str] = frozenset()


def extract_target_file(event: HookEvent) -> Path | None:
    for key in ("file_path", "path"):
        path = resolve_event_path(event, event.tool_input.get(key))
        if path is not None:
            return path
    for path in extract_apply_patch_paths(event):
        return path
    return None


def extract_apply_patch_paths(event: HookEvent) -> list[Path]:
    """Extract touched files from Codex apply_patch hook input."""
    if event.tool_name != "apply_patch":
        return []

    command = str(
        event.tool_input.get("command")
        or event.tool_input.get("cmd")
        or event.tool_input.get("patch")
        or ""
    )
    paths: list[Path] = []
    seen: set[Path] = set()
    patterns = (
        r"^\*\*\* (?:Update|Add|Delete) File: (.+)$",
        r"^\*\*\* (?:Move|Rename) to: (.+)$",
        r"^\+\+\+ b/(.+)$",
        r"^--- a/(.+)$",
    )
    for line in command.splitlines():
        for pattern in patterns:
            match = re.match(pattern, line.strip())
            if not match:
                continue
            raw_path = match.group(1).strip()
            if raw_path == "/dev/null":
                continue
            path = resolve_event_path(event, raw_path)
            if path is not None and path not in seen:
                paths.append(path)
                seen.add(path)
            break
    return paths


SYMBOL_PATTERN = re.compile(r"\b(?:def|class|function)\s+([A-Za-z_][\w]*)")


def _first_symbol_in_post_edit_value(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("new_string", "content", "text", "edits"):
            symbol = _first_symbol_in_post_edit_value(value.get(key))
            if symbol:
                return symbol
        return None
    if isinstance(value, list):
        for item in value:
            symbol = _first_symbol_in_post_edit_value(item)
            if symbol:
                return symbol
        return None
    match = SYMBOL_PATTERN.search(str(value or ""))
    return match.group(1) if match else None


def _likely_symbol(tool_input: dict) -> str | None:
    """Return a symbol visible in the pending edit's post-edit text.

    Do not infer from old_string alone: for renames/deletions, naming the
    pre-edit symbol can imply it will still exist after the edit.
    """
    for key in ("new_string", "content", "text", "edits"):
        symbol = _first_symbol_in_post_edit_value(tool_input.get(key))
        if symbol:
            return symbol
    return None


def build_pre_edit_response(
    event: HookEvent, budget: int = 2000
) -> HookExecutionResult:
    if event.tool_name not in EDIT_TOOLS:
        return skipped(reason="wrong_tool")
    if event.tool_name == "apply_patch":
        # Codex shows the diff inline; pre-edit context causes more confusion
        # than orientation. Post-edit diagnostics still run.
        return skipped(reason="apply_patch_pre_edit_suppressed")
    if event.client in CLIENTS_PRE_EDIT_CONTEXT_SUPPRESSED:
        return skipped(reason="client_pre_edit_suppressed")

    file_path = extract_target_file(event)
    trigger_path = event_relative_path(event, file_path)
    trigger = [trigger_path] if trigger_path is not None else []
    if file_path is None:
        return skipped(reason="bypass", trigger_files=trigger)

    result = build_file_context_for_path(event, file_path, mode="edit", budget=budget)
    if result.status != "ok":
        return skipped(
            reason=result.reason or "bypass", trigger_files=result.trigger_files
        )

    context = result.context or ""
    symbol = _likely_symbol(event.tool_input)
    if symbol:
        context += (
            f"\n\nYour pending edit introduces or modifies: {symbol} "
            "(inferred from the post-edit text; the file structure above is a pre-edit snapshot)."
        )

    return ok(
        HookResponse(
            message=context, additional_context=context, suppress_output=False
        ),
        trigger_files=result.trigger_files,
        recommended_files=result.recommended_files,
        surfaced_files=result.surfaced_files,
        candidate_files=result.candidate_files,
        context_kind=result.context_kind,
    )
