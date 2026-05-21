from __future__ import annotations

from pathlib import Path
from typing import Any

from tldr.api import extract_file
from tldr.hooks.outcome import HookExecutionResult, event_relative_path, ok, skipped
from tldr.hooks.path_policy import (
    discover_related_candidates,
    format_related_files_section,
    resolve_event_path,
    should_exclude_context_path,
)
from tldr.hooks.runtime import HookEvent, HookResponse


def should_bypass_read(file_path: Path, tool_input: dict[str, Any]) -> bool:
    if "offset" in tool_input:
        return True
    if "limit" in tool_input:
        try:
            limit = int(tool_input.get("limit") or 0)
        except (TypeError, ValueError):
            return True
        if limit < 100:
            return True
    try:
        if file_path.stat().st_size < 1500:
            return True
    except OSError:
        return True
    return False


def _truncate(text: str, budget: int) -> str:
    max_chars = max(500, budget * 4)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n... [truncated]"


def format_nav_map(file_path: Path, info: dict[str, Any], budget: int = 1200) -> str:
    rel_name = file_path.name
    lines = [f"[TLDR nav map: {rel_name}]", ""]

    imports = info.get("imports") or []
    if imports:
        lines.append("Imports:")
        for imp in imports[:12]:
            names = imp.get("names") or []
            prefix = "from " if imp.get("is_from") else ""
            suffix = f": {', '.join(names)}" if names else ""
            lines.append(f"- {prefix}{imp.get('module', '')}{suffix}")
        if len(imports) > 12:
            lines.append(f"- ... +{len(imports) - 12} more")
        lines.append("")

    functions = info.get("functions") or []
    if functions:
        lines.append("Functions:")
        for func in functions[:20]:
            doc = (func.get("docstring") or "").split("\n")[0][:100]
            signature = func.get("signature") or func.get("name")
            lines.append(f"- {signature} [L{func.get('line_number', '?')}]")
            if doc:
                lines.append(f"  # {doc}")
        if len(functions) > 20:
            lines.append(f"- ... +{len(functions) - 20} more")
        lines.append("")

    classes = info.get("classes") or []
    if classes:
        lines.append("Classes:")
        for cls in classes[:12]:
            lines.append(f"- {cls.get('signature') or cls.get('name')} [L{cls.get('line_number', '?')}]")
            for method in (cls.get("methods") or [])[:8]:
                lines.append(f"  - {method.get('signature') or method.get('name')} [L{method.get('line_number', '?')}]")
        lines.append("")

    lines.append("Read specific lines with offset=N limit=M.")
    return _truncate("\n".join(lines), budget)


def build_read_response(event: HookEvent, budget: int = 1200) -> HookExecutionResult:
    if event.tool_name != "Read":
        return skipped(reason="wrong_tool")

    raw_path = event.tool_input.get("file_path") or event.tool_input.get("path")
    file_path = resolve_event_path(event, raw_path)
    trigger_path = event_relative_path(event, file_path)
    trigger = [trigger_path] if trigger_path is not None else []
    if file_path is None or should_exclude_context_path(event.cwd, file_path) or should_bypass_read(
        file_path, event.tool_input
    ):
        return skipped(reason="bypass", trigger_files=trigger)

    try:
        info = extract_file(str(file_path), base_path=str(event.cwd))
    except Exception:
        return skipped(reason="extract_failed", trigger_files=trigger)

    candidate_files, recommended_files, surfaced_files = discover_related_candidates(
        event, file_path, info, context_kind="read_nav_map"
    )
    context = format_nav_map(file_path, info, budget=budget)
    context += format_related_files_section(surfaced_files)
    if event.client == "claude":
        updated_input = dict(event.tool_input)
        updated_input["file_path"] = str(file_path)
        updated_input.setdefault("limit", 200)
        return ok(
            HookResponse(
                permission_decision="allow",
                updated_input=updated_input,
                additional_context=context,
                suppress_output=True,
            ),
            trigger_files=trigger,
            recommended_files=recommended_files,
            surfaced_files=surfaced_files,
            candidate_files=candidate_files,
            context_kind="read_nav_map",
        )

    return ok(
        HookResponse(message=context, additional_context=context, suppress_output=False),
        trigger_files=trigger,
        recommended_files=recommended_files,
        surfaced_files=surfaced_files,
        candidate_files=candidate_files,
        context_kind="read_nav_map",
    )
