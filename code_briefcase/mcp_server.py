"""
Code Briefcase MCP Server - Model Context Protocol interface for Code Briefcase.

Provides 1:1 mapping with Code Briefcase daemon commands, enabling AI tools
(OpenCode, Claude Desktop, Claude Code) to use Code Briefcase code analysis.

Usage:
    code-briefcase-mcp --project /path/to/project
"""

import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import time
import os

from pathlib import Path

# Conditional imports for file locking
if os.name == "nt":
    import msvcrt
else:
    import fcntl

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("code-briefcase")


def _resolve_project(project: str | None = None) -> str:
    explicit = project not in (None, "", "auto")
    if explicit:
        path = Path(str(project)).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError(f"Code Briefcase project does not exist: {path}")
        return str(path)

    candidates = [
        os.environ.get("CODE_BRIEFCASE_PROJECT"),
        os.environ.get("CLAUDE_PROJECT_DIR"),
        os.environ.get("CODEX_PROJECT_DIR"),
        os.environ.get("CODEX_CWD"),
        os.environ.get("PWD"),
        ".",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if path.exists() and path.is_dir():
            return str(path)
    raise FileNotFoundError("Could not resolve Code Briefcase project root")


def _resolve_tool_file(file: str, project: str | None = None) -> tuple[str, str]:
    root = Path(_resolve_project(project))
    path = Path(file).expanduser()
    if not path.is_absolute():
        path = root / path
    return str(root), str(path.resolve())


def _get_socket_path(project: str) -> Path:
    """Compute socket path matching daemon.py logic."""
    hash_val = hashlib.md5(str(Path(project).resolve()).encode()).hexdigest()[:8]
    tmp_dir = tempfile.gettempdir()
    return Path(tmp_dir) / f"code-briefcase-{hash_val}.sock"


def _get_lock_path(project: str) -> Path:
    """Get lock file path for daemon startup synchronization."""
    hash_val = hashlib.md5(str(Path(project).resolve()).encode()).hexdigest()[:8]
    tmp_dir = tempfile.gettempdir()
    return Path(tmp_dir) / f"code-briefcase-{hash_val}.lock"


def _get_connection_info(project: str) -> tuple[str, int | None]:
    """Return (address, port) - port is None for Unix sockets.

    On Windows, uses TCP on localhost with a deterministic port.
    On Unix, uses Unix domain sockets.
    """
    if sys.platform == "win32":
        hash_val = hashlib.md5(str(Path(project).resolve()).encode()).hexdigest()[:8]
        port = 49152 + (int(hash_val, 16) % 10000)
        return ("127.0.0.1", port)
    else:
        socket_path = _get_socket_path(project)
        return (str(socket_path), None)


def _ping_daemon(project: str) -> bool:
    """Check if daemon is alive and responding."""
    addr, port = _get_connection_info(project)
    
    # On Unix, check if socket file exists first
    if port is None and not Path(addr).exists():
        return False
    
    try:
        result = _send_raw(project, {"cmd": "ping"})
        return result.get("status") == "ok"
    except Exception:
        return False


def _ensure_daemon(project: str, timeout: float = 10.0) -> None:
    """Ensure daemon is running, starting it if needed.

    Uses file locking to prevent race conditions when multiple agents
    try to start the daemon simultaneously.
    """
    # Fast path: daemon already running (no lock needed)
    if _ping_daemon(project):
        return

    socket_path = _get_socket_path(project)
    lock_path = _get_lock_path(project)

    # Acquire exclusive lock for startup coordination
    lock_path.touch(exist_ok=True)
    with open(lock_path, "w") as lock_file:
        try:
            if os.name == "nt":
                # Windows locking logic with timeout
                # LK_NBLCK is non-blocking; we loop with a 10s timeout
                lock_start = time.time()
                lock_timeout = 10.0  # Same as startup.py
                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as e:
                        if time.time() - lock_start > lock_timeout:
                            raise RuntimeError(
                                f"Timeout acquiring lock on {lock_path} after {lock_timeout}s"
                            ) from e
                        time.sleep(0.1)
            else:
                # Unix locking
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

            # Re-check after acquiring lock (another process may have started daemon)
            if _ping_daemon(project):
                return

            # Clean up stale socket if daemon is dead
            if socket_path.exists():
                is_win = os.name == "nt"
                if not is_win:
                    # Unix: check if it's a socket
                    import stat
                    try:
                        if stat.S_ISSOCK(socket_path.stat().st_mode):
                            socket_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                # Windows: do nothing (TCP no socket file), or if it was a file,
                # we don't accidentally delete random files unless we are sure.

            # Start daemon
            subprocess.Popen(
                [sys.executable, "-m", "code_briefcase.cli", "daemon", "start", "--project", project],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            # Wait for daemon to be ready
            start = time.time()
            while time.time() - start < timeout:
                if _ping_daemon(project):
                    return
                time.sleep(0.1)

            raise RuntimeError(f"Failed to start Code Briefcase daemon for {project}")
        finally:
            if os.name == "nt":
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _send_raw(project: str, command: dict) -> dict:
    """Send command to daemon socket."""
    addr, port = _get_connection_info(project)
    
    sock = None
    try:
        if port is not None:
            # TCP socket for Windows
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((addr, port))
        else:
            # Unix socket for Linux/macOS
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(addr)
    
        sock.sendall(json.dumps(command).encode() + b"\n")

        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            try:
                return _decode_socket_response(chunks)
            except json.JSONDecodeError:
                continue

        return _decode_socket_response(chunks)
    finally:
        if sock:
            sock.close()


def _decode_socket_response(chunks: list[bytes]) -> dict:
    return json.loads(b"".join(chunks))


def _send_command(project: str, command: dict) -> dict:
    """Send command to daemon, auto-starting if needed."""
    _ensure_daemon(project)
    return _send_raw(project, command)


# === NAVIGATION TOOLS ===


@mcp.tool()
def tree(project: str = "auto", extensions: list[str] | None = None) -> dict:
    """Get file tree structure for a project.

    Args:
        project: Project root directory
        extensions: Optional list of extensions to filter (e.g., [".py", ".ts"])
    """
    return _send_command(
        _resolve_project(project),
        {
            "cmd": "tree",
            "extensions": tuple(extensions) if extensions else None,
            "exclude_hidden": True,
        },
    )


@mcp.tool()
def structure(
    project: str = "auto", language: str = "python", max_results: int = 100
) -> dict:
    """Get code structure (codemaps) - functions, classes, imports per file.

    Args:
        project: Project root directory
        language: Programming language (python, typescript, go, rust, etc.)
        max_results: Maximum files to analyze
    """
    return _send_command(
        _resolve_project(project),
        {"cmd": "structure", "language": language, "max_results": max_results},
    )


@mcp.tool()
def search(pattern: str, project: str = "auto", max_results: int = 100) -> dict:
    """Search files for a regex pattern.

    Args:
        project: Project root directory
        pattern: Regex pattern to search for
        max_results: Maximum matches to return
    """
    return _send_command(
        _resolve_project(project),
        {"cmd": "search", "pattern": pattern, "max_results": max_results}
    )


@mcp.tool()
def extract(file: str, project: str = "auto") -> dict:
    """Extract full code structure from a file.

    Returns imports, functions, classes, and intra-file call graph.

    Args:
        file: Path to source file
    """
    resolved_project, resolved_file = _resolve_tool_file(file, project)
    return _send_command(resolved_project, {"cmd": "extract", "file": resolved_file})


# === CONTEXT TOOLS (Key differentiator - 95% token savings) ===


@mcp.tool()
def context(
    entry: str, project: str = "auto", depth: int = 2, language: str = "python"
) -> str:
    """Get token-efficient LLM context starting from an entry point.

    Follows call graph to specified depth, returning signatures and complexity
    metrics. This is Code Briefcase's key value - 95% token savings vs reading raw files.

    Args:
        project: Project root directory
        entry: Entry point (function_name or Class.method)
        depth: How deep to follow calls (default 2)
        language: Programming language

    Returns:
        LLM-ready formatted context string
    """
    result = _send_command(
        _resolve_project(project),
        {"cmd": "context", "entry": entry, "depth": depth, "language": language},
    )
    # Return formatted string for LLM consumption
    if result.get("status") == "ok":
        ctx = result.get("result", {})
        if hasattr(ctx, "to_llm_string"):
            return ctx.to_llm_string()
        return str(ctx)
    return str(result)


# === FLOW ANALYSIS TOOLS ===


@mcp.tool()
def cfg(file: str, function: str, language: str = "python", project: str = "auto") -> dict:
    """Get control flow graph for a function.

    Returns basic blocks, control flow edges, and cyclomatic complexity.

    Args:
        file: Path to source file
        function: Function name to analyze
        language: Programming language
    """
    resolved_project, resolved_file = _resolve_tool_file(file, project)
    return _send_command(
        resolved_project,
        {"cmd": "cfg", "file": resolved_file, "function": function, "language": language},
    )


@mcp.tool()
def dfg(file: str, function: str, language: str = "python", project: str = "auto") -> dict:
    """Get data flow graph for a function.

    Returns variable references and def-use chains.

    Args:
        file: Path to source file
        function: Function name to analyze
        language: Programming language
    """
    resolved_project, resolved_file = _resolve_tool_file(file, project)
    return _send_command(
        resolved_project,
        {"cmd": "dfg", "file": resolved_file, "function": function, "language": language},
    )


@mcp.tool()
def slice(
    file: str,
    function: str,
    line: int,
    direction: str = "backward",
    variable: str | None = None,
    language: str = "python",
    project: str = "auto",
) -> dict:
    """Get program slice - lines affecting or affected by a given line.

    Args:
        file: Path to source file
        function: Function name
        line: Line number to slice from
        direction: "backward" (what affects this line) or "forward" (what this line affects)
        variable: Optional specific variable to trace
        language: Programming language

    Returns:
        Dict with lines in the slice and count
    """
    resolved_project, resolved_file = _resolve_tool_file(file, project)
    return _send_command(
        resolved_project,
        {
            "cmd": "slice",
            "file": resolved_file,
            "function": function,
            "line": line,
            "direction": direction,
            "variable": variable or "",
            "language": language,
        },
    )


# === CODEBASE ANALYSIS TOOLS ===


@mcp.tool()
def impact(function: str, project: str = "auto") -> dict:
    """Find all callers of a function (reverse call graph).

    Useful before refactoring to understand what would break.

    Args:
        project: Project root directory
        function: Function name to find callers of
    """
    return _send_command(_resolve_project(project), {"cmd": "impact", "func": function})


@mcp.tool()
def dead(
    project: str = "auto",
    entry_points: list[str] | None = None,
    language: str = "python",
) -> dict:
    """Find unreachable (dead) code not called from entry points.

    Args:
        project: Project root directory
        entry_points: List of entry point patterns (default: main, test_, cli)
        language: Programming language
    """
    return _send_command(
        _resolve_project(project),
        {"cmd": "dead", "entry_points": entry_points, "language": language},
    )


@mcp.tool()
def arch(project: str = "auto", language: str = "python") -> dict:
    """Detect architectural layers from call patterns.

    Identifies entry layer (controllers), middle layer (services),
    and leaf layer (utilities). Also detects circular dependencies.

    Args:
        project: Project root directory
        language: Programming language
    """
    return _send_command(_resolve_project(project), {"cmd": "arch", "language": language})


@mcp.tool()
def calls(project: str = "auto", language: str = "python") -> dict:
    """Build cross-file call graph for the project.

    Args:
        project: Project root directory
        language: Programming language
    """
    return _send_command(_resolve_project(project), {"cmd": "calls", "language": language})


# === IMPORT ANALYSIS ===


@mcp.tool()
def imports(file: str, language: str = "python", project: str = "auto") -> dict:
    """Parse imports from a source file.

    Args:
        file: Path to source file
        language: Programming language
    """
    resolved_project, resolved_file = _resolve_tool_file(file, project)
    return _send_command(
        resolved_project, {"cmd": "imports", "file": resolved_file, "language": language}
    )


@mcp.tool()
def importers(module: str, project: str = "auto", language: str = "python") -> dict:
    """Find all files that import a given module.

    Args:
        project: Project root directory
        module: Module name to search for
        language: Programming language
    """
    return _send_command(
        _resolve_project(project), {"cmd": "importers", "module": module, "language": language}
    )


# === SEMANTIC SEARCH ===


@mcp.tool()
def semantic(query: str, project: str = "auto", k: int = 10) -> dict:
    """Semantic code search using embeddings.

    Searches over function/class summaries using vector similarity.
    Auto-downloads embedding model and builds index on first use.

    Args:
        project: Project root directory
        query: Natural language query
        k: Number of results to return
    """
    return _send_command(
        _resolve_project(project),
        {"cmd": "semantic", "action": "search", "query": query, "k": k},
    )


# === QUALITY TOOLS ===


@mcp.tool()
def diagnostics(path: str, language: str = "python", project: str = "auto") -> dict:
    """Get type and lint diagnostics.

    For Python: runs pyright (types) + ruff (lint).

    Args:
        path: File or directory path
        language: Programming language
    """
    resolved_project, resolved_path = _resolve_tool_file(path, project)
    return _send_command(
        resolved_project, {"cmd": "diagnostics", "file": resolved_path, "language": language}
    )


@mcp.tool()
def change_impact(project: str = "auto", files: list[str] | None = None) -> dict:
    """Find tests affected by changed files.

    Uses call graph + import analysis to identify which tests to run.

    Args:
        project: Project root directory
        files: List of changed files (auto-detects from git if None)
    """
    return _send_command(_resolve_project(project), {"cmd": "change_impact", "files": files})


# === DAEMON MANAGEMENT ===


@mcp.tool()
def status(project: str = "auto") -> dict:
    """Get daemon status including uptime and cache statistics.

    Args:
        project: Project root directory
    """
    return _send_command(_resolve_project(project), {"cmd": "status"})


def main():
    """Entry point for code-briefcase-mcp command."""
    import argparse

    parser = argparse.ArgumentParser(description="Code Briefcase MCP Server")
    parser.add_argument(
        "--project",
        default="auto",
        help="Project root or 'auto' to resolve from CODE_BRIEFCASE_PROJECT/CLAUDE_PROJECT_DIR/CODEX_CWD/PWD",
    )
    args = parser.parse_args()

    if args.project != "auto":
        os.environ["CODE_BRIEFCASE_PROJECT"] = _resolve_project(args.project)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
