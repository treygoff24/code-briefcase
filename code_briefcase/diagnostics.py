"""
Real-time diagnostics for Code Briefcase.

Wraps type checkers, linters, and formatter checks to provide structured error
output for LLM agents.

Supports:
- Python: pyright (type checker) + ruff (linter)
- TypeScript/JavaScript: tsc (type checker) + oxlint (linter) + oxfmt (formatter)
- Go: go vet (type checker) + golangci-lint (linter)
- Rust: cargo check (type checker) + clippy (linter)
- Java: javac (type checker) + checkstyle (linter)
- C/C++: clang/gcc (type checker) + cppcheck (linter)
- Ruby: rubocop (linter)
- PHP: phpstan (linter)
- Kotlin: kotlinc (type checker) + ktlint (linter)
- Swift: swiftc (type checker) + swiftlint (linter)
- C#: dotnet build (type checker)
- Scala: scalac (type checker)
- Elixir: mix compile (type checker) + credo (linter)
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, TypedDict, Any
from xml.etree import ElementTree

from code_briefcase.command_exec import expand_shebang_command
from code_briefcase.tsc_cache import CachedTsconfig, prepare_phase0_single_file_tsconfig

# Cap _resolve_tool's ancestor walk so deeply-nested paths don't trigger
# a stat-storm for tools that aren't installed locally anywhere.
_MAX_RESOLVE_DEPTH = 12


class ToolSpec(TypedDict):
    """A command-line tool used by diagnostics or doctor."""

    name: str
    executable: str
    install: str


class LanguageToolConfig(TypedDict, total=False):
    """Canonical language metadata for diagnostics and doctor."""

    extensions: list[str]
    type_checkers: list[ToolSpec]
    linters: list[ToolSpec]
    formatters: list[ToolSpec]
    install_command: list[str]


# Canonical language -> tools configuration used by diagnostics and doctor.
LANG_TOOLS: dict[str, LanguageToolConfig] = {
    "python": {
        "extensions": [".py"],
        "type_checkers": [
            {
                "name": "pyright",
                "executable": "pyright",
                "install": "pip install pyright  OR  npm install -g pyright",
            }
        ],
        "linters": [
            {"name": "ruff", "executable": "ruff", "install": "pip install ruff"}
        ],
        "install_command": ["pip", "install", "pyright", "ruff"],
    },
    "typescript": {
        "extensions": [".ts", ".tsx"],
        "type_checkers": [
            {
                "name": "tsc",
                "executable": "tsc",
                "install": "npm install -D typescript  OR  npm install -g typescript",
            }
        ],
        "linters": [
            {
                "name": "oxlint",
                "executable": "oxlint",
                "install": "npm install -D oxlint  OR  npm install -g oxlint",
            }
        ],
        "formatters": [
            {
                "name": "oxfmt",
                "executable": "oxfmt",
                "install": "npm install -D oxfmt  OR  npm install -g oxfmt",
            }
        ],
    },
    "javascript": {
        "extensions": [".js", ".jsx"],
        "type_checkers": [
            {
                "name": "tsc",
                "executable": "tsc",
                "install": "npm install -D typescript  OR  npm install -g typescript",
            }
        ],
        "linters": [
            {
                "name": "oxlint",
                "executable": "oxlint",
                "install": "npm install -D oxlint  OR  npm install -g oxlint",
            }
        ],
        "formatters": [
            {
                "name": "oxfmt",
                "executable": "oxfmt",
                "install": "npm install -D oxfmt  OR  npm install -g oxfmt",
            }
        ],
    },
    "go": {
        "extensions": [".go"],
        "type_checkers": [
            {"name": "go vet", "executable": "go", "install": "https://go.dev/dl/"}
        ],
        "linters": [
            {
                "name": "golangci-lint",
                "executable": "golangci-lint",
                "install": "brew install golangci-lint  OR  go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest",
            }
        ],
        "install_command": [
            "go",
            "install",
            "github.com/golangci/golangci-lint/cmd/golangci-lint@latest",
        ],
    },
    "rust": {
        "extensions": [".rs"],
        "type_checkers": [
            {
                "name": "cargo check",
                "executable": "cargo",
                "install": "https://rustup.rs/",
            }
        ],
        "linters": [
            {
                "name": "clippy",
                "executable": "cargo",
                "install": "rustup component add clippy",
            }
        ],
        "install_command": ["rustup", "component", "add", "clippy"],
    },
    "java": {
        "extensions": [".java"],
        "type_checkers": [
            {
                "name": "javac",
                "executable": "javac",
                "install": "Install JDK: https://adoptium.net/",
            }
        ],
        "linters": [
            {
                "name": "checkstyle",
                "executable": "checkstyle",
                "install": "brew install checkstyle  OR  download from checkstyle.org",
            }
        ],
    },
    "c": {
        "extensions": [".c", ".h"],
        "type_checkers": [
            {
                "name": "gcc",
                "executable": "gcc",
                "install": "xcode-select --install  OR  apt install gcc",
            }
        ],
        "linters": [
            {
                "name": "cppcheck",
                "executable": "cppcheck",
                "install": "brew install cppcheck  OR  apt install cppcheck",
            }
        ],
    },
    "cpp": {
        "extensions": [".cpp", ".cc", ".cxx", ".hpp"],
        "type_checkers": [
            {
                "name": "g++",
                "executable": "g++",
                "install": "xcode-select --install  OR  apt install g++",
            }
        ],
        "linters": [
            {
                "name": "cppcheck",
                "executable": "cppcheck",
                "install": "brew install cppcheck  OR  apt install cppcheck",
            }
        ],
    },
    "ruby": {
        "extensions": [".rb"],
        "linters": [
            {
                "name": "rubocop",
                "executable": "rubocop",
                "install": "gem install rubocop",
            }
        ],
        "install_command": ["gem", "install", "rubocop"],
    },
    "php": {
        "extensions": [".php"],
        "linters": [
            {
                "name": "phpstan",
                "executable": "phpstan",
                "install": "composer global require phpstan/phpstan",
            }
        ],
    },
    "kotlin": {
        "extensions": [".kt"],
        "type_checkers": [
            {
                "name": "kotlinc",
                "executable": "kotlinc",
                "install": "brew install kotlin  OR  sdk install kotlin",
            }
        ],
        "linters": [
            {"name": "ktlint", "executable": "ktlint", "install": "brew install ktlint"}
        ],
        "install_command": ["brew", "install", "kotlin", "ktlint"],
    },
    "swift": {
        "extensions": [".swift"],
        "type_checkers": [
            {
                "name": "swiftc",
                "executable": "swiftc",
                "install": "xcode-select --install",
            }
        ],
        "linters": [
            {
                "name": "swiftlint",
                "executable": "swiftlint",
                "install": "brew install swiftlint",
            }
        ],
        "install_command": ["brew", "install", "swiftlint"],
    },
    "csharp": {
        "extensions": [".cs"],
        "type_checkers": [
            {
                "name": "dotnet build",
                "executable": "dotnet",
                "install": "https://dotnet.microsoft.com/download",
            }
        ],
    },
    "scala": {
        "extensions": [".scala"],
        "type_checkers": [
            {
                "name": "scalac",
                "executable": "scalac",
                "install": "brew install scala  OR  sdk install scala",
            }
        ],
    },
    "elixir": {
        "extensions": [".ex", ".exs"],
        "type_checkers": [
            {
                "name": "mix compile",
                "executable": "mix",
                "install": "brew install elixir  OR  asdf install elixir",
            }
        ],
        "linters": [
            {"name": "credo", "executable": "mix", "install": "Included with Elixir"}
        ],
    },
    "lua": {
        "extensions": [".lua"],
        "linters": [
            {
                "name": "luacheck",
                "executable": "luacheck",
                "install": "luarocks install luacheck",
            }
        ],
        "install_command": ["luarocks", "install", "luacheck"],
    },
}

TOOL_SLOTS = ("type_checkers", "linters", "formatters")
PROJECT_OXLINT_IGNORE_PATTERNS = (
    "node_modules/**",
    "dist/**",
    "build/**",
    ".next/**",
    "coverage/**",
)
JS_TS_PROJECT_CONFIG_NAMES = ("tsconfig.json", "jsconfig.json")


def _detect_language(file_path: str) -> str:
    """Detect language from file extension."""
    ext = Path(file_path).suffix.lower()
    mapping = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
        ".hpp": "cpp",
        ".rb": "ruby",
        ".php": "php",
        ".kt": "kotlin",
        ".swift": "swift",
        ".cs": "csharp",
        ".scala": "scala",
        ".ex": "elixir",
        ".exs": "elixir",
        ".lua": "lua",
    }
    return mapping.get(ext, "unknown")


def _resolve_tool(name: str, start: Path) -> str | None:
    """
    Find a tool, preferring node_modules/.bin nearest to the source file.

    Walks every ancestor instead of stopping at package.json because workspace
    roots usually own the installed binary in pnpm/yarn/turbo monorepos.
    """
    search_dir = start if start.is_dir() else start.parent
    seen: set[Path] = set()
    for depth, parent in enumerate([search_dir, *search_dir.parents]):
        if depth >= _MAX_RESOLVE_DEPTH:
            break
        if parent in seen:
            continue
        seen.add(parent)
        candidate = parent / "node_modules" / ".bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


def _find_js_ts_project_config(start: Path) -> Path | None:
    """Find the nearest TypeScript/JavaScript project config for a source path."""
    search_dir = start if start.is_dir() else start.parent
    for depth, parent in enumerate([search_dir, *search_dir.parents]):
        if depth >= _MAX_RESOLVE_DEPTH:
            break
        for name in JS_TS_PROJECT_CONFIG_NAMES:
            candidate = parent / name
            if candidate.is_file():
                return candidate
    return None


def _write_single_file_tsconfig(
    project_config: Path,
    target_file: Path,
    *,
    allow_js: bool,
    tsc_path: str | None = None,
) -> CachedTsconfig | tempfile.TemporaryDirectory:
    """Create a stable single-file tsconfig that checks through project config.

    Passing a file directly to tsc ignores tsconfig settings in TypeScript 5 and
    errors in TypeScript 6. Running the whole project respects aliases but leaks
    unrelated diagnostics. A tiny config that extends the real config and
    overrides the root set with a single absolute file gives single-file hooks
    project-correct behavior without project-wide noise.

    When a tsc path is available, the config lives under the canonical Phase 0
    cache tree so TypeScript can reuse a stable tsBuildInfoFile across edits.
    If the cache lock is busy for too long, fall back to the old temp-dir path
    to preserve hook behavior.
    """
    if tsc_path is not None:
        cached = prepare_phase0_single_file_tsconfig(
            project_config,
            target_file,
            tsc_path=tsc_path,
            allow_js=allow_js,
        )
        if cached is not None:
            return cached

    temp_dir = tempfile.TemporaryDirectory(prefix="tldr-tsc-")
    config_path = Path(temp_dir.name) / "tsconfig.json"
    compiler_options: dict[str, object] = {"noEmit": True}
    if allow_js:
        compiler_options["allowJs"] = True

    config_path.write_text(
        json.dumps(
            {
                "extends": str(project_config.resolve()),
                "compilerOptions": compiler_options,
                "files": [str(target_file.resolve())],
                "include": [],
            },
            indent=2,
        )
        + "\n"
    )
    return temp_dir


def _write_javascript_project_tsconfig(
    project_path: Path,
    project_config: Path | None,
) -> tempfile.TemporaryDirectory:
    """Create an ephemeral project config that checks JS/JSX under project_path."""
    temp_dir = tempfile.TemporaryDirectory(prefix="tldr-js-tsc-")
    config_path = Path(temp_dir.name) / "tsconfig.json"
    project_root = project_path.resolve()
    payload: dict[str, object] = {
        "compilerOptions": {
            "allowJs": True,
            "noEmit": True,
        },
        "include": [
            f"{project_root}/**/*.js",
            f"{project_root}/**/*.jsx",
        ],
        "exclude": [
            f"{project_root}/node_modules/**",
            f"{project_root}/dist/**",
            f"{project_root}/build/**",
            f"{project_root}/.next/**",
        ],
    }
    if project_config is not None:
        payload["extends"] = str(project_config.resolve())

    config_path.write_text(json.dumps(payload, indent=2) + "\n")
    return temp_dir


def _parse_pyright_output(stdout: str) -> list[dict[str, Any]]:
    """Parse pyright JSON output into structured diagnostics."""
    try:
        data = json.loads(stdout)
        diagnostics = []
        for diag in data.get("generalDiagnostics", []):
            diagnostics.append(
                {
                    "file": diag.get("file", ""),
                    "line": diag.get("range", {}).get("start", {}).get("line", 0) + 1,
                    "column": diag.get("range", {}).get("start", {}).get("character", 0)
                    + 1,
                    "severity": diag.get("severity", "error"),
                    "message": diag.get("message", ""),
                    "rule": diag.get("rule", ""),
                    "source": "pyright",
                }
            )
        return diagnostics
    except json.JSONDecodeError:
        return []


def _parse_ruff_output(stdout: str) -> list[dict[str, Any]]:
    """Parse ruff JSON output into structured diagnostics."""
    try:
        data = json.loads(stdout)
        diagnostics = []
        for diag in data:
            diagnostics.append(
                {
                    "file": diag.get("filename", ""),
                    "line": diag.get("location", {}).get("row", 0),
                    "column": diag.get("location", {}).get("column", 0),
                    "severity": "warning",  # ruff is mostly lint warnings
                    "message": diag.get("message", ""),
                    "rule": diag.get("code", ""),
                    "source": "ruff",
                }
            )
        return diagnostics
    except json.JSONDecodeError:
        return []


def _parse_tsc_output(output: str) -> list[dict[str, Any]]:
    """Parse tsc output into structured diagnostics.

    With --pretty false, tsc writes diagnostics to stdout, not stderr; all
    callers correctly pass result.stdout. The parameter name documents what
    the function reads (the tsc-formatted output stream), not which fd.
    """
    diagnostics = []
    # tsc format: file(line,col): error TSxxxx: message
    pattern = r"(.+?)\((\d+),(\d+)\):\s*(error|warning)\s+(TS\d+):\s*(.+)"
    for line in output.strip().split("\n"):
        match = re.match(pattern, line)
        if match:
            diagnostics.append(
                {
                    "file": match.group(1),
                    "line": int(match.group(2)),
                    "column": int(match.group(3)),
                    "severity": match.group(4),
                    "message": match.group(6),
                    "rule": match.group(5),
                    "source": "tsc",
                }
            )
    return diagnostics


def _parse_oxlint_output(stdout: str) -> list[dict[str, Any]]:
    """Parse oxlint JSON output into structured diagnostics."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []

    diagnostics = []
    for diag in data.get("diagnostics", []):
        labels = diag.get("labels") or []
        span: Any = {}
        if labels and isinstance(labels[0], dict):
            span = labels[0].get("span") or {}

        diagnostics.append(
            {
                "file": diag.get("filename", ""),
                "line": span.get("line", 0),
                "column": span.get("column", 0),
                "severity": diag.get("severity", "warning"),
                "message": diag.get("message", ""),
                "rule": diag.get("code", ""),
                "source": "oxlint",
            }
        )

    return diagnostics


