from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from code_briefcase.api import extract_file
from code_briefcase.hooks.outcome import event_relative_path
from code_briefcase.hooks.path_policy import (
    CODE_EXTENSIONS,
    classify_context_path,
    discover_related_candidates,
    format_related_files_section,
)
from code_briefcase.hooks.runtime import HookEvent
from code_briefcase.hooks.signature_render import render_signature

NAV_CONTEXT_STATE_FILE = ".code-briefcase/cache/nav-context.json"
LEGACY_TARGETED_READ_STATE_FILE = ".code-briefcase/cache/targeted-read-context.json"
TARGETED_READ_STATE_FILE = LEGACY_TARGETED_READ_STATE_FILE
MIN_CONTEXT_FILE_BYTES = 1500
TARGETED_READ_MIN_BYTES = MIN_CONTEXT_FILE_BYTES
TARGETED_READ_BUDGET = 500
TARGETED_READ_MAX_SESSIONS = 64
TARGETED_READ_MAX_FILES_PER_SESSION = 256


def should_bypass_read(file_path: Path, tool_input: dict[str, Any]) -> bool:
    if "offset" in tool_input or "limit" in tool_input:
        return False
    try:
        if file_path.stat().st_size < MIN_CONTEXT_FILE_BYTES:
            return True
    except OSError:
        return True
    return False


def _render_symbol_signature(
    file_path: Path,
    item: dict[str, Any],
    *,
    is_method: bool = False,
) -> str:
    raw = str(item.get("signature") or item.get("name") or "")
    return render_signature(raw, file_path, is_method=is_method)


def format_nav_map(file_path: Path, info: dict[str, Any], budget: int = 1200) -> str:
    rel_name = file_path.name
    lines = [f"[Code Briefcase nav map: {rel_name}]", ""]

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
            signature = _render_symbol_signature(file_path, func)
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
            lines.append(
                f"- {_render_symbol_signature(file_path, cls)} "
                f"[L{cls.get('line_number', '?')}]"
            )
            for method in (cls.get("methods") or [])[:8]:
                lines.append(
                    f"  - {_render_symbol_signature(file_path, method, is_method=True)} "
                    f"[L{method.get('line_number', '?')}]"
                )
        lines.append("")

    lines.append("Read specific lines with offset=N limit=M.")
    return _truncate("\n".join(lines), budget)


def format_targeted_read_orientation(
    file_path: Path,
    info: dict[str, Any],
    tool_input: dict[str, Any] | None,
    budget: int = TARGETED_READ_BUDGET,
) -> str:
    """Tiny orientation for reads that already asked for a narrow line range."""
    target_line = _target_line(tool_input)
    lines = [f"[Code Briefcase targeted read orientation: {file_path.name}]"]

    imports = info.get("imports") or []
    if imports:
        sample_imports = []
        for imp in imports[:5]:
            prefix = "from " if imp.get("is_from") else ""
            sample_imports.append(f"{prefix}{imp.get('module', '')}".strip())
        lines.append(
            f"- imports: {', '.join(item for item in sample_imports if item) or '(none)'}"
        )

    symbols: list[tuple[int, str]] = []
    for func in info.get("functions") or []:
        line_number = _safe_line_number(func)
        if line_number is not None:
            symbols.append(
                (line_number, str(func.get("signature") or func.get("name") or ""))
            )
    for cls in info.get("classes") or []:
        class_line = _safe_line_number(cls)
        class_name = str(cls.get("signature") or cls.get("name") or "")
        if class_line is not None:
            symbols.append((class_line, class_name))
        for method in (cls.get("methods") or [])[:12]:
            method_line = _safe_line_number(method)
            if method_line is None:
                continue
            method_name = str(method.get("signature") or method.get("name") or "")
            symbols.append(
                (
                    method_line,
                    f"{class_name}.{method_name}" if class_name else method_name,
                )
            )

    if symbols:
        if target_line is None:
            selected = sorted(symbols)[:6]
            label = "top symbols"
        else:
            selected = sorted(symbols, key=lambda item: abs(item[0] - target_line))[:5]
            selected = sorted(selected)
            label = f"near L{target_line}"
        lines.append(f"- {label}:")
        for line_number, name in selected:
            lines.append(f"  - {name} [L{line_number}]")

    lines.append("- repeated targeted reads for this file/session are suppressed")
    return _truncate("\n".join(lines), min(budget, TARGETED_READ_BUDGET))


