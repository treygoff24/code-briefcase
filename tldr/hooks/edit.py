from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tldr.api import extract_file, get_imports
from tldr.hooks.outcome import HookExecutionResult, event_relative_path, ok, skipped
from tldr.hooks.path_policy import (
    discover_related_candidates,
    format_related_files_section,
    resolve_event_path,
    should_exclude_context_path,
)
from tldr.hooks.runtime import HookEvent, HookResponse

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "Update", "apply_patch"}


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

    command = str(event.tool_input.get("command") or event.tool_input.get("cmd") or event.tool_input.get("patch") or "")
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


def _likely_symbol(tool_input: dict[str, Any]) -> str | None:
    text = " ".join(
        str(tool_input.get(key) or "")
        for key in ("old_string", "new_string", "content", "text")
    )
    match = re.search(r"\b(?:def|class|function)\s+([A-Za-z_][\w]*)", text)
    return match.group(1) if match else None


def _format_structure(file_path: Path, info: dict[str, Any], budget: int) -> str:
    lines = [f"[TLDR edit context: {file_path.name}]", "", "File structure:"]
    for func in (info.get("functions") or [])[:30]:
        lines.append(f"- {func.get('signature') or func.get('name')} [L{func.get('line_number', '?')}]")
    for cls in (info.get("classes") or [])[:15]:
        lines.append(f"- {cls.get('signature') or cls.get('name')} [L{cls.get('line_number', '?')}]")
        for method in (cls.get("methods") or [])[:8]:
            lines.append(f"  - {method.get('signature') or method.get('name')} [L{method.get('line_number', '?')}]")

    imports = info.get("imports") or []
    if imports:
        lines.extend(["", "Imports:"])
        for imp in imports[:15]:
            names = imp.get("names") or []
            suffix = f": {', '.join(names)}" if names else ""
            prefix = "from " if imp.get("is_from") else ""
            lines.append(f"- {prefix}{imp.get('module', '')}{suffix}")

    lines.extend(
        [
            "",
            "Before editing:",
            "- preserve signatures unless the task requires an API change",
            "- after edit, diagnostics hook will run",
        ]
    )
    text = "\n".join(lines)
    max_chars = max(200, budget * 4)
    if len(text) > max_chars:
        return text[: max_chars - 20].rstrip() + "\n... [truncated]"
    return text


def build_pre_edit_response(event: HookEvent, budget: int = 2000) -> HookExecutionResult:
    if event.tool_name not in EDIT_TOOLS:
        return skipped(reason="wrong_tool")

    file_path = extract_target_file(event)
    trigger_path = event_relative_path(event, file_path)
    trigger = [trigger_path] if trigger_path is not None else []
    if file_path is None or should_exclude_context_path(event.cwd, file_path):
        return skipped(reason="bypass", trigger_files=trigger)
    if not file_path.exists():
        return skipped(reason="missing_file", trigger_files=trigger)

    try:
        info = extract_file(str(file_path), base_path=str(event.cwd))
        try:
            get_imports(str(file_path), language=info.get("language", "python"))
        except Exception:
            pass
    except Exception:
        return skipped(reason="extract_failed", trigger_files=trigger)

    candidate_files, recommended_files, surfaced_files = discover_related_candidates(
        event, file_path, info, context_kind="edit_structure"
    )
    context = _format_structure(file_path, info, budget)
    context += format_related_files_section(surfaced_files)
    symbol = _likely_symbol(event.tool_input)
    if symbol:
        context += f"\n\nLikely target symbol: {symbol}"

    return ok(
        HookResponse(message=context, additional_context=context, suppress_output=False),
        trigger_files=trigger,
        recommended_files=recommended_files,
        surfaced_files=surfaced_files,
        candidate_files=candidate_files,
        context_kind="edit_structure",
    )