def _parse_go_vet_output(stderr: str) -> list[dict[str, Any]]:
    """Parse go vet output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stderr.strip():
        return diagnostics
    # go vet format: file.go:line:col: message
    pattern = r"(.+?):(\d+):(\d+):\s*(.+)"
    for line in stderr.strip().split("\n"):
        match = re.match(pattern, line)
        if match:
            diagnostics.append(
                {
                    "file": match.group(1),
                    "line": int(match.group(2)),
                    "column": int(match.group(3)),
                    "severity": "error",
                    "message": match.group(4),
                    "rule": "",
                    "source": "go vet",
                }
            )
    return diagnostics


def _parse_golangci_lint_output(stdout: str) -> list[dict[str, Any]]:
    """Parse golangci-lint JSON output into structured diagnostics."""
    try:
        data = json.loads(stdout)
        diagnostics = []
        for issue in data.get("Issues", []):
            pos = issue.get("Pos", {})
            diagnostics.append(
                {
                    "file": pos.get("Filename", ""),
                    "line": pos.get("Line", 0),
                    "column": pos.get("Column", 0),
                    "severity": "warning",
                    "message": issue.get("Text", ""),
                    "rule": issue.get("FromLinter", ""),
                    "source": "golangci-lint",
                }
            )
        return diagnostics
    except json.JSONDecodeError:
        return []


def _parse_cargo_check_output(stdout: str) -> list[dict[str, Any]]:
    """Parse cargo check JSON output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stdout.strip():
        return diagnostics
    # cargo outputs one JSON object per line
    for line in stdout.strip().split("\n"):
        try:
            data = json.loads(line)
            if data.get("reason") != "compiler-message":
                continue
            msg = data.get("message", {})
            spans = msg.get("spans", [])
            if not spans:
                continue
            span = spans[0]
            code = msg.get("code", {})
            diagnostics.append(
                {
                    "file": span.get("file_name", ""),
                    "line": span.get("line_start", 0),
                    "column": span.get("column_start", 0),
                    "severity": msg.get("level", "error"),
                    "message": msg.get("message", ""),
                    "rule": code.get("code", "") if code else "",
                    "source": "cargo",
                }
            )
        except json.JSONDecodeError:
            continue
    return diagnostics