def _safe_line_number(item: dict[str, Any]) -> int | None:
    raw_value = item.get("line_number")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


FileContextMode = Literal["read", "edit", "shell"]
STRUCTURED_READ_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class FileContextResult:
    status: Literal["ok", "skipped"]
    reason: str | None
    context: str | None
    context_kind: str | None
    trigger_files: list[str]
    recommended_files: list[str]
    surfaced_files: list[str]
    candidate_files: list[dict[str, object]]


def _truncate(text: str, budget: int) -> str:
    max_chars = max(500, budget * 4)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n... [truncated]"


def is_targeted_read(tool_input: dict[str, Any] | None) -> bool:
    if not tool_input:
        return False
    return "offset" in tool_input or "limit" in tool_input


def _target_line(tool_input: dict[str, Any] | None) -> int | None:
    if not tool_input:
        return None
    for key in ("offset", "line", "line_number", "start_line"):
        raw_value = tool_input.get(key)
        if raw_value is None:
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _nav_state_path(project: Path) -> Path:
    return project / NAV_CONTEXT_STATE_FILE


def _legacy_targeted_state_path(project: Path) -> Path:
    return project / LEGACY_TARGETED_READ_STATE_FILE


def _file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _empty_nav_state() -> dict[str, Any]:
    return {"schema_version": 2, "sessions": {}}


def _migrate_legacy_targeted_state(
    project: Path, data: dict[str, Any]
) -> dict[str, Any]:
    migrated = _empty_nav_state()
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return migrated
    new_sessions = migrated["sessions"]
    assert isinstance(new_sessions, dict)
    for session_id, session in sessions.items():
        if not isinstance(session, dict):
            continue
        files = session.get("files")
        if not isinstance(files, dict):
            continue
        file_entries: dict[str, Any] = {}
        for rel, _surfaced_at in files.items():
            path = project / rel
            file_entries[rel] = {
                "mtime": _file_mtime(path),
                "kinds": {"targeted_read_orientation": True},
            }
        new_sessions[session_id] = {
            "updated_at": session.get("updated_at", _now_iso()),
            "files": file_entries,
        }
    return migrated


