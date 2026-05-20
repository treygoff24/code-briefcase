from __future__ import annotations

from pathlib import Path
from typing import Any

from tldr.api import extract_file
from tldr.hooks.outcome import HookExecutionResult, ok, skipped
from tldr.hooks.runtime import HookEvent, HookResponse

CODE_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".cxx",
    ".hpp",
    ".rb",
    ".php",
    ".kt",
    ".swift",
    ".cs",
    ".scala",
    ".ex",
    ".exs",
    ".lua",
    ".luau",
}
BYPASS_SUFFIXES = (
    ".test.py",
    "_test.py",
    ".spec.ts",
    ".test.ts",
    ".spec.tsx",
    ".test.tsx",
    ".spec.js",
    ".test.js",
    ".spec.jsx",
    ".test.jsx",
)
BYPASS_PARTS = {
    ".git",
    ".tldr",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
}
SECRET_PARTS = {".env", "secrets", "secret", "credentials", "id_rsa", "id_ed25519"}


def resolve_event_path(event: HookEvent, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = event.cwd / path
    return path.resolve()


def _is_test_file(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith("test_") or any(name.endswith(suffix) for suffix in BYPASS_SUFFIXES)


def _looks_secret(path: Path) -> bool:
    lowered = [part.lower() for part in path.parts]
    return any(part in SECRET_PARTS for part in lowered) or any(
        "secret" in part or "credential" in part for part in lowered
    )


def should_bypass_read(file_path: Path, tool_input: dict[str, Any]) -> bool:
    if file_path.suffix.lower() not in CODE_EXTENSIONS:
        return True
    if set(file_path.parts) & BYPASS_PARTS:
        return True
    if _looks_secret(file_path) or _is_test_file(file_path):
        return True
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
        if not file_path.exists() or file_path.stat().st_size < 1500:
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
    trigger = [str(file_path.relative_to(event.cwd))] if file_path is not None else []
    if file_path is None or should_bypass_read(file_path, event.tool_input):
        return skipped(reason="bypass", trigger_files=trigger)

    try:
        info = extract_file(str(file_path), base_path=str(event.cwd))
    except Exception:
        return skipped(reason="extract_failed", trigger_files=trigger)


    context = format_nav_map(file_path, info, budget=budget)
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
        )

    return ok(
        HookResponse(message=context, additional_context=context, suppress_output=False),
        trigger_files=trigger,
    )