def _parse_clippy_output(stdout: str) -> list[dict[str, Any]]:
    """Parse cargo clippy JSON output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stdout.strip():
        return diagnostics
    # clippy uses the same format as cargo check
    for line in stdout.strip().split("\n"):
        try:
            data = json.loads(line)
            if data.get("reason") != "compiler-message":
                continue
            msg = data.get("message", {})
            spans = msg.get("spans", [])
            if not spans:
                continue
            span = spans[0]
            code = msg.get("code", {})
            diagnostics.append(
                {
                    "file": span.get("file_name", ""),
                    "line": span.get("line_start", 0),
                    "column": span.get("column_start", 0),
                    "severity": msg.get("level", "warning"),
                    "message": msg.get("message", ""),
                    "rule": code.get("code", "") if code else "",
                    "source": "clippy",
                }
            )
        except json.JSONDecodeError:
            continue
    return diagnostics


def _parse_rubocop_output(stdout: str) -> list[dict[str, Any]]:
    """Parse rubocop JSON output into structured diagnostics."""
    try:
        data = json.loads(stdout)
        diagnostics = []
        for file_info in data.get("files", []):
            file_path = file_info.get("path", "")
            for offense in file_info.get("offenses", []):
                loc = offense.get("location", {})
                diagnostics.append(
                    {
                        "file": file_path,
                        "line": loc.get("line", 0),
                        "column": loc.get("column", 0),
                        "severity": offense.get("severity", "warning"),
                        "message": offense.get("message", ""),
                        "rule": offense.get("cop_name", ""),
                        "source": "rubocop",
                    }
                )
        return diagnostics
    except json.JSONDecodeError:
        return []


def _parse_phpstan_output(stdout: str) -> list[dict[str, Any]]:
    """Parse phpstan JSON output into structured diagnostics."""
    try:
        data = json.loads(stdout)
        diagnostics = []
        for file_path, file_info in data.get("files", {}).items():
            for msg in file_info.get("messages", []):
                diagnostics.append(
                    {
                        "file": file_path,
                        "line": msg.get("line", 0),
                        "column": 0,  # phpstan doesn't provide column
                        "severity": "error",
                        "message": msg.get("message", ""),
                        "rule": "",
                        "source": "phpstan",
                    }
                )
        return diagnostics
    except json.JSONDecodeError:
        return []


def _parse_ktlint_output(stdout: str) -> list[dict[str, Any]]:
    """Parse ktlint JSON output into structured diagnostics."""
    try:
        data = json.loads(stdout)
        diagnostics = []
        for file_info in data:
            file_path = file_info.get("file", "")
            for error in file_info.get("errors", []):
                diagnostics.append(
                    {
                        "file": file_path,
                        "line": error.get("line", 0),
                        "column": error.get("column", 0),
                        "severity": "warning",
                        "message": error.get("message", ""),
                        "rule": error.get("rule", ""),
                        "source": "ktlint",
                    }
                )
        return diagnostics
    except json.JSONDecodeError:
        return []


def _parse_swiftlint_output(stdout: str) -> list[dict[str, Any]]:
    """Parse swiftlint JSON output into structured diagnostics."""
    try:
        data = json.loads(stdout)
        diagnostics = []
        for item in data:
            diagnostics.append(
                {
                    "file": item.get("file", ""),
                    "line": item.get("line", 0),
                    "column": item.get("column", 0),
                    "severity": item.get("severity", "warning").lower(),
                    "message": item.get("reason", ""),
                    "rule": item.get("rule_id", ""),
                    "source": "swiftlint",
                }
            )
        return diagnostics
    except json.JSONDecodeError:
        return []


def _parse_cppcheck_output(stdout: str) -> list[dict[str, Any]]:
    """Parse cppcheck XML output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stdout.strip():
        return diagnostics
    try:
        root = ElementTree.fromstring(stdout)
        for error in root.findall(".//error"):
            location = error.find("location")
            if location is not None:
                diagnostics.append(
                    {
                        "file": location.get("file", ""),
                        "line": int(location.get("line", 0)),
                        "column": int(location.get("column", 0)),
                        "severity": error.get("severity", "error"),
                        "message": error.get("msg", ""),
                        "rule": error.get("id", ""),
                        "source": "cppcheck",
                    }
                )
        return diagnostics
    except ElementTree.ParseError:
        return []


