from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tldr.api import extract_file, get_imports
from tldr.hooks.read import CODE_EXTENSIONS, _looks_secret, resolve_event_path
from tldr.hooks.runtime import HookEvent, HookResponse

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "Update"}


def extract_target_file(event: HookEvent) -> Path | None:
    for key in ("file_path", "path"):
        path = resolve_event_path(event, event.tool_input.get(key))
        if path is not None:
            return path
    return None


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


def build_pre_edit_response(event: HookEvent, budget: int = 2000) -> HookResponse:
    if event.tool_name not in EDIT_TOOLS:
        return HookResponse.noop()

    file_path = extract_target_file(event)
    if file_path is None or file_path.suffix.lower() not in CODE_EXTENSIONS or _looks_secret(file_path):
        return HookResponse.noop()
    if not file_path.exists():
        return HookResponse.noop()

    try:
        info = extract_file(str(file_path), base_path=str(event.cwd))
        # Exercise the public import API as part of the edit context path. If it
        # cannot parse a language, the extracted imports above are still enough.
        try:
            get_imports(str(file_path), language=info.get("language", "python"))
        except Exception:
            pass
    except Exception:
        return HookResponse.noop()

    context = _format_structure(file_path, info, budget)
    symbol = _likely_symbol(event.tool_input)
    if symbol:
        context += f"\n\nLikely target symbol: {symbol}"

    return HookResponse(message=context, additional_context=context, suppress_output=False)
