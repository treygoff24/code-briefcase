from __future__ import annotations

import re
import shlex
from pathlib import Path

from code_briefcase.hooks.outcome import HookExecutionResult, noop, ok
from code_briefcase.hooks.runtime import HookEvent, HookResponse

# High-confidence destructive command patterns
_RF_FLAGS = r"-(?=[A-Za-z]*[rR])(?=[A-Za-z]*f)[A-Za-z]*"

_DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # rm -rf /, rm -fr /, and sudo-wrapped variants.
    (
        re.compile(rf"\brm\s+({_RF_FLAGS}\s+|--\s+)\/\s*$"),
        "recursive forced deletion of /",
    ),
    (
        re.compile(rf"\brm\s+({_RF_FLAGS}\s+|--\s+)~\s*$"),
        "recursive forced deletion of home directory",
    ),
    (
        re.compile(rf"\bsudo\s+.*\brm\s+({_RF_FLAGS}\s+|--\s+)\/\s*$"),
        "recursive forced deletion of / with sudo",
    ),
    (
        re.compile(rf"\bsudo\s+.*\brm\s+({_RF_FLAGS}\s+|--\s+)~\s*$"),
        "recursive forced deletion of home directory with sudo",
    ),
    (
        re.compile(rf"\brm\s+({_RF_FLAGS}\s+|--\s+)\$HOME\b"),
        "recursive forced deletion of $HOME",
    ),
    (
        re.compile(rf"\bsudo\s+.*\brm\s+.*{_RF_FLAGS}.*\$HOME\s*$"),
        "recursive forced deletion of $HOME with sudo",
    ),
    # Disk erase/format
    (re.compile(r"\bmkfs\b"), "disk format command"),
    (re.compile(r"\bdd\s+.*of=/dev/"), "disk erase via dd"),
    (re.compile(r"\bshred\s+/dev/"), "disk shred command"),
]

# Known-safe command prefixes that should never be blocked
_SAFE_COMMAND_PREFIXES = (
    "npm ",
    "npm\t",
    "npx ",
    "npx\t",
    "yarn ",
    "yarn\t",
    "pnpm ",
    "pnpm\t",
    "pip ",
    "pip\t",
    "pip3 ",
    "pip3\t",
    "python ",
    "python\t",
    "python3 ",
    "python3\t",
    "pytest ",
    "pytest\t",
    "pytest",  # bare pytest
    "tox ",
    "tox\t",
    "make ",
    "make\t",
    "cargo ",
    "cargo\t",
    "go ",
    "go\t",
    "git ",
    "git\t",
    "docker ",
    "docker\t",
    "echo ",
    "echo\t",
    "cat ",
    "cat\t",
    "ls ",
    "ls\t",
    "head ",
    "head\t",
    "tail ",
    "tail\t",
    "grep ",
    "grep\t",
    "rg ",
    "rg\t",
    "find ",
    "find\t",
    "which ",
    "which\t",
    "type ",
    "type\t",
    "wc ",
    "wc\t",
    "sort ",
    "sort\t",
    "curl ",
    "curl\t",
    "wget ",
    "wget\t",
    "mkdir ",
    "mkdir\t",
    "cp ",
    "cp\t",
    "mv ",
    "mv\t",
    "touch ",
    "touch\t",
    "chmod ",
    "chmod\t",
    "chown ",
    "chown\t",
    "stat ",
    "stat\t",
    "file ",
    "file\t",
    "tree ",
    "tree\t",
    "diff ",
    "diff\t",
    "patch ",
    "patch\t",
    "node ",
    "node\t",
    "tsc ",
    "tsc\t",
    "eslint ",
    "eslint\t",
    "prettier ",
    "prettier\t",
    "black ",
    "black\t",
    "ruff ",
    "ruff\t",
    "mypy ",
    "mypy\t",
    "flake8 ",
    "flake8\t",
    "isort ",
    "isort\t",
    "terraform ",
    "terraform\t",
    "helm ",
    "helm\t",
    "kubectl ",
    "kubectl\t",
    "az ",
    "az\t",
    "gcloud ",
    "gcloud\t",
    "aws ",
    "aws\t",
    "rustc ",
    "rustc\t",
    "javac ",
    "javac\t",
    "java ",
    "java\t",
    "dotnet ",
    "dotnet\t",
    "msbuild ",
    "msbuild\t",
    "gradle ",
    "gradle\t",
    "mvn ",
    "mvn\t",
)