def _parse_credo_output(stdout: str) -> list[dict[str, Any]]:
    """Parse credo JSON output into structured diagnostics."""
    try:
        data = json.loads(stdout)
        diagnostics = []
        for issue in data.get("issues", []):
            diagnostics.append(
                {
                    "file": issue.get("filename", ""),
                    "line": issue.get("line_no", 0),
                    "column": issue.get("column", 0),
                    "severity": "warning",
                    "message": issue.get("message", ""),
                    "rule": issue.get("check", ""),
                    "source": "credo",
                }
            )
        return diagnostics
    except json.JSONDecodeError:
        return []


def _parse_javac_output(stderr: str) -> list[dict[str, Any]]:
    """Parse javac output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stderr.strip():
        return diagnostics
    # javac format: file.java:line: error: message
    pattern = r"(.+?):(\d+):\s*(error|warning):\s*(.+)"
    for line in stderr.strip().split("\n"):
        match = re.match(pattern, line)
        if match:
            diagnostics.append(
                {
                    "file": match.group(1),
                    "line": int(match.group(2)),
                    "column": 0,
                    "severity": match.group(3),
                    "message": match.group(4),
                    "rule": "",
                    "source": "javac",
                }
            )
    return diagnostics


def _parse_checkstyle_output(stdout: str) -> list[dict[str, Any]]:
    """Parse checkstyle XML output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stdout.strip():
        return diagnostics
    try:
        root = ElementTree.fromstring(stdout)
        for file_elem in root.findall("file"):
            file_path = file_elem.get("name", "")
            for error in file_elem.findall("error"):
                diagnostics.append(
                    {
                        "file": file_path,
                        "line": int(error.get("line", 0)),
                        "column": int(error.get("column", 0)),
                        "severity": error.get("severity", "warning"),
                        "message": error.get("message", ""),
                        "rule": error.get("source", "").split(".")[-1],
                        "source": "checkstyle",
                    }
                )
        return diagnostics
    except ElementTree.ParseError:
        return []


def _parse_gcc_output(stderr: str) -> list[dict[str, Any]]:
    """Parse gcc/g++/clang output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stderr.strip():
        return diagnostics
    # gcc format: file.c:line:col: error: message
    pattern = r"(.+?):(\d+):(\d+):\s*(error|warning):\s*(.+)"
    for line in stderr.strip().split("\n"):
        match = re.match(pattern, line)
        if match:
            diagnostics.append(
                {
                    "file": match.group(1),
                    "line": int(match.group(2)),
                    "column": int(match.group(3)),
                    "severity": match.group(4),
                    "message": match.group(5),
                    "rule": "",
                    "source": "gcc",
                }
            )
    return diagnostics


def _parse_kotlinc_output(stderr: str) -> list[dict[str, Any]]:
    """Parse kotlinc output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stderr.strip():
        return diagnostics
    # kotlinc format: file.kt:line:col: error: message
    pattern = r"(.+?):(\d+):(\d+):\s*(error|warning):\s*(.+)"
    for line in stderr.strip().split("\n"):
        match = re.match(pattern, line)
        if match:
            diagnostics.append(
                {
                    "file": match.group(1),
                    "line": int(match.group(2)),
                    "column": int(match.group(3)),
                    "severity": match.group(4),
                    "message": match.group(5),
                    "rule": "",
                    "source": "kotlinc",
                }
            )
    return diagnostics


def _parse_swiftc_output(stderr: str) -> list[dict[str, Any]]:
    """Parse swiftc output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stderr.strip():
        return diagnostics
    # swiftc format: file.swift:line:col: error: message
    pattern = r"(.+?):(\d+):(\d+):\s*(error|warning):\s*(.+)"
    for line in stderr.strip().split("\n"):
        match = re.match(pattern, line)
        if match:
            diagnostics.append(
                {
                    "file": match.group(1),
                    "line": int(match.group(2)),
                    "column": int(match.group(3)),
                    "severity": match.group(4),
                    "message": match.group(5),
                    "rule": "",
                    "source": "swiftc",
                }
            )
    return diagnostics


def _parse_dotnet_build_output(stderr: str) -> list[dict[str, Any]]:
    """Parse dotnet build output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stderr.strip():
        return diagnostics
    # dotnet format: file.cs(line,col): error CS0000: message
    pattern = r"(.+?)\((\d+),(\d+)\):\s*(error|warning)\s+(\w+):\s*(.+)"
    for line in stderr.strip().split("\n"):
        match = re.match(pattern, line)
        if match:
            diagnostics.append(
                {
                    "file": match.group(1),
                    "line": int(match.group(2)),
                    "column": int(match.group(3)),
                    "severity": match.group(4),
                    "message": match.group(6),
                    "rule": match.group(5),
                    "source": "dotnet",
                }
            )
    return diagnostics


