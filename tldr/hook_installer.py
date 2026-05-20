from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tldr import __version__
from tldr.command_exec import expand_shebang_command

TLDR_MARKER = "tldr hooks run"
LEGACY_MARKERS = ("tldr-read.mjs", "post-edit-diagnostics.mjs")
CLAUDE_PROFILE_CONFIGS = {
    "claude": "~/.claude/settings.json",
    "claude-work": "~/.claude-work/settings.json",
    "claude-personal": "~/.claude-personal/settings.json",
    "claude-space": "~/.claude-space/settings.json",
}
DEFAULT_DOCTOR_CLIENTS = [
    "claude",
    "claude-work",
    "claude-personal",
    "claude-space",
    "codex",
    "droid",
    "factory",
    "opencode",
]
SHELL_HOOK_CLIENTS = {"claude", "codex", "droid", "factory"}
TELEMETRY_ENV_PREFIX = "TLDR_TELEMETRY=1 TLDR_TELEMETRY_REDACT_PATHS=1"
TldrCommand = list[str]


@dataclass
class InstallResult:
    client: str
    config_path: Path
    dry_run: bool
    changed: bool
    backup_path: Path | None = None
    actions: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        mode = "Dry run" if self.dry_run else "Install"
        lines = [
            f"{mode}: {self.client} hooks",
            f"Config: {self.config_path}",
            f"Changed: {str(self.changed).lower()}",
        ]
        if self.backup_path:
            lines.append(f"Backup: {self.backup_path}")
        if self.actions:
            lines.append("Actions:")
            lines.extend(f"- {action}" for action in self.actions)
        return "\n".join(lines)


def default_config_path(client: str) -> Path:
    if client in CLAUDE_PROFILE_CONFIGS:
        return Path(CLAUDE_PROFILE_CONFIGS[client]).expanduser()
    if client == "codex":
        return Path("~/.codex/hooks.json").expanduser()
    if client in ("droid", "factory"):
        return Path("~/.factory/settings.json").expanduser()
    if client == "opencode":
        return Path("~/.config/opencode/plugins/tldr-hooks.js").expanduser()
    raise ValueError(f"Unsupported client: {client}")


def load_json(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text())


def _existing_mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def backup_file(path: str | Path) -> Path:
    source = Path(path).expanduser()
    timestamp = time.strftime("%Y%m%d%H%M%S")
    backup = source.with_name(f"{source.name}.bak-{timestamp}")
    shutil.copy2(source, backup)
    return backup


def _quote_command(*parts: str) -> str:
    import shlex

    return " ".join(shlex.quote(part) for part in parts)


def _resolve_tldr_command(tldr_path: str | None = None) -> TldrCommand:
    candidates = _candidate_tldr_commands(tldr_path)
    if not candidates:
        raise FileNotFoundError("Could not find tldr executable on PATH")

    errors: list[str] = []
    for command in candidates:
        try:
            _validate_tldr_hooks_command(command)
            return command
        except RuntimeError as exc:
            errors.append(str(exc))

    raise RuntimeError("; ".join(errors))


def _candidate_tldr_commands(tldr_path: str | None = None) -> list[TldrCommand]:
    if tldr_path:
        return [[str(Path(tldr_path).expanduser().resolve())]]

    path_tldr = shutil.which("tldr")
    candidates = [
        [str(Path(sys.executable).with_name("tldr"))],
        [sys.executable, "-m", "tldr.cli"],
        [str(Path(path_tldr).expanduser())] if path_tldr else None,
    ]
    resolved: list[TldrCommand] = []
    seen: set[tuple[str, ...]] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        executable = Path(candidate[0]).expanduser()
        if not executable.exists():
            continue
        command = [str(executable.resolve()), *candidate[1:]]
        key = tuple(command)
        if key not in seen:
            resolved.append(command)
            seen.add(key)
    return resolved


