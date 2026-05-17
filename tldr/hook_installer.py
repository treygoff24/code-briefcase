from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tldr import __version__

TLDR_MARKER = "tldr hooks run"
LEGACY_MARKERS = ("tldr-read.mjs", "post-edit-diagnostics.mjs")


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
    if client == "claude":
        return Path("~/.claude/settings.json").expanduser()
    if client == "codex":
        return Path("~/.codex/hooks.json").expanduser()
    raise ValueError(f"Unsupported client: {client}")


def load_json(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text())


def backup_file(path: str | Path) -> Path:
    source = Path(path).expanduser()
    timestamp = time.strftime("%Y%m%d%H%M%S")
    backup = source.with_name(f"{source.name}.bak-{timestamp}")
    shutil.copy2(source, backup)
    return backup


def _quote_command(executable: str, *args: str) -> str:
    import shlex

    return " ".join([shlex.quote(executable), *(shlex.quote(arg) for arg in args)])


def _resolve_tldr_path(tldr_path: str | None = None) -> str:
    candidate = tldr_path or shutil.which("tldr")
    if not candidate:
        raise FileNotFoundError("Could not find tldr executable on PATH")
    return str(Path(candidate).expanduser().resolve())


def _command(tldr_path: str, event_name: str, client: str) -> str:
    return _quote_command(tldr_path, "hooks", "run", event_name, "--client", client)


def _hook(command: str, timeout: int = 10, status_message: str | None = None) -> dict[str, Any]:
    hook = {"type": "command", "command": command, "timeout": timeout}
    if status_message:
        hook["statusMessage"] = status_message
    return hook


def _desired_groups(client: str, tldr_path: str) -> dict[str, list[dict[str, Any]]]:
    codex = client == "codex"

    def group(matcher: str, event: str, status: str) -> dict[str, Any]:
        command = _command(tldr_path, event, client)
        hook = _hook(command, status_message=status if codex else None)
        return {"matcher": matcher, "hooks": [hook]}

    if codex:
        return {
            "SessionStart": [group(".*", "session-start", "TLDR starting context")],
            "PreToolUse": [
                group("^Read$", "pre-read", "TLDR building read map"),
                group("^(Edit|Write|MultiEdit|Update)$", "pre-edit", "TLDR building edit context"),
            ],
            "PostToolUse": [
                group("^(Edit|Write|MultiEdit|Update)$", "post-edit", "TLDR checking edited file")
            ],
        }

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
        or bool(re.search(r"\bhooks\s+run\s+(session-start|pre-read|pre-edit|post-edit)\b", command))
        or any(marker in command for marker in LEGACY_MARKERS)
    )


def _group_hooks(group: dict[str, Any]) -> list[dict[str, Any]]:
    hooks = group.get("hooks")
    return hooks if isinstance(hooks, list) else []


def merge_hook_group(
    existing: dict[str, Any],
    desired: dict[str, list[dict[str, Any]]],
    marker: str = TLDR_MARKER,
) -> tuple[dict[str, Any], list[str]]:
    merged = dict(existing)
    hooks_root = dict(merged.get("hooks") or {})
    actions: list[str] = []

    for event, desired_groups in desired.items():
        groups = [dict(group) for group in hooks_root.get(event, [])]
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
                    if any(
                        any(marker in str(hook.get("command", "")) for marker in LEGACY_MARKERS)
                        for hook in old_hooks
                    ):
                        actions.append(f"replace legacy TLDR hook for {event} {matcher}")
                    else:
                        actions.append(f"replace TLDR hook for {event} {matcher}")
                group["hooks"] = kept_hooks + desired_group["hooks"]
                break
            else:
                groups.append(desired_group)
                actions.append(f"add TLDR hook for {event} {matcher}")
        hooks_root[event] = groups

    merged["hooks"] = hooks_root
    return merged, actions


def _resolved_config_path(client: str, config_path: str | None) -> Path:
    path = Path(config_path).expanduser() if config_path else default_config_path(client)
    return path.resolve() if path.exists() else path


def install_hooks(
    client: str,
    scope: str = "global",
    config_path: str | None = None,
    dry_run: bool = False,
    *,
    tldr_path: str | None = None,
) -> InstallResult:
    if scope != "global":
        raise ValueError("Only global hook scope is currently supported")

    path = _resolved_config_path(client, config_path)
    executable = _resolve_tldr_path(tldr_path)
    existing = load_json(path)
    desired = _desired_groups(client, executable)
    merged, actions = merge_hook_group(existing, desired)
    changed = merged != existing
    backup_path = None

    if changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup_path = backup_file(path)
        path.write_text(json.dumps(merged, indent=2) + "\n")

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
    clients = clients or ["claude", "codex"]
    report: dict[str, Any] = {
        "version": __version__,
        "tldr": shutil.which("tldr"),
        "tldr_mcp": shutil.which("tldr-mcp"),
        "clients": {},
        "semantic_index_present": (Path(project) / ".tldr" / "cache" / "semantic" / "index.faiss").exists(),
    }

    for client in clients:
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