def _parse_scalac_output(stderr: str) -> list[dict[str, Any]]:
    """Parse scalac output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stderr.strip():
        return diagnostics
    # scalac format varies, common: file.scala:line: error: message
    pattern = r"(.+?):(\d+):\s*(error|warning):\s*(.+)"
    for line in stderr.strip().split("\n"):
        match = re.match(pattern, line)
        if match:
            diagnostics.append(
                {
                    "file": match.group(1),
                    "line": int(match.group(2)),
                    "column": 0,
                    "severity": match.group(3),
                    "message": match.group(4),
                    "rule": "",
                    "source": "scalac",
                }
            )
    return diagnostics


def _parse_mix_compile_output(stderr: str) -> list[dict[str, Any]]:
    """Parse mix compile output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    if not stderr.strip():
        return diagnostics
    # mix compile format: warning: message
    #   file.ex:line
    # Or: ** (CompileError) file.ex:line: message
    pattern = r"\*\*\s*\((\w+)\)\s*(.+?):(\d+):\s*(.+)"
    for line in stderr.strip().split("\n"):
        match = re.match(pattern, line)
        if match:
            diagnostics.append(
                {
                    "file": match.group(2),
                    "line": int(match.group(3)),
                    "column": 0,
                    "severity": "error" if "Error" in match.group(1) else "warning",
                    "message": match.group(4),
                    "rule": "",
                    "source": "mix",
                }
            )
    return diagnostics


def _oxfmt_drift_diagnostic(path: Path) -> dict:
    return {
        "file": str(path),
        "line": 1,
        "column": 1,
        "severity": "warning",
        "message": "Formatting drift — run oxfmt to fix",
        "rule": "",
        "source": "oxfmt",
    }