def _load_nav_state(project: Path) -> dict[str, Any]:
    path = _nav_state_path(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        legacy_path = _legacy_targeted_state_path(project)
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _empty_nav_state()
        if isinstance(legacy, dict):
            return _migrate_legacy_targeted_state(project, legacy)
        return _empty_nav_state()
    if not isinstance(data, dict):
        return _empty_nav_state()
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        data["sessions"] = {}
    return data


def _write_nav_state(project: Path, data: dict[str, Any]) -> None:
    for relative_name in (NAV_CONTEXT_STATE_FILE, LEGACY_TARGETED_READ_STATE_FILE):
        path = project / relative_name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            return


def _prune_nav_state(data: dict[str, Any]) -> None:
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        data["sessions"] = {}
        return
    if len(sessions) > TARGETED_READ_MAX_SESSIONS:
        ordered = sorted(
            sessions.items(),
            key=lambda item: (
                str(item[1].get("updated_at", "")) if isinstance(item[1], dict) else ""
            ),
            reverse=True,
        )
        data["sessions"] = dict(ordered[:TARGETED_READ_MAX_SESSIONS])
        sessions = data["sessions"]
    for session in sessions.values():
        if not isinstance(session, dict):
            continue
        files = session.get("files")
        if (
            not isinstance(files, dict)
            or len(files) <= TARGETED_READ_MAX_FILES_PER_SESSION
        ):
            continue
        ordered_files = sorted(
            files.items(), key=lambda item: str(item[1]), reverse=True
        )
        session["files"] = dict(ordered_files[:TARGETED_READ_MAX_FILES_PER_SESSION])


def nav_context_recently_surfaced(event: HookEvent, path: Path, kind: str) -> bool:
    """Return True when this session already surfaced kind for path at current mtime."""
    if not event.session_id:
        return False
    rel = event_relative_path(event, path)
    if rel is None:
        return False

    data = _load_nav_state(event.cwd)
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return False
    session = sessions.get(event.session_id)
    if not isinstance(session, dict):
        return False
    files = session.get("files")
    if not isinstance(files, dict):
        return False
    entry = files.get(rel)
    if not isinstance(entry, dict):
        return False
    if entry.get("mtime") != _file_mtime(path):
        return False
    kinds = entry.get("kinds")
    return isinstance(kinds, dict) and kind in kinds


def mark_nav_context_surfaced(event: HookEvent, path: Path, kind: str) -> None:
    """Remember that nav context kind was emitted for a session/file at this mtime."""
    if not event.session_id:
        return
    rel = event_relative_path(event, path)
    if rel is None:
        return

    data = _load_nav_state(event.cwd)
    sessions = data.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        return
    session = sessions.setdefault(
        event.session_id, {"files": {}, "updated_at": _now_iso()}
    )
    if not isinstance(session, dict):
        session = {"files": {}, "updated_at": _now_iso()}
        sessions[event.session_id] = session
    files = session.setdefault("files", {})
    if not isinstance(files, dict):
        files = {}
        session["files"] = files

    entry = files.get(rel)
    if not isinstance(entry, dict) or entry.get("mtime") != _file_mtime(path):
        entry = {"mtime": _file_mtime(path), "kinds": {}}
        files[rel] = entry
    kinds = entry.setdefault("kinds", {})
    if isinstance(kinds, dict):
        kinds[kind] = True

    session["updated_at"] = _now_iso()
    _prune_nav_state(data)
    _write_nav_state(event.cwd, data)


def targeted_read_recently_surfaced(event: HookEvent, path: Path) -> bool:
    """Return True when this session already received targeted context for path."""
    return nav_context_recently_surfaced(event, path, "targeted_read_orientation")


def mark_targeted_read_surfaced(event: HookEvent, path: Path) -> None:
    """Remember that targeted context was actually emitted for a session/file."""
    mark_nav_context_surfaced(event, path, "targeted_read_orientation")


def _targeted_read_file_size_ok(path: Path) -> bool:
    try:
        return path.stat().st_size >= TARGETED_READ_MIN_BYTES
    except OSError:
        return False


def _read_bounded_text(path: Path, max_bytes: int = STRUCTURED_READ_MAX_BYTES) -> str:
    try:
        data = path.read_bytes()[:max_bytes]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def format_html_summary(path: Path, text: str, budget: int) -> str:
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    headings = re.findall(r"<h[1-6][^>]*>(.*?)</h[1-6]>", text, re.I | re.S)
    forms = len(re.findall(r"<form\b", text, re.I))
    scripts = len(re.findall(r"<script\b", text, re.I))
    styles = len(re.findall(r"<style\b", text, re.I))
    ids = re.findall(r'\bid=["\']([^"\']+)["\']', text)[:6]
    classes = re.findall(r'\bclass=["\']([^"\']+)["\']', text)[:6]
    lines = [
        f"[Code Briefcase html summary: {path.name}]",
        f"- title: {(title.group(1).strip() if title else '(none)')[:120]}",
        f"- headings: {len(headings)}",
        f"- forms/scripts/styles: {forms}/{scripts}/{styles}",
    ]
    if headings:
        lines.append(f"- sample headings: {', '.join(h[:60] for h in headings[:4])}")
    if ids:
        lines.append(f"- ids: {', '.join(ids)}")
    if classes:
        lines.append(f"- classes: {', '.join(classes)}")
    return _truncate("\n".join(lines), budget)


def format_sql_summary(path: Path, text: str, budget: int) -> str:
    keywords = []
    for word in ("CREATE", "ALTER", "DROP", "TABLE", "FUNCTION", "INDEX", "VIEW"):
        if re.search(rf"\b{word}\b", text, re.I):
            keywords.append(word)
    tables = re.findall(
        r"\b(?:TABLE|INTO|FROM|JOIN|UPDATE)\s+([A-Za-z_][\w.]*)",
        text,
        re.I,
    )[:8]
    lines = [
        f"[Code Briefcase sql summary: {path.name}]",
        f"- keywords: {', '.join(keywords) or '(none)'}",
        f"- referenced names: {', '.join(dict.fromkeys(tables)) or '(none)'}",
    ]
    return _truncate("\n".join(lines), budget)


def format_data_summary(path: Path, text: str, budget: int) -> str:
    suffix = path.suffix.lower()
    keys: list[str] = []
    if suffix == ".json":
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                keys = list(payload.keys())[:20]
        except json.JSONDecodeError:
            keys = []
    else:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" in stripped and not stripped.startswith("-"):
                keys.append(stripped.split(":", 1)[0].strip())
            if len(keys) >= 20:
                break
    lines = [
        f"[Code Briefcase data summary: {path.name}]",
        f"- top-level keys: {', '.join(keys) or '(none)'}",
    ]
    return _truncate("\n".join(lines), budget)


def format_shell_summary(path: Path, text: str, budget: int) -> str:
    shebang = text.splitlines()[0].strip() if text else ""
    functions = re.findall(
        r"^\s*(?:function\s+)?([A-Za-z_][\w]*)\s*\(\)\s*\{", text, re.M
    )[:12]
    commands = re.findall(
        r"^\s*(?:sudo\s+)?([A-Za-z_][\w-]+)\b",
        text,
        re.M,
    )
    command_counts: dict[str, int] = {}
    for command in commands:
        if command in {"if", "then", "else", "fi", "for", "do", "done", "case", "esac"}:
            continue
        command_counts[command] = command_counts.get(command, 0) + 1
    top_commands = sorted(
        command_counts, key=lambda command: command_counts[command], reverse=True
    )[:8]
    lines = [
        f"[Code Briefcase shell summary: {path.name}]",
        f"- shebang: {shebang or '(none)'}",
        f"- functions: {', '.join(functions) or '(none)'}",
        f"- commands: {', '.join(top_commands) or '(none)'}",
    ]
    return _truncate("\n".join(lines), budget)


def format_config_summary(path: Path, text: str, budget: int) -> str:
    patterns: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped[:120])
        if len(patterns) >= 12:
            break
    lines = [
        f"[Code Briefcase config summary: {path.name}]",
        "- patterns:",
    ]
    lines.extend(f"  - {item}" for item in patterns or ["(empty)"])
    return _truncate("\n".join(lines), budget)