def _validate_tldr_hooks_command(command: TldrCommand) -> None:
    """Fail before installing hook commands that the target tldr cannot run."""
    validation_command = [*command, "hooks", "--help"]
    try:
        result = subprocess.run(
            expand_shebang_command(validation_command),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not validate TLDR hooks command: {_quote_command(*command)}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        raise RuntimeError(
            f"TLDR command does not support 'tldr hooks': {_quote_command(*command)}{suffix}"
        )


def _command(tldr_command: TldrCommand, event_name: str, client: str) -> str:
    command = _quote_command(*tldr_command, "hooks", "run", event_name, "--client", client)
    if client in SHELL_HOOK_CLIENTS:
        return f"{TELEMETRY_ENV_PREFIX} {command}"
    return command


def _runtime_client(client: str) -> str:
    if client in CLAUDE_PROFILE_CONFIGS:
        return "claude"
    return client


def _hook(command: str, timeout: int = 10, status_message: str | None = None) -> dict[str, Any]:
    hook = {"type": "command", "command": command, "timeout": timeout}
    if status_message:
        hook["statusMessage"] = status_message
    return hook


def _desired_groups(
    client: str,
    tldr_command: TldrCommand,
    *,
    enable_prompt_guard: bool = False,
    enable_tool_guard: bool = False,
    enable_compact_context: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    runtime_client = _runtime_client(client)
    is_codex = runtime_client == "codex"
    is_droid = runtime_client in ("droid", "factory")

    def group(matcher: str, event: str, status: str) -> dict[str, Any]:
        command = _command(tldr_command, event, runtime_client)
        hook = _hook(command, status_message=status if is_codex else None)
        return {"matcher": matcher, "hooks": [hook]}

    if is_codex:
        groups: dict[str, list[dict[str, Any]]] = {
            "SessionStart": [group("startup|resume|clear", "session-start", "TLDR starting context")],
            "PreToolUse": [
                group("apply_patch|Edit|Write", "pre-edit", "TLDR building edit context"),
            ],
            "PostToolUse": [
                group("apply_patch|Edit|Write", "post-edit", "TLDR checking edited file")
            ],
        }
        if enable_prompt_guard:
            groups["UserPromptSubmit"] = [group(".*", "user-prompt-submit", "TLDR prompt guard")]
        if enable_tool_guard:
            groups.setdefault("PreToolUse", []).append(
                group("Bash", "pre-tool", "TLDR tool guard")
            )
            groups["PermissionRequest"] = [
                group(
                    "Bash|apply_patch|Edit|Write|mcp__.*",
                    "permission-request",
                    "TLDR permission guard",
                )
            ]
        return groups

    if is_droid:
        groups = {
            "SessionStart": [
                group("startup|resume|clear|compact", "session-start", "")
            ],
            "PreToolUse": [
                group("Read", "pre-read", ""),
                group("Edit|Create|ApplyPatch", "pre-edit", ""),
            ],
            "PostToolUse": [
                group("Edit|Create|ApplyPatch", "post-edit", "")
            ],
        }
        if enable_prompt_guard:
            groups["UserPromptSubmit"] = [group(".*", "user-prompt-submit", "")]
        if enable_tool_guard:
            groups.setdefault("PreToolUse", []).append(
                group("Execute", "pre-tool", "")
            )
        if enable_compact_context:
            groups["PreCompact"] = [group("manual|auto", "pre-compact", "")]
        return groups

    # Claude (default)
    return {
        "SessionStart": [group(".*", "session-start", "")],
        "PreToolUse": [
            group("Read", "pre-read", ""),
            group("Edit|Write|MultiEdit|Update", "pre-edit", ""),
        ],
        "PostToolUse": [group("Edit|Write|MultiEdit|Update", "post-edit", "")],
    }


def _is_tldr_owned(command: str) -> bool:
    return (
        TLDR_MARKER in command
        or bool(
            re.search(
                r"\bhooks\s+run\s+(session-start|pre-read|pre-edit|post-edit|user-prompt-submit|permission-request|pre-tool|post-tool|stop|session-end|notification|subagent-start|subagent-stop|pre-compact)\b",
                command,
            )
        )
        or any(marker in command for marker in LEGACY_MARKERS)
    )


def _managed_events(client: str) -> set[str]:
    runtime_client = _runtime_client(client)
    if runtime_client == "codex":
        return {"SessionStart", "PreToolUse", "PostToolUse", "UserPromptSubmit", "PermissionRequest"}
    if runtime_client in ("droid", "factory"):
        return {"SessionStart", "PreToolUse", "PostToolUse", "UserPromptSubmit", "PreCompact"}
    if runtime_client == "claude":
        return {"SessionStart", "PreToolUse", "PostToolUse"}
    return set()


def _group_hooks(group: dict[str, Any]) -> list[dict[str, Any]]:
    hooks = group.get("hooks")
    return hooks if isinstance(hooks, list) else []


def _contains_legacy_tldr_hook(hooks: list[dict[str, Any]]) -> bool:
    return any(
        any(marker in str(hook.get("command", "")) for marker in LEGACY_MARKERS)
        for hook in hooks
    )


def merge_hook_group(
    existing: dict[str, Any],
    desired: dict[str, list[dict[str, Any]]],
    marker: str = TLDR_MARKER,
    managed_events: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    merged = dict(existing)
    hooks_root = dict(merged.get("hooks") or {})
    actions: list[str] = []

    for event in sorted(set(desired) | set(managed_events or ())):
        desired_groups = desired.get(event, [])
        groups = []
        for group in hooks_root.get(event, []):
            group = dict(group)
            old_hooks = _group_hooks(group)
            kept_hooks = [
                hook for hook in old_hooks
                if not _is_tldr_owned(str(hook.get("command", "")))
            ]
            if len(kept_hooks) == len(old_hooks):
                groups.append(group)
                continue

            matcher = group.get("matcher")
            label = "legacy TLDR hook" if _contains_legacy_tldr_hook(old_hooks) else "stale TLDR hook"
            if kept_hooks:
                group["hooks"] = kept_hooks
                groups.append(group)
                actions.append(f"remove {label} from {event} {matcher}")
            else:
                actions.append(f"remove {label} group for {event} {matcher}")

        for desired_group in desired_groups:
            matcher = desired_group.get("matcher")
            for group in groups:
                if group.get("matcher") != matcher:
                    continue

                old_hooks = _group_hooks(group)
                kept_hooks = [
                    hook for hook in old_hooks
                    if not _is_tldr_owned(str(hook.get("command", "")))
                ]
                if len(kept_hooks) != len(old_hooks):
                    if _contains_legacy_tldr_hook(old_hooks):
                        actions.append(f"replace legacy TLDR hook for {event} {matcher}")
                    else:
                        actions.append(f"replace TLDR hook for {event} {matcher}")
                group["hooks"] = kept_hooks + desired_group["hooks"]
                break
            else:
                groups.append(desired_group)
                actions.append(f"add TLDR hook for {event} {matcher}")
        if groups:
            hooks_root[event] = groups
        else:
            hooks_root.pop(event, None)

    merged["hooks"] = hooks_root
    return merged, actions


def _resolved_config_path(client: str, config_path: str | None) -> Path:
    path = Path(config_path).expanduser() if config_path else default_config_path(client)
    return path.resolve() if path.exists() else path


def _is_managed_config_path(path: Path) -> bool:
    path_str = str(path)
    return (
        path_str == "/etc"
        or path_str.startswith("/etc/")
        or path_str == "/Library/Application Support"
        or path_str.startswith("/Library/Application Support/")
        or path_str == "/Library/Managed Preferences"
        or path_str.startswith("/Library/Managed Preferences/")
    )


def install_hooks(
    client: str,
    scope: str = "global",
    config_path: str | None = None,
    dry_run: bool = False,
    *,
    tldr_path: str | None = None,
    enable_prompt_guard: bool = False,
    enable_tool_guard: bool = False,
    enable_compact_context: bool = False,
) -> InstallResult:
    if scope != "global":
        raise ValueError("Only global hook scope is currently supported")

    # Cursor is experimental-only
    if client == "cursor":
        raise ValueError(
            "Cursor hook install is experimental_unverified and disabled until "
            "a local Cursor hook payload/output fixture proves the config shape."
        )

    # Reject managed/enterprise policy paths early (before reading)
    path = _resolved_config_path(client, config_path)
    if _is_managed_config_path(path):
        raise ValueError(f"Refusing to edit managed config path: {path}")

    tldr_command = _resolve_tldr_command(tldr_path)
    if client == "opencode":
        from tldr.hooks.opencode_adapter import generate_opencode_adapter
        new_content = generate_opencode_adapter(
            expand_shebang_command(tldr_command),
            enable_tool_guard=enable_tool_guard,
            enable_compact_context=enable_compact_context,
        )
        existing_content = ""
        if path.exists():
            try:
                existing_content = path.read_text()
            except Exception:
                pass
        changed = existing_content != new_content
        actions = []
        backup_path = None
        if changed:
            if existing_content:
                if "tldr" not in existing_content.lower():
                    actions.append("backup and replace existing plugin file containing non-TLDR content")
                else:
                    actions.append("replace existing TLDR-owned plugin file")
            else:
                actions.append("write generated OpenCode plugin adapter")
            if not dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    backup_path = backup_file(path)
                tmp_path = path.with_name(f".{path.name}.tmp")
                tmp_path.write_text(new_content)
                existing_mode = _existing_mode(path)
                if existing_mode is not None:
                    tmp_path.chmod(existing_mode)
                tmp_path.replace(path)
        return InstallResult(
            client=client,
            config_path=path,
            dry_run=dry_run,
            changed=changed,
            backup_path=backup_path,
            actions=actions,
            config={},
        )

    existing = load_json(path)
    desired = _desired_groups(
        client,
        tldr_command,
        enable_prompt_guard=enable_prompt_guard,
        enable_tool_guard=enable_tool_guard,
        enable_compact_context=enable_compact_context,
    )
    merged, actions = merge_hook_group(existing, desired, managed_events=_managed_events(client))
    changed = merged != existing
    if not changed:
        actions = []
    backup_path = None

    if changed and not dry_run:
        # Safety: reject managed JSON markers
        if existing.get("enterprise_managed") or existing.get("managed") or "managedPolicy" in existing:
            raise ValueError("Refusing to edit managed/enterprise policy config")

        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup_path = backup_file(path)
        tmp_path = path.with_name(f".{path.name}.tmp")
        tmp_path.write_text(json.dumps(merged, indent=2) + "\n")
        existing_mode = _existing_mode(path)
        if existing_mode is not None:
            tmp_path.chmod(existing_mode)
        tmp_path.replace(path)

    return InstallResult(
        client=client,
        config_path=path,
        dry_run=dry_run,
        changed=changed,
        backup_path=backup_path,
        actions=actions,
        config=merged,
    )


def _hooks_present(config: dict[str, Any]) -> bool:
    for groups in (config.get("hooks") or {}).values():
        for group in groups:
            for hook in _group_hooks(group):
                if _is_tldr_owned(str(hook.get("command", ""))):
                    return True
    return False


def doctor_report(
    clients: list[str] | None = None,
    project: str | Path = ".",
) -> dict[str, Any]:
    clients = clients or DEFAULT_DOCTOR_CLIENTS
    try:
        tldr_command = _quote_command(*_resolve_tldr_command())
    except Exception:
        tldr_command = shutil.which("tldr")

    report: dict[str, Any] = {
        "version": __version__,
        "tldr": tldr_command,
        "tldr_mcp": shutil.which("tldr-mcp"),
        "clients": {},
        "semantic_index_present": (Path(project) / ".tldr" / "cache" / "semantic" / "index.faiss").exists(),
    }

    for client in clients:
        is_cursor = client == "cursor"
        if is_cursor:
            report["clients"][client] = {
                "config_path": None,
                "exists": False,
                "tldr_hooks_present": False,
                "error": None,
                "status": "experimental_unverified",
            }
            continue

        is_opencode = client == "opencode"
        if is_opencode:
            path = default_config_path(client)
            exists = path.exists()
            try:
                if exists:
                    content = path.read_text()
                    hooks_present = "TLDR_COMMAND" in content or "tldr" in content.lower()
                else:
                    hooks_present = False
                error = None
            except Exception as exc:
                hooks_present = False
                error = str(exc)
            report["clients"][client] = {
                "config_path": str(path.resolve() if path.exists() else path),
                "exists": exists,
                "tldr_hooks_present": hooks_present,
                "error": error,
                "status": None,
            }
            continue

        path = default_config_path(client)
        try:
            config = load_json(path)
        except Exception as exc:
            config = {}
            error = str(exc)
        else:
            error = None
        report["clients"][client] = {
            "config_path": str(path.resolve() if path.exists() else path),
            "exists": path.exists(),
            "tldr_hooks_present": _hooks_present(config),
            "error": error,
            "status": None,
        }

    try:
        from tldr.daemon import query_daemon

        status = query_daemon(Path(project).resolve(), {"cmd": "status"})
    except Exception:
        status = {"status": "not_running"}
    report["daemon"] = status
    return report


def format_doctor_report(report: dict[str, Any]) -> str:
    lines = [
        "TLDR Hooks Doctor",
        f"version: {report.get('version')}",
        f"tldr: {report.get('tldr') or 'missing'}",
        f"tldr-mcp: {report.get('tldr_mcp') or 'missing'}",
        f"semantic index present: {str(report.get('semantic_index_present')).lower()}",
        f"daemon: {report.get('daemon', {}).get('status', 'unknown')}",
        "",
        "Clients:",
    ]
    for client, info in (report.get("clients") or {}).items():
        lines.append(
            f"- {client}: exists={str(info.get('exists')).lower()} "
            f"tldr_hooks_present={str(info.get('tldr_hooks_present')).lower()} "
            f"path={info.get('config_path')}"
        )
        if info.get("error"):
            lines.append(f"  error: {info['error']}")
    return "\n".join(lines)