def _run_oxfmt(file_path: Path) -> list[dict[str, Any]]:
    """Run oxfmt --check and convert formatting drift into one diagnostic."""
    if file_path.name.endswith(".d.ts"):
        # oxfmt --check has false positives on .d.ts files in the beta
        # (oxc-project/oxc#19077). Re-enable once that closes.
        return []

    oxfmt = _resolve_tool("oxfmt", file_path)
    if not oxfmt:
        return []

    try:
        result = subprocess.run(
            expand_shebang_command([oxfmt, "--check", str(file_path)]),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return []

    if result.returncode == 0:
        return []
    return [_oxfmt_drift_diagnostic(file_path)]


def _run_project_oxfmt(project_path: Path) -> list[dict[str, Any]]:
    """Run oxfmt --check for a project without checking .d.ts files."""
    oxfmt = _resolve_tool("oxfmt", project_path)
    if not oxfmt:
        return []

    try:
        result = subprocess.run(
            expand_shebang_command([oxfmt, "--check", ".", "!**/*.d.ts"]),
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(project_path),
        )
    except subprocess.TimeoutExpired:
        return []

    output = f"{result.stdout}\n{result.stderr}"
    if (
        "Expected at least one target file" in output
        or "All matched files may have been excluded" in output
    ):
        return []

    if result.returncode == 0:
        return []
    return [_oxfmt_drift_diagnostic(project_path)]


def _run_python_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if shutil.which("pyright"):
        try:
            result = subprocess.run(
                ["pyright", "--outputjson", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostics.extend(_parse_pyright_output(result.stdout))
            tools_used.append("pyright")
        except subprocess.TimeoutExpired:
            pass

    if include_lint and shutil.which("ruff"):
        try:
            result = subprocess.run(
                ["ruff", "check", "--output-format=json", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            diagnostics.extend(_parse_ruff_output(result.stdout))
            tools_used.append("ruff")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_js_ts_diagnostics(
    path: Path,
    include_lint: bool,
    *,
    allow_js: bool,
) -> tuple[list[dict], list[str]]:
    tsc = _resolve_tool("tsc", path)
    tasks: list[tuple[str, list[str], int]] = []
    tsc_cwd = None
    tsc_filter_path = None
    tsc_temp_config: CachedTsconfig | tempfile.TemporaryDirectory | None = None
    if tsc:
        cmd = [tsc, "--noEmit"]
        project_config = _find_js_ts_project_config(path)
        if project_config:
            tsc_temp_config = _write_single_file_tsconfig(
                project_config,
                path,
                allow_js=allow_js,
                tsc_path=tsc,
            )
            cmd.extend(
                [
                    "--pretty",
                    "false",
                    "--project",
                    str(Path(tsc_temp_config.name) / "tsconfig.json"),
                ]
            )
            tsc_cwd = project_config.parent
            tsc_filter_path = path
        else:
            if allow_js:
                cmd.append("--allowJs")
            cmd.extend(["--pretty", "false", str(path)])
        tasks.append(("tsc", cmd, 30))
    tasks.extend(_js_ts_lint_format_tasks(path, include_lint=include_lint))

    try:
        return _collect_js_ts_results(
            path,
            tasks,
            cwd=tsc_cwd,
            tsc_filter_path=tsc_filter_path,
        )
    finally:
        if tsc_temp_config is not None:
            tsc_temp_config.cleanup()


def _js_ts_lint_format_tasks(
    path: Path,
    *,
    include_lint: bool,
) -> list[tuple[str, list[str], int]]:
    if not include_lint:
        return []
    tasks: list[tuple[str, list[str], int]] = []
    oxlint = _resolve_tool("oxlint", path)
    oxfmt = _resolve_tool("oxfmt", path) if not path.name.endswith(".d.ts") else None
    if oxlint:
        tasks.append(("oxlint", [oxlint, "--format=json", str(path)], 10))
    if oxfmt:
        tasks.append(("oxfmt", [oxfmt, "--check", str(path)], 15))
    return tasks


def _run_js_ts_lint_format_diagnostics(
    path: Path,
    include_lint: bool,
) -> tuple[list[dict], list[str]]:
    return _collect_js_ts_results(
        path,
        _js_ts_lint_format_tasks(path, include_lint=include_lint),
    )


def _collect_js_ts_results(
    drift_target: Path,
    tasks: list[tuple[str, list[str], int]],
    *,
    cwd: Path | None = None,
    tsc_filter_path: Path | None = None,
) -> tuple[list[dict], list[str]]:
    """Run JS/TS tool tasks in parallel; preserve submission order in results.

    drift_target: path attached to a synthesized oxfmt drift diagnostic
    (the file in single-file mode, the project root in project mode).
    """
    diagnostics: list[dict] = []
    tools_used: list[str] = []

    if not tasks:
        return diagnostics, tools_used

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [
            (name, executor.submit(_run_subprocess, cmd, timeout, cwd))
            for name, cmd, timeout in tasks
        ]
        for name, future in futures:
            result = future.result()
            if result is None:
                continue
            if name == "tsc":
                tsc_diagnostics = _parse_tsc_output(result.stdout)
                if tsc_filter_path is not None:
                    tsc_diagnostics = _filter_diagnostics_to_file(
                        tsc_diagnostics,
                        tsc_filter_path,
                        cwd,
                    )
                diagnostics.extend(tsc_diagnostics)
            elif name == "oxlint":
                oxlint_diagnostics = _parse_oxlint_output(result.stdout)
                if tsc_filter_path is not None:
                    oxlint_diagnostics = _filter_diagnostics_to_file(
                        oxlint_diagnostics,
                        tsc_filter_path,
                        cwd,
                    )
                diagnostics.extend(oxlint_diagnostics)
            elif name == "oxfmt":
                if _oxfmt_signals_drift(result):
                    diagnostics.append(_oxfmt_drift_diagnostic(drift_target))
                elif _oxfmt_signals_no_targets(result):
                    continue  # don't claim oxfmt ran when nothing matched
            tools_used.append(name)
    return diagnostics, tools_used


def _run_subprocess(
    cmd: list[str], timeout: int, cwd: Path | None = None
) -> subprocess.CompletedProcess | None:
    try:
        kwargs: dict = {"capture_output": True, "text": True, "timeout": timeout}
        if cwd is not None:
            kwargs["cwd"] = str(cwd)
        return subprocess.run(expand_shebang_command(cmd), **kwargs)
    except subprocess.TimeoutExpired:
        return None


def _filter_diagnostics_to_file(
    diagnostics: list[dict],
    target_path: Path,
    cwd: Path | None,
) -> list[dict[str, Any]]:
    """Keep only diagnostics that belong to target_path.

    Project-aware tsc is the correct way to respect path aliases, JSX settings,
    and framework tsconfigs, but it can report unrelated project errors. Single
    file diagnostics are hook-facing, so they should surface only findings for
    the edited file. Some tools also report project-relative paths; normalize
    matching diagnostics to the absolute target path for unambiguous output.
    """
    filtered = []
    target = target_path.resolve()
    base = cwd or target.parent

    for diagnostic in diagnostics:
        raw_file = diagnostic.get("file")
        if not raw_file:
            continue

        candidate = Path(raw_file)
        if not candidate.is_absolute():
            candidate = base / candidate
        if candidate.resolve() != target:
            continue

        normalized = dict(diagnostic)
        normalized["file"] = str(target)
        filtered.append(normalized)

    return filtered


def _oxfmt_signals_drift(result: subprocess.CompletedProcess) -> bool:
    if result.returncode == 0:
        return False
    return not _oxfmt_signals_no_targets(result)


def _oxfmt_signals_no_targets(result: subprocess.CompletedProcess) -> bool:
    output = f"{result.stdout}\n{result.stderr}"
    return (
        "Expected at least one target file" in output
        or "All matched files may have been excluded" in output
    )


def _run_typescript_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    return _run_js_ts_diagnostics(path, include_lint, allow_js=False)


def _run_javascript_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    return _run_js_ts_diagnostics(path, include_lint, allow_js=True)


def _run_js_ts_project_diagnostics(
    path: Path,
    include_lint: bool,
    *,
    allow_js: bool,
) -> tuple[list[dict], list[str]]:
    tsc = _resolve_tool("tsc", path)
    oxlint = _resolve_tool("oxlint", path) if include_lint else None
    oxfmt = _resolve_tool("oxfmt", path) if include_lint else None

    tasks: list[tuple[str, list[str], int]] = []
    tsc_temp_config: tempfile.TemporaryDirectory | None = None
    if tsc:
        cmd = [tsc, "--noEmit"]
        if allow_js:
            tsc_temp_config = _write_javascript_project_tsconfig(
                path,
                _find_js_ts_project_config(path),
            )
            cmd.extend(
                [
                    "--pretty",
                    "false",
                    "--project",
                    str(Path(tsc_temp_config.name) / "tsconfig.json"),
                ]
            )
        else:
            cmd.extend(["--pretty", "false"])
        tasks.append(("tsc", cmd, 120))
    if oxlint:
        oxlint_cmd = [oxlint, "--format=json", "--no-error-on-unmatched-pattern"]
        oxlint_cmd.extend(
            f"--ignore-pattern={pattern}" for pattern in PROJECT_OXLINT_IGNORE_PATTERNS
        )
        oxlint_cmd.append(".")
        tasks.append(("oxlint", oxlint_cmd, 60))
    if oxfmt:
        tasks.append(("oxfmt", [oxfmt, "--check", ".", "!**/*.d.ts"], 60))

    try:
        return _collect_js_ts_results(path, tasks, cwd=path)
    finally:
        if tsc_temp_config is not None:
            tsc_temp_config.cleanup()


def _run_go_diagnostics(path: Path, include_lint: bool) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if shutil.which("go"):
        try:
            result = subprocess.run(
                ["go", "vet", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostics.extend(_parse_go_vet_output(result.stderr))
            tools_used.append("go vet")
        except subprocess.TimeoutExpired:
            pass

    if include_lint and shutil.which("golangci-lint"):
        try:
            result = subprocess.run(
                ["golangci-lint", "run", "--out-format=json", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            diagnostics.extend(_parse_golangci_lint_output(result.stdout))
            tools_used.append("golangci-lint")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_rust_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if shutil.which("cargo"):
        try:
            result = subprocess.run(
                ["cargo", "check", "--message-format=json"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(path.parent),
            )
            diagnostics.extend(_parse_cargo_check_output(result.stdout))
            tools_used.append("cargo check")
        except subprocess.TimeoutExpired:
            pass

    if include_lint and shutil.which("cargo"):
        try:
            result = subprocess.run(
                ["cargo", "clippy", "--message-format=json"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(path.parent),
            )
            diagnostics.extend(_parse_clippy_output(result.stdout))
            tools_used.append("clippy")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_java_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if shutil.which("javac"):
        try:
            result = subprocess.run(
                ["javac", "-Xlint:all", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostics.extend(_parse_javac_output(result.stderr))
            tools_used.append("javac")
        except subprocess.TimeoutExpired:
            pass

    if include_lint and shutil.which("checkstyle"):
        try:
            result = subprocess.run(
                ["checkstyle", "-f", "xml", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostics.extend(_parse_checkstyle_output(result.stdout))
            tools_used.append("checkstyle")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_c_family_diagnostics(
    path: Path, include_lint: bool, *, compiler: str
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if shutil.which(compiler):
        try:
            result = subprocess.run(
                [compiler, "-fsyntax-only", "-Wall", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostics.extend(_parse_gcc_output(result.stderr))
            tools_used.append(compiler)
        except subprocess.TimeoutExpired:
            pass

    if include_lint and shutil.which("cppcheck"):
        try:
            result = subprocess.run(
                ["cppcheck", "--xml", "--enable=all", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostics.extend(_parse_cppcheck_output(result.stderr))
            tools_used.append("cppcheck")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_c_diagnostics(path: Path, include_lint: bool) -> tuple[list[dict], list[str]]:
    return _run_c_family_diagnostics(path, include_lint, compiler="gcc")


def _run_cpp_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    return _run_c_family_diagnostics(path, include_lint, compiler="g++")


def _run_ruby_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if include_lint and shutil.which("rubocop"):
        try:
            result = subprocess.run(
                ["rubocop", "--format", "json", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostics.extend(_parse_rubocop_output(result.stdout))
            tools_used.append("rubocop")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_php_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if include_lint and shutil.which("phpstan"):
        try:
            result = subprocess.run(
                ["phpstan", "analyse", "--error-format=json", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            diagnostics.extend(_parse_phpstan_output(result.stdout))
            tools_used.append("phpstan")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_kotlin_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if shutil.which("kotlinc"):
        try:
            result = subprocess.run(
                ["kotlinc", "-d", "/dev/null", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            diagnostics.extend(_parse_kotlinc_output(result.stderr))
            tools_used.append("kotlinc")
        except subprocess.TimeoutExpired:
            pass

    if include_lint and shutil.which("ktlint"):
        try:
            result = subprocess.run(
                ["ktlint", "--reporter=json", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostics.extend(_parse_ktlint_output(result.stdout))
            tools_used.append("ktlint")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_swift_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if shutil.which("swiftc"):
        try:
            result = subprocess.run(
                ["swiftc", "-typecheck", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostics.extend(_parse_swiftc_output(result.stderr))
            tools_used.append("swiftc")
        except subprocess.TimeoutExpired:
            pass

    if include_lint and shutil.which("swiftlint"):
        try:
            result = subprocess.run(
                ["swiftlint", "lint", "--reporter", "json", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostics.extend(_parse_swiftlint_output(result.stdout))
            tools_used.append("swiftlint")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_csharp_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if shutil.which("dotnet"):
        try:
            result = subprocess.run(
                ["dotnet", "build", "--no-restore", str(path.parent)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            diagnostics.extend(_parse_dotnet_build_output(result.stderr))
            tools_used.append("dotnet build")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_scala_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if shutil.which("scalac"):
        try:
            result = subprocess.run(
                ["scalac", "-d", "/tmp", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            diagnostics.extend(_parse_scalac_output(result.stderr))
            tools_used.append("scalac")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _run_elixir_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if shutil.which("mix"):
        try:
            result = subprocess.run(
                ["mix", "compile", "--warnings-as-errors"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(path.parent),
            )
            diagnostics.extend(_parse_mix_compile_output(result.stderr))
            tools_used.append("mix compile")
        except subprocess.TimeoutExpired:
            pass

    if include_lint and shutil.which("mix"):
        try:
            result = subprocess.run(
                ["mix", "credo", "--format", "json", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(path.parent),
            )
            diagnostics.extend(_parse_credo_output(result.stdout))
            tools_used.append("credo")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


def _parse_luacheck_output(output: str) -> list[dict[str, Any]]:
    """Parse luacheck --formatter plain output: `file:line:col: (CODE) message`."""
    diagnostics = []
    pattern = re.compile(r"^\s*(.+?):(\d+):(\d+):\s*(?:\((\w+)\)\s*)?(.+)$")
    for line in output.strip().split("\n"):
        match = pattern.match(line)
        if not match:
            continue
        code = match.group(4) or ""
        diagnostics.append(
            {
                "file": match.group(1),
                "line": int(match.group(2)),
                "column": int(match.group(3)),
                "severity": "error" if code.startswith("E") else "warning",
                "message": match.group(5),
                "rule": code,
                "source": "luacheck",
            }
        )
    return diagnostics


def _run_lua_diagnostics(
    path: Path, include_lint: bool
) -> tuple[list[dict], list[str]]:
    diagnostics = []
    tools_used = []

    if include_lint and shutil.which("luacheck"):
        try:
            result = subprocess.run(
                [
                    "luacheck",
                    "--codes",
                    "--no-color",
                    "--formatter",
                    "plain",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            diagnostics.extend(_parse_luacheck_output(result.stdout))
            tools_used.append("luacheck")
        except subprocess.TimeoutExpired:
            pass

    return diagnostics, tools_used


DiagnosticRunner = Callable[[Path, bool], tuple[list[dict], list[str]]]


DIAGNOSTIC_RUNNERS: dict[str, DiagnosticRunner] = {
    "python": _run_python_diagnostics,
    "typescript": _run_typescript_diagnostics,
    "javascript": _run_javascript_diagnostics,
    "go": _run_go_diagnostics,
    "rust": _run_rust_diagnostics,
    "java": _run_java_diagnostics,
    "c": _run_c_diagnostics,
    "cpp": _run_cpp_diagnostics,
    "ruby": _run_ruby_diagnostics,
    "php": _run_php_diagnostics,
    "kotlin": _run_kotlin_diagnostics,
    "swift": _run_swift_diagnostics,
    "csharp": _run_csharp_diagnostics,
    "scala": _run_scala_diagnostics,
    "elixir": _run_elixir_diagnostics,
    "lua": _run_lua_diagnostics,
}


def get_diagnostics(
    file_path: str,
    language: str | None = None,
    include_lint: bool = True,
) -> dict:
    """
    Get type, lint, and formatter diagnostics for a file.

    Args:
        file_path: Path to the source file
        language: Override language detection (python, typescript, go, rust, etc.)
        include_lint: Include linter/formatter diagnostics (default: True)

    Returns:
        Dict with 'diagnostics' list and metadata
    """
    path = Path(file_path).resolve()
    if not path.exists():
        return {"error": f"File not found: {file_path}", "diagnostics": []}

    lang = language or _detect_language(str(path))
    runner = DIAGNOSTIC_RUNNERS.get(lang)
    if runner:
        all_diagnostics, tools_used = runner(path, include_lint)
    else:
        all_diagnostics, tools_used = [], []

    all_diagnostics.sort(key=lambda d: (d.get("file", ""), d.get("line", 0)))

    return {
        "file": str(path),
        "language": lang,
        "tools": tools_used,
        "diagnostics": all_diagnostics,
        "error_count": sum(1 for d in all_diagnostics if d.get("severity") == "error"),
        "warning_count": sum(
            1 for d in all_diagnostics if d.get("severity") == "warning"
        ),
    }


def get_lint_format_diagnostics(
    file_path: str,
    language: str | None = None,
    include_lint: bool = True,
) -> dict:
    """Get the lint/format-only leg for a file.

    Used by watch diagnostics so a warm TypeScript watcher can replace the
    expensive tsc leg without dropping oxlint/oxfmt feedback.
    """
    path = Path(file_path).resolve()
    if not path.exists():
        return {"error": f"File not found: {file_path}", "diagnostics": []}

    lang = language or _detect_language(str(path))
    if lang in {"typescript", "javascript"}:
        all_diagnostics, tools_used = _run_js_ts_lint_format_diagnostics(
            path,
            include_lint,
        )
    else:
        all_diagnostics, tools_used = [], []

    all_diagnostics.sort(key=lambda d: (d.get("file", ""), d.get("line", 0)))
    return {
        "file": str(path),
        "language": lang,
        "tools": tools_used,
        "diagnostics": all_diagnostics,
        "error_count": sum(1 for d in all_diagnostics if d.get("severity") == "error"),
        "warning_count": sum(
            1 for d in all_diagnostics if d.get("severity") == "warning"
        ),
    }


def get_project_diagnostics(
    project_path: str,
    language: str = "python",
    include_lint: bool = True,
) -> dict:
    """
    Get diagnostics for entire project.

    Uses pyright/tsc on the whole project for faster checking.
    """
    path = Path(project_path).resolve()
    if not path.exists():
        return {"error": f"Path not found: {project_path}", "diagnostics": []}

    all_diagnostics = []
    tools_used = []

    if language == "python":
        # Run pyright on project
        if shutil.which("pyright"):
            try:
                result = subprocess.run(
                    ["pyright", "--outputjson", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(path),
                )
                all_diagnostics.extend(_parse_pyright_output(result.stdout))
                tools_used.append("pyright")
            except subprocess.TimeoutExpired:
                pass

        # Run ruff on project
        if include_lint and shutil.which("ruff"):
            try:
                result = subprocess.run(
                    ["ruff", "check", "--output-format=json", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=str(path),
                )
                all_diagnostics.extend(_parse_ruff_output(result.stdout))
                tools_used.append("ruff")
            except subprocess.TimeoutExpired:
                pass

    elif language in ("typescript", "javascript"):
        all_diagnostics, tools_used = _run_js_ts_project_diagnostics(
            path,
            include_lint,
            allow_js=language == "javascript",
        )

    elif language == "go":
        # Run go vet on project
        if shutil.which("go"):
            try:
                result = subprocess.run(
                    ["go", "vet", "./..."],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(path),
                )
                all_diagnostics.extend(_parse_go_vet_output(result.stderr))
                tools_used.append("go vet")
            except subprocess.TimeoutExpired:
                pass

        # Run golangci-lint on project
        if include_lint and shutil.which("golangci-lint"):
            try:
                result = subprocess.run(
                    ["golangci-lint", "run", "--out-format=json", "./..."],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(path),
                )
                all_diagnostics.extend(_parse_golangci_lint_output(result.stdout))
                tools_used.append("golangci-lint")
            except subprocess.TimeoutExpired:
                pass

    elif language == "rust":
        # Run cargo check on project
        if shutil.which("cargo"):
            try:
                result = subprocess.run(
                    ["cargo", "check", "--message-format=json"],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    cwd=str(path),
                )
                all_diagnostics.extend(_parse_cargo_check_output(result.stdout))
                tools_used.append("cargo check")
            except subprocess.TimeoutExpired:
                pass

        # Run clippy on project
        if include_lint and shutil.which("cargo"):
            try:
                result = subprocess.run(
                    ["cargo", "clippy", "--message-format=json"],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    cwd=str(path),
                )
                all_diagnostics.extend(_parse_clippy_output(result.stdout))
                tools_used.append("clippy")
            except subprocess.TimeoutExpired:
                pass

    elif language == "ruby":
        # Run rubocop on project
        if include_lint and shutil.which("rubocop"):
            try:
                result = subprocess.run(
                    ["rubocop", "--format", "json", "."],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(path),
                )
                all_diagnostics.extend(_parse_rubocop_output(result.stdout))
                tools_used.append("rubocop")
            except subprocess.TimeoutExpired:
                pass

    elif language == "elixir":
        # Run mix compile on project
        if shutil.which("mix"):
            try:
                result = subprocess.run(
                    ["mix", "compile", "--warnings-as-errors"],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    cwd=str(path),
                )
                all_diagnostics.extend(_parse_mix_compile_output(result.stderr))
                tools_used.append("mix compile")
            except subprocess.TimeoutExpired:
                pass

        # Run credo on project
        if include_lint and shutil.which("mix"):
            try:
                result = subprocess.run(
                    ["mix", "credo", "--format", "json"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(path),
                )
                all_diagnostics.extend(_parse_credo_output(result.stdout))
                tools_used.append("credo")
            except subprocess.TimeoutExpired:
                pass

    # Sort by file, then line
    all_diagnostics.sort(key=lambda d: (d.get("file", ""), d.get("line", 0)))

    return {
        "project": str(path),
        "language": language,
        "tools": tools_used,
        "diagnostics": all_diagnostics,
        "error_count": sum(1 for d in all_diagnostics if d.get("severity") == "error"),
        "warning_count": sum(
            1 for d in all_diagnostics if d.get("severity") == "warning"
        ),
        "file_count": len(set(d.get("file", "") for d in all_diagnostics)),
    }


def format_diagnostics_for_llm(result: dict) -> str:
    """
    Format diagnostics as concise text for LLM context.
    """
    if result.get("error"):
        return f"Error: {result['error']}"

    diagnostics = result.get("diagnostics", [])
    if not diagnostics:
        return "No diagnostics found."

    lines = []
    errors = result.get("error_count", 0)
    warnings = result.get("warning_count", 0)
    lines.append(f"Found {errors} errors, {warnings} warnings")
    lines.append("")

    for d in diagnostics:
        severity = "E" if d.get("severity") == "error" else "W"
        rule = f" [{d['rule']}]" if d.get("rule") else ""
        lines.append(
            f"{severity} {d['file']}:{d['line']}:{d['column']}: {d['message']}{rule}"
        )

    return "\n".join(lines)