def _format_edit_structure(file_path: Path, info: dict[str, Any], budget: int) -> str:
    lines = [
        f"[Code Briefcase pre-edit context: {file_path.name}]",
        "",
        "Pre-existing file structure:",
    ]
    for func in (info.get("functions") or [])[:30]:
        lines.append(
            f"- {_render_symbol_signature(file_path, func)} "
            f"[L{func.get('line_number', '?')}]"
        )
    for cls in (info.get("classes") or [])[:15]:
        lines.append(
            f"- {_render_symbol_signature(file_path, cls)} "
            f"[L{cls.get('line_number', '?')}]"
        )
        for method in (cls.get("methods") or [])[:8]:
            lines.append(
                f"  - {_render_symbol_signature(file_path, method, is_method=True)} "
                f"[L{method.get('line_number', '?')}]"
            )
    imports = info.get("imports") or []
    if imports:
        lines.extend(["", "Pre-existing imports:"])
        for imp in imports[:15]:
            names = imp.get("names") or []
            suffix = f": {', '.join(names)}" if names else ""
            prefix = "from " if imp.get("is_from") else ""
            lines.append(f"- {prefix}{imp.get('module', '')}{suffix}")
    text = "\n".join(lines)
    max_chars = max(200, budget * 4)
    if len(text) > max_chars:
        return text[: max_chars - 20].rstrip() + "\n... [truncated]"
    return text


def _structured_summary(
    path: Path, text: str, decision_reason: str, budget: int
) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        return format_html_summary(path, text, budget), "html_summary"
    if suffix == ".sql":
        return format_sql_summary(path, text, budget), "sql_summary"
    if suffix in {".yaml", ".yml", ".json"}:
        return format_data_summary(path, text, budget), "data_summary"
    if suffix == ".sh":
        return format_shell_summary(path, text, budget), "shell_summary"
    if decision_reason == "ok_config":
        return format_config_summary(path, text, budget), "config_summary"
    return format_config_summary(path, text, budget), "config_summary"