def _tokenize_command(command: str) -> list[str]:
    """Shell-aware tokenization of a command string."""
    try:
        return shlex.split(command)
    except ValueError:
        # Fallback for malformed shell strings: split on whitespace
        return command.split()


def _is_safe_command(command: str) -> bool:
    """Check if a command starts with a known-safe prefix."""
    stripped = command.strip()
    if not stripped:
        return True
    for prefix in _SAFE_COMMAND_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return False


def _split_shell_segments(command: str) -> list[str]:
    return [
        segment.strip()
        for segment in re.split(r"\s*(?:&&|\|\||[;|])\s*", command)
        if segment.strip()
    ]


def _sudo_command_tokens(tokens: list[str]) -> list[str]:
    i = 1
    options_with_values = {"-u", "-g", "-h", "-p", "-C", "-T"}
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            return tokens[i + 1 :]
        if not token.startswith("-"):
            return tokens[i:]
        i += 1
        if token in options_with_values and i < len(tokens):
            i += 1
    return []


def _target_is_project_root(target: str, project: Path | None) -> bool:
    if project is None:
        return False
    try:
        project_path = project.expanduser().resolve()
        target_path = Path(target).expanduser()
        if not target_path.is_absolute():
            target_path = project_path / target_path
        return target_path.resolve(strict=False) == project_path
    except Exception:
        return False


def _check_single_destructive_command(
    command: str, *, project: Path | None = None
) -> str | None:
    if not command or not command.strip():
        return None

    for pattern, label in _DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return label

    if _is_safe_command(command):
        return None

    tokens = _tokenize_command(command)

    if not tokens:
        return None

    # Check for rm with recursive-force flags targeting repo root
    if tokens[0] == "rm" or tokens[0].endswith("/rm"):
        has_recursive = False
        has_force = False
        target_paths: list[str] = []

        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("-") and not tok.startswith("--"):
                if "r" in tok or "R" in tok:
                    has_recursive = True
                if "f" in tok:
                    has_force = True
                i += 1
                continue
            if tok == "--":
                i += 1
                continue
            # Remaining tokens are targets
            target_paths.extend(tokens[i:])
            break

        if has_recursive and has_force and target_paths:
            # Check if any target looks like a repo root or critical path
            for target in target_paths:
                if target in ("/", "~", "$HOME"):
                    return "recursive forced deletion of critical path"
                # Heuristic: single-component path like "." or bare directory
                if target == ".":
                    return "recursive forced deletion of current directory"
                if _target_is_project_root(target, project):
                    return "recursive forced deletion of project root"

    if tokens[0] == "sudo":
        remaining = " ".join(_sudo_command_tokens(tokens))
        sub_result = check_destructive_command(remaining, project=project)
        if sub_result:
            return sub_result + " with sudo"

    return None


def check_destructive_command(
    command: str, *, project: Path | None = None
) -> str | None:
    """Return a reason string if a high-confidence destructive command is detected."""
    if not command or not command.strip():
        return None

    for segment in _split_shell_segments(command):
        result = _check_single_destructive_command(segment, project=project)
        if result is not None:
            return result
    return None


def build_permission_request_response(event: HookEvent) -> HookExecutionResult:
    """Destructive-command guard for PermissionRequest events."""
    # Extract the command from various payload shapes
    command = ""
    tool_input = event.tool_input
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")

    if not command:
        return noop("no_command")

    reason = check_destructive_command(command, project=event.cwd)
    if reason is None:
        return noop("clean")

    return ok(
        HookResponse(
            permission_decision="deny",
            reason=reason,
            suppress_output=True,
        ),
    )