def build_file_context_for_path(
    event: HookEvent,
    path: Path,
    *,
    mode: FileContextMode,
    budget: int,
    tool_input: dict[str, Any] | None = None,
) -> FileContextResult:
    trigger_path = event_relative_path(event, path)
    trigger = [trigger_path] if trigger_path is not None else []
    decision = classify_context_path(event.cwd, path)
    if not decision.allowed:
        return FileContextResult(
            status="skipped",
            reason=decision.reason,
            context=None,
            context_kind=None,
            trigger_files=trigger,
            recommended_files=[],
            surfaced_files=[],
            candidate_files=[],
        )

    if decision.file_kind in {"code", "test"} or path.suffix.lower() in CODE_EXTENSIONS:
        targeted_read = mode == "read" and is_targeted_read(tool_input)
        if targeted_read and not _targeted_read_file_size_ok(path):
            return FileContextResult(
                status="skipped",
                reason="targeted_read_small_file",
                context=None,
                context_kind=None,
                trigger_files=trigger,
                recommended_files=[],
                surfaced_files=[],
                candidate_files=[],
            )
        if targeted_read and targeted_read_recently_surfaced(event, path):
            return FileContextResult(
                status="skipped",
                reason="targeted_read_recently_surfaced",
                context=None,
                context_kind=None,
                trigger_files=trigger,
                recommended_files=[],
                surfaced_files=[],
                candidate_files=[],
            )
        read_nav_kind = "read_nav_map"
        edit_nav_kind = "edit_structure"
        if mode == "read" and not targeted_read:
            if nav_context_recently_surfaced(event, path, read_nav_kind):
                return FileContextResult(
                    status="skipped",
                    reason="read_nav_map_recently_surfaced",
                    context=None,
                    context_kind=None,
                    trigger_files=trigger,
                    recommended_files=[],
                    surfaced_files=[],
                    candidate_files=[],
                )
        if mode == "edit":
            if nav_context_recently_surfaced(event, path, edit_nav_kind):
                return FileContextResult(
                    status="skipped",
                    reason="edit_structure_recently_surfaced",
                    context=None,
                    context_kind=None,
                    trigger_files=trigger,
                    recommended_files=[],
                    surfaced_files=[],
                    candidate_files=[],
                )
            if nav_context_recently_surfaced(event, path, read_nav_kind):
                return FileContextResult(
                    status="skipped",
                    reason="read_nav_map_recently_surfaced",
                    context=None,
                    context_kind=None,
                    trigger_files=trigger,
                    recommended_files=[],
                    surfaced_files=[],
                    candidate_files=[],
                )
        if (
            mode == "read"
            and tool_input is not None
            and should_bypass_read(path, tool_input)
        ):
            return FileContextResult(
                status="skipped",
                reason="bypass",
                context=None,
                context_kind=None,
                trigger_files=trigger,
                recommended_files=[],
                surfaced_files=[],
                candidate_files=[],
            )
        try:
            info = extract_file(str(path), base_path=str(event.cwd))
        except Exception:
            return FileContextResult(
                status="skipped",
                reason="extract_failed",
                context=None,
                context_kind=None,
                trigger_files=trigger,
                recommended_files=[],
                surfaced_files=[],
                candidate_files=[],
            )
        if targeted_read:
            context = format_targeted_read_orientation(
                path, info, tool_input, budget=budget
            )
            context_kind = "targeted_read_orientation"
            mark_targeted_read_surfaced(event, path)
            return FileContextResult(
                status="ok",
                reason=None,
                context=context,
                context_kind=context_kind,
                trigger_files=trigger,
                recommended_files=[],
                surfaced_files=[],
                candidate_files=[],
            )

        candidate_files, recommended_files, surfaced_files = (
            discover_related_candidates(
                event,
                path,
                info,
                context_kind="read_nav_map" if mode == "read" else "edit_structure",
            )
        )
        if mode == "read":
            context = format_nav_map(path, info, budget=budget)
            context += format_related_files_section(surfaced_files)
            context_kind = read_nav_kind
            mark_nav_context_surfaced(event, path, read_nav_kind)
        else:
            context = _format_edit_structure(path, info, budget)
            context += format_related_files_section(surfaced_files)
            context_kind = edit_nav_kind
            mark_nav_context_surfaced(event, path, edit_nav_kind)
        return FileContextResult(
            status="ok",
            reason=None,
            context=context,
            context_kind=context_kind,
            trigger_files=trigger,
            recommended_files=recommended_files,
            surfaced_files=surfaced_files,
            candidate_files=candidate_files,
        )

    text = _read_bounded_text(path)
    context, context_kind = _structured_summary(path, text, decision.reason, budget)
    if mode == "edit":
        context = context.replace(
            "[Code Briefcase ", "[Code Briefcase pre-edit context — ", 1
        )
        context += (
            "\n(Pre-edit snapshot only. This hook does NOT block or modify "
            "your edit. The post-edit hook will confirm completion.)"
            "\n- keep edits minimal and preserve existing structure"
        )
    return FileContextResult(
        status="ok",
        reason=None,
        context=context,
        context_kind=context_kind,
        trigger_files=trigger,
        recommended_files=[],
        surfaced_files=[],
        candidate_files=[],
    )
