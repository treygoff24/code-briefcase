# Max-Impact Agent Context Integration Implementation Plan

**Goal:** Turn the TLDR fork into a global, always-on context governor for Claude Code and Codex that reduces blind file reads, injects targeted code understanding before edits, catches deterministic errors immediately after edits, and exposes a reliable MCP/CLI surface for manual agent use.

**Architecture:** Keep TLDR's Python package as the source of truth and add a package-owned hook runtime, installer, and task-oriented context packer. Hooks call TLDR through stable Python entry points instead of shelling to ad hoc scripts, while MCP remains the manual/interactive tool surface after daemon serialization and project-resolution bugs are fixed. The integration must degrade silently when context cannot be built and must never block normal agent work unless a hook is explicitly configured to enforce policy.

**Tech Stack:** Python 3.10+, argparse CLI, MCP FastMCP, Unix/TCP daemon sockets, tree-sitter/Python AST/Pygments, FAISS/sentence-transformers optional semantic search, Claude Code JSON hooks, Codex hooks.json, pytest.

---

## Current Evidence and Known Gaps

- Direct CLI structure/context commands work under the repo virtualenv.
- The full test suite currently has one failing TypeScript diagnostics test: tests/test_typescript_features.py::TestDiagnostics::test_diagnostics_invalid_file.
- Global tldr and tldr-mcp are already installed via pipx on this machine, but implementation and tests must not assume global install.
- Claude global config already has TLDR-adjacent hooks in ~/.claude-shared/settings.json:
  - PreToolUse Read -> ~/.claude-shared/hooks/tldr-read.mjs
  - PostToolUse Edit/Write/MultiEdit/Update -> ~/.claude-shared/hooks/post-edit-diagnostics.mjs
- Existing Claude hook scripts are useful prototypes, but they are external to this repo, parse old diagnostics shapes, and are not installable/testable as part of TLDR.
- Codex has ~/.codex/hooks.json with the expected top-level shape: {"hooks": {}}.
- Current daemon/MCP path is not safe to promote globally:
  - daemon context currently fails over the socket because a RelevantContext dataclass is returned inside a JSON response.
  - mcp_server._send_raw should get a pure response-decoding regression test so socket chunk handling stays correct.
  - daemon._ensure_call_graph_loaded() looks for .tldr/call_graph.json, while warm writes .tldr/cache/call_graph.json.
  - daemon._handle_impact() expects stale edge keys (caller, callee, file) instead of the actual edge shape (from_file, from_func, to_file, to_func).
  - daemon._handle_diagnostics() returns an old errors/summary schema while direct tldr diagnostics returns top-level diagnostics/error_count/warning_count.
- Claude Code hook docs confirm command hooks can emit JSON with hookSpecificOutput, permissionDecision, updatedInput, and systemMessage.
- Codex CLI 0.130 hook docs confirm hooks.json/config.toml discovery, command handler fields (type, command, timeout, statusMessage), and hookSpecificOutput.additionalContext for SessionStart, PreToolUse, and PostToolUse. Codex hooks are narrower than Claude hooks: they do not expose Read, and file edits are reported as apply_patch with Edit/Write matcher aliases.

## Product Shape

### Commands to Add or Harden

~~~bash
tldr pack "fix login bug" --project . --budget 3000
tldr pack --changed --project . --budget 3000
tldr hooks run session-start --client claude
tldr hooks run pre-read --client claude
tldr hooks run pre-edit --client claude
tldr hooks run post-edit --client claude
tldr hooks install claude --scope global --dry-run
tldr hooks install codex --scope global --dry-run
tldr hooks doctor
tldr-mcp --project auto
~~~

### Hook Behavior Target

~~~text
SessionStart
  - resolve project
  - ensure .tldrignore
  - start daemon if cheap
  - warm call graph/cache in background when safe
  - never download semantic model unless enabled

PreToolUse Read
  - for large code files, inject a nav map
  - for Claude, optionally add limit/offset to avoid giant reads
  - bypass small files, tests, config, secrets, non-code, and targeted reads

PreToolUse Edit/Write/MultiEdit; Codex apply_patch
  - identify target file and likely symbol
  - inject structure, nearby functions/classes, callers/callees, and diagnostics state
  - never mutate tool input except for explicit safety-preserving adjustments

PostToolUse Edit/Write/MultiEdit; Codex apply_patch
  - run fast file diagnostics when supported
  - notify daemon/dirty state
  - report only actionable errors/warnings
  - suggest targeted tests when confidence is high

Stop / PreCompact
  - optional summary of changed files, changed symbols, diagnostics, and affected tests
~~~

## Non-Goals

- Do not make semantic model downloads automatic on session start.
- Do not require Node scripts for hooks; Node prototypes can remain external but repo-owned hooks should be Python.
- Do not replace Claude/Codex native tools. TLDR should guide and compress context, not become a mandatory read/edit proxy.
- Do not install or mutate global user config without an explicit install command and backup.
- Do not add remote services or credentials.
- Do not claim Codex Read hooks are supported; Codex hook integration is limited to documented SessionStart and apply_patch-backed edit events unless future CLI docs add more events.

---

## Task 1: Stabilize Daemon and MCP Contracts

**Parallel:** no  
**Blocked by:** none  
**Owned files:** tldr/daemon/cached_queries.py, tldr/daemon/core.py, tldr/mcp_server.py, tests/test_daemon_mcp_contracts.py  
**Invariants:** Direct CLI behavior for context, calls, impact, tree, and structure must remain compatible. Daemon responses must always be JSON-serializable. MCP tools must not auto-download semantic models unless semantic search/index is explicitly called.  
**Out of scope:** Hook installer, context packer ranking, global config writes.

**Files:**
- Create: tests/test_daemon_mcp_contracts.py
- Modify: tldr/daemon/cached_queries.py
- Modify: tldr/daemon/core.py
- Modify: tldr/mcp_server.py

**Step 1: Add a regression test for daemon context serialization**

Create tests/test_daemon_mcp_contracts.py:

~~~python
import json
from pathlib import Path

from tldr.daemon.core import TLDRDaemon


def test_daemon_context_response_is_json_serializable(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text(
        "def helper():\n"
        "    return 1\n\n"
        "def main():\n"
        "    return helper()\n"
    )

    daemon = TLDRDaemon(tmp_path)
    response = daemon.handle_command(
        {"cmd": "context", "entry": "main", "language": "python", "depth": 1}
    )

    json.dumps(response)
    assert response["status"] == "ok"
    assert "main" in json.dumps(response)
~~~

**Step 2: Add tests for call graph cache path and impact response**

Append:

~~~python
def test_daemon_loads_call_graph_from_cache_dir(tmp_path: Path):
    cache_dir = tmp_path / ".tldr" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "call_graph.json").write_text(json.dumps({
        "edges": [
            {
                "from_file": "app.py",
                "from_func": "main",
                "to_file": "util.py",
                "to_func": "helper",
            }
        ],
        "languages": ["python"],
        "timestamp": 1,
    }))

    daemon = TLDRDaemon(tmp_path)
    daemon._ensure_call_graph_loaded()

    assert daemon.indexes["call_graph"]["edges"][0]["to_func"] == "helper"


def test_daemon_impact_uses_current_edge_shape(tmp_path: Path):
    cache_dir = tmp_path / ".tldr" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "call_graph.json").write_text(json.dumps({
        "edges": [
            {
                "from_file": "app.py",
                "from_func": "main",
                "to_file": "util.py",
                "to_func": "helper",
            }
        ],
        "languages": ["python"],
        "timestamp": 1,
    }))

    daemon = TLDRDaemon(tmp_path)
    response = daemon.handle_command({"cmd": "impact", "func": "helper"})

    assert response["status"] == "ok"
    payload = json.dumps(response)
    assert "main" in payload
    assert "helper" in payload
~~~

**Step 3: Add an MCP socket chunk regression**

Extract JSON response decoding into a pure helper and test it. This is a stability regression, not necessarily a currently failing test:

~~~python
from tldr.mcp_server import _decode_socket_response


def test_mcp_decode_socket_response_does_not_duplicate_chunks():
    payload = {"status": "ok", "result": "abc"}
    raw = json.dumps(payload).encode()
    midpoint = len(raw) // 2

    assert _decode_socket_response([raw[:midpoint], raw[midpoint:]]) == payload
~~~

**Step 4: Add a daemon diagnostics schema regression**

Append:

~~~python
def test_daemon_diagnostics_uses_current_schema(tmp_path: Path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")

    def fake_get_diagnostics(path, language=None, include_lint=True):
        return {
            "file": path,
            "language": "python",
            "tools": ["pyright"],
            "diagnostics": [],
            "error_count": 0,
            "warning_count": 0,
        }

    monkeypatch.setattr("tldr.diagnostics.get_diagnostics", fake_get_diagnostics)

    daemon = TLDRDaemon(tmp_path)
    response = daemon.handle_command(
        {"cmd": "diagnostics", "file": str(source), "language": "python"}
    )

    assert response["status"] == "ok"
    assert "diagnostics" in response
    assert "error_count" in response
    assert "warning_count" in response
    assert "summary" not in response
~~~

**Step 5: Run tests to confirm failure**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_daemon_mcp_contracts.py -q
~~~

Expected: at least the context serialization and diagnostics schema tests fail before implementation.

**Step 6: Fix daemon cached context**

In tldr/daemon/cached_queries.py, change cached_context so it returns JSON-safe data:

~~~python
@salsa_query
def cached_context(db: SalsaDB, project: str, entry: str, language: str, depth: int) -> dict:
    """Cached relevant context - memoized by SalsaDB."""
    from tldr.api import get_relevant_context
    ctx = get_relevant_context(project, entry, language=language, depth=depth)
    return {
        "status": "ok",
        "result": ctx.to_llm_string(),
        "entry_point": ctx.entry_point,
        "depth": ctx.depth,
        "functions": [
            {
                "name": f.name,
                "file": f.file,
                "line": f.line,
                "signature": f.signature,
                "docstring": f.docstring,
                "calls": f.calls,
                "blocks": f.blocks,
                "cyclomatic": f.cyclomatic,
            }
            for f in ctx.functions
        ],
    }
~~~

**Step 7: Fix daemon call graph cache path**

In tldr/daemon/core.py, update _ensure_call_graph_loaded() to prefer .tldr/cache/call_graph.json and fall back to .tldr/call_graph.json for backward compatibility:

~~~python
cache_path = self.tldr_dir / "cache" / "call_graph.json"
legacy_path = self.tldr_dir / "call_graph.json"
call_graph_path = cache_path if cache_path.exists() else legacy_path
~~~

**Step 8: Fix daemon impact**

Replace stale edge-key logic with current edge shape. Minimal implementation:

~~~python
callers = []
for edge in edges:
    if isinstance(edge, dict):
        from_file = edge.get("from_file")
        from_func = edge.get("from_func")
        to_file = edge.get("to_file")
        to_func = edge.get("to_func")
    else:
        from_file, from_func, to_file, to_func = edge

    if to_func == func_name:
        callers.append({
            "caller": from_func,
            "caller_file": from_file,
            "callee": to_func,
            "callee_file": to_file,
        })
return {"status": "ok", "callers": callers, "count": len(callers)}
~~~

**Step 9: Fix daemon diagnostics schema**

In tldr/daemon/core.py, replace the old _handle_diagnostics implementation with delegation to the current diagnostics module:

~~~python
def _handle_diagnostics(self, command: dict) -> dict:
    file_path = command.get("file")
    check_project = command.get("project", False)
    no_lint = command.get("no_lint", False)
    language = command.get("language")

    try:
        from tldr.diagnostics import get_diagnostics, get_project_diagnostics

        if check_project:
            result = get_project_diagnostics(
                str(self.project),
                language=language or "python",
                include_lint=not no_lint,
            )
        else:
            if not file_path:
                return {"status": "error", "message": "Missing required parameter: file"}
            result = get_diagnostics(
                file_path,
                language=language,
                include_lint=not no_lint,
            )
        return {"status": "ok", **result}
    except Exception as e:
        logger.exception("Diagnostics failed")
        return {"status": "error", "message": str(e)}
~~~

**Step 10: Fix MCP socket decoding**

In tldr/mcp_server.py, add:

~~~python
def _decode_socket_response(chunks: list[bytes]) -> dict:
    return json.loads(b"".join(chunks))
~~~

Then change _send_raw to append each chunk exactly once:

~~~python
chunks.append(chunk)
try:
    return _decode_socket_response(chunks)
except json.JSONDecodeError:
    continue
~~~

**Step 11: Run targeted tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_daemon_mcp_contracts.py -q
~~~

Expected: PASS.

**Step 12: Smoke daemon over real socket with cleanup trap**

Run:

~~~bash
.venv/bin/python -m tldr.cli daemon start --project .
trap '.venv/bin/python -m tldr.cli daemon stop --project . >/dev/null 2>&1 || true' EXIT
.venv/bin/python - <<'PY'
from tldr.daemon import query_daemon
result = query_daemon(".", {"cmd": "context", "entry": "get_relevant_context", "language": "python", "depth": 1})
assert result["status"] == "ok", result
assert "get_relevant_context" in result["result"], result
print("daemon context ok")
PY
.venv/bin/python -m tldr.cli daemon stop --project .
trap - EXIT
~~~

Expected: prints daemon context ok.

**Verification plan:**
- Primary: .venv/bin/python -m pytest tests/test_daemon_mcp_contracts.py -q
- Secondary: real daemon smoke command above
- Regression: .venv/bin/python -m pytest tests/test_daemon_stats.py -q

---

## Task 2: Fix Diagnostics Contract and Existing TypeScript Failure

**Parallel:** no  
**Blocked by:** none, but complete before broad gates.  
**Owned files:** tldr/diagnostics.py, tests/test_typescript_features.py, tests/test_diagnostics_js_typecheck.py, tests/test_diagnostics_local_bin.py  
**Invariants:** Diagnostics output keeps top-level diagnostics, error_count, warning_count, tools, file, and language. Existing local-bin resolution behavior must keep working.  
**Out of scope:** Hook post-edit rendering; that is Task 5.

**Files:**
- Modify: tldr/diagnostics.py
- Modify only if needed: tests/test_typescript_features.py
- Modify only if needed: tests/test_diagnostics_js_typecheck.py
- Modify only if needed: tests/test_diagnostics_local_bin.py

**Step 1: Reproduce current failure**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_typescript_features.py::TestDiagnostics::test_diagnostics_invalid_file -q
~~~

Expected before fix: FAIL with zero diagnostics.

**Step 2: Inspect the tool resolution path**

Run:

~~~bash
.venv/bin/python - <<'PY'
from pathlib import Path
from tldr.diagnostics import _resolve_tool
print(_resolve_tool("tsc", Path("tests/fixtures/oxlint_sample.ts").resolve()))
PY
~~~

Expected: either a local/project tool or None; use this to confirm whether the failing fixture is caused by temp path normalization, local bin search, or project config detection.

**Step 3: Add a narrow regression if the existing test is not enough**

If the root cause is /private/var vs /var temp symlink mismatch, add a test in tests/test_diagnostics_local_bin.py that creates a temp project, resolves the file through both realpath and symlink path, and asserts _resolve_tool("tsc", file_path) finds node_modules/.bin/tsc.

**Step 4: Fix the resolver or runner**

Likely fix locations:
- _resolve_tool(name, start) should resolve start once and walk parents from the file's containing directory.
- JS/TS single-file diagnostics should use the project-local node_modules/.bin/tsc when present.
- Single-file tsc output parser must parse stdout and stderr consistently.

Implementation constraints:
- Do not call shell with interpolated file paths.
- Preserve current ephemeral tsconfig behavior for JS/TS.
- Preserve filtering of project diagnostics back down to the target file.

**Step 5: Run targeted diagnostics tests**

Run:

~~~bash
.venv/bin/python -m pytest \
  tests/test_typescript_features.py::TestDiagnostics::test_diagnostics_invalid_file \
  tests/test_diagnostics_js_typecheck.py \
  tests/test_diagnostics_local_bin.py \
  -q
~~~

Expected: PASS.

**Step 6: Run all diagnostics tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_diagnostics_*.py tests/test_typescript_features.py::TestDiagnostics -q
~~~

Expected: PASS.

**Verification plan:**
- Primary: targeted command in Step 5
- Secondary: all diagnostics command in Step 6
- Final gate contribution: full .venv/bin/python -m pytest -q in Task 11

---

## Task 3: Add Package-Owned Hook Runtime and Event Normalizer

**Parallel:** no  
**Blocked by:** Task 1 for stable daemon/MCP assumptions; Task 2 for diagnostics schema confidence.  
**Owned files:** tldr/hooks/__init__.py, tldr/hooks/runtime.py, tldr/hooks/session.py, tests/test_hooks_runtime.py  
**Invariants:** Hook runtime must be pure-Python, must read JSON from stdin only in CLI adapter code, and must expose pure functions for tests. Unknown hook inputs must produce no-op JSON, not crashes.  
**Out of scope:** Concrete read/edit/post-edit hook behavior and installers.

**Files:**
- Create: tldr/hooks/__init__.py
- Create: tldr/hooks/runtime.py
- Create: tldr/hooks/session.py
- Create: tests/test_hooks_runtime.py

**Step 1: Define hook dataclasses**

Create tldr/hooks/runtime.py:

~~~python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ClientName = Literal["claude", "codex", "generic"]

@dataclass
class HookEvent:
    client: ClientName
    event_name: str
    tool_name: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_result: dict[str, Any] = field(default_factory=dict)
    cwd: Path = field(default_factory=Path.cwd)
    session_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

@dataclass
class HookResponse:
    message: str | None = None
    permission_decision: Literal["allow", "deny", "ask"] | None = None
    updated_input: dict[str, Any] | None = None
    additional_context: str | None = None
    suppress_output: bool = True

    @classmethod
    def noop(cls) -> "HookResponse":
        return cls()
~~~

**Step 2: Create temporary no-op session hook**

Create tldr/hooks/session.py now so CLI routing and installer code can safely reference session-start before Task 10 fills in warm/daemon behavior:

~~~python
from tldr.hooks.runtime import HookEvent, HookResponse


def build_session_start_response(event: HookEvent) -> HookResponse:
    return HookResponse.noop()
~~~

Task 10 replaces this no-op implementation before any real global install is allowed.

**Step 3: Implement event parsing**

Add parse_hook_event(payload, client="generic"). It must accept Claude/Codex-like keys:
- hook_event_name or event
- tool_name
- tool_input or toolInput
- tool_result, toolResult, tool_response, or toolResponse
- cwd, project_dir, project, or current directory
- session_id or sessionId

The normalized HookEvent must preserve both tool_result and tool_response payloads. If both are present, prefer tool_result but retain raw input so post-edit file extraction can fall back to tool_response.filePath.

**Step 4: Implement response rendering**

Add render_hook_response(response, client):
- No-op response returns {}
- Claude response can include continue, suppressOutput, hookSpecificOutput.hookEventName, hookSpecificOutput.permissionDecision, hookSpecificOutput.updatedInput, hookSpecificOutput.additionalContext, and systemMessage.
- Codex response uses the documented hookSpecificOutput.hookEventName + additionalContext shape for SessionStart, PreToolUse, and PostToolUse, and avoids unsupported PreToolUse continue/suppressOutput controls.

**Step 5: Add tests**

Create tests/test_hooks_runtime.py:
- parse Claude tool event
- render no-op is empty
- render Claude pre-tool response includes permissionDecision, updatedInput, and additionalContext
- render Codex PreToolUse/PostToolUse/SessionStart responses use hookSpecificOutput.hookEventName + additionalContext
- parse Codex payload with tool_input
- parse Codex payload with tool_response / toolResponse, including tool_response.filePath
- session-start no-op can be imported and rendered safely

**Step 6: Run tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_hooks_runtime.py -q
~~~

Expected: PASS.

**Verification plan:**
- Primary: .venv/bin/python -m pytest tests/test_hooks_runtime.py -q

---

## Task 4: Implement Read and Pre-Edit Context Hooks

**Parallel:** no  
**Blocked by:** Task 3  
**Owned files:** tldr/hooks/read.py, tldr/hooks/edit.py, tests/test_hooks_read.py, tests/test_hooks_edit.py  
**Invariants:** Hooks must silently no-op on unsupported files, missing files, binary files, secrets, test files, and already-targeted reads. Hooks must not emit more than the configured context budget.  
**Out of scope:** Hook CLI registration and installer.

**Files:**
- Create: tldr/hooks/read.py
- Create: tldr/hooks/edit.py
- Create: tests/test_hooks_read.py
- Create: tests/test_hooks_edit.py

**Step 1: Implement code-file filters**

In tldr/hooks/read.py define CODE_EXTENSIONS, BYPASS_SUFFIXES, and BYPASS_PARTS for source files and obvious junk/secrets. Implement should_bypass_read(file_path, tool_input).

Rules:
- bypass if extension unsupported
- bypass if path component is .git, .tldr, .venv, node_modules, dist, build, or coverage
- bypass if offset exists
- bypass if limit exists and limit < 100
- bypass if file size < 1500 bytes
- bypass test/spec files by filename patterns

**Step 2: Implement nav map formatter**

Use tldr.api.extract_file and produce compact context:

~~~text
[TLDR nav map: api.py]

Imports:
- pathlib: Path
- tldr.cfg_extractor: extract_python_cfg, ...

Functions:
- get_relevant_context(project, entry_point, depth, language, include_docstrings) -> RelevantContext [L525]
  # Get token-efficient context for an LLM starting from an entry point.

Classes:
- RelevantContext [L389]
  .to_llm_string(self) [L394]

Read specific lines with offset=N limit=M.
~~~

**Step 3: Implement build_read_response(event)**

Behavior:
- only handle tool_name Read
- extract file_path/path from tool_input
- bypass unsupported/targeted/small files
- call extract_file
- build nav map
- for Claude: return permission_decision allow, updated_input with file_path and limit, additional_context with nav map
- no Codex Read hook is installed because Codex CLI 0.130 does not expose Read as a PreToolUse hook event

**Step 4: Write read hook tests**

Create tests/test_hooks_read.py:
- large code file returns context and limit
- small code file no-ops
- targeted read no-ops
- markdown/config file no-ops
- test file no-ops

**Step 5: Implement pre-edit context hook**

In tldr/hooks/edit.py implement:
- extract_target_file(event) for Claude Edit/Write/MultiEdit/Update and Codex apply_patch
- build_pre_edit_response(event, budget=2000)
- use extract_file(file_path) for structure
- use tldr.api.get_imports(file_path, language=detected)
- optionally call get_relevant_context(project, likely_symbol, depth=1) only when a likely symbol can be extracted cheaply
- output a concise context block, not raw file content

Initial context block:

~~~text
[TLDR edit context: src/auth.py]

File structure:
- login(username, password) [L18]
- verify_access_token(token) [L52]
- AuthError [L7]

Imports:
- jwt
- database.get_user

Before editing:
- preserve signatures unless task requires API change
- after edit, diagnostics hook will run
~~~

**Step 6: Write edit hook tests**

Create tests/test_hooks_edit.py:
- Edit event on code file returns structure
- Write event for new file no-ops or returns minimal message without crashing
- Non-code file no-ops
- Output stays under budget

**Step 7: Run hook tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_hooks_read.py tests/test_hooks_edit.py -q
~~~

Expected: PASS.

**Verification plan:**
- Primary: .venv/bin/python -m pytest tests/test_hooks_read.py tests/test_hooks_edit.py -q
- Manual after Task 7: pipe a Claude-like JSON event into hooks run pre-read

---

## Task 5: Implement Post-Edit Diagnostics and Dirty Notify Hook

**Parallel:** no  
**Blocked by:** Task 2, Task 3  
**Owned files:** tldr/hooks/post_edit.py, tests/test_hooks_post_edit.py  
**Invariants:** Post-edit hook must be fast, best-effort, and silent on clean results. It must parse the current diagnostics schema (diagnostics, error_count, warning_count) and never depend on old summary.type_errors fields.  
**Out of scope:** Installing hooks into Claude/Codex configs.

**Files:**
- Create: tldr/hooks/post_edit.py
- Create: tests/test_hooks_post_edit.py

**Step 1: Implement edited-file extraction**

Support Claude Edit/Write/MultiEdit/Update and Codex apply_patch. Read candidate file paths in this order:
1. tool_input.file_path
2. tool_input.path
3. tool_response.file_path
4. tool_response.filePath
5. toolResponse.file_path
6. toolResponse.filePath

This is required for Codex-shaped payloads; missing all paths should no-op, not crash.

**Step 2: Implement diagnostics formatter**

Format current diagnostics schema:

~~~text
TLDR diagnostics for app.ts: 1 errors, 0 warnings
- app.ts:1:7 [tsc] Type 'number' is not assignable to type 'string'.
~~~

**Step 3: Implement dirty notify**

Best effort:
1. query daemon with {"cmd": "notify", "file": file_path}
2. if daemon unavailable, call dirty_flag.mark_dirty(project, file_path)
3. swallow failures

**Step 4: Implement build_post_edit_response(event)**

- detect language from extension
- call tldr.diagnostics.get_diagnostics(str(file_path), language=detected)
- call notify_daemon(event.cwd, file_path) regardless of diagnostic result
- return no-op if no diagnostic message
- return HookResponse(message=message, suppress_output=False) if actionable diagnostics exist

**Step 5: Add tests**

Create tests/test_hooks_post_edit.py:
- clean diagnostics no-op
- error diagnostics message includes top-level count and first diagnostic
- notify fallback marks dirty when daemon unavailable
- unsupported extension no-op
- Codex-shaped payload with tool_response.filePath finds the edited file
- Codex-shaped payload with toolResponse.filePath finds the edited file

**Step 6: Run tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_hooks_post_edit.py -q
~~~

Expected: PASS.

**Verification plan:**
- Primary: .venv/bin/python -m pytest tests/test_hooks_post_edit.py -q
- Secondary: targeted diagnostics tests from Task 2

---

## Task 6: Add tldr pack Context Packer

**Parallel:** no  
**Blocked by:** Task 1 for stable context calls; Task 2 for diagnostics.  
**Owned files:** tldr/context_pack.py, tests/test_context_pack.py  
**Invariants:** tldr pack must respect .tldrignore, never include secret-looking files, enforce budget, and degrade when semantic index is missing. It must not require daemon or semantic search to succeed.  
**Out of scope:** Hook installer and global config.

**Files:**
- Create: tldr/context_pack.py
- Create: tests/test_context_pack.py
- Modify later in Task 7: tldr/cli.py

**Step 1: Define context pack result types**

Create ContextPackItem and ContextPack dataclasses with to_markdown() and to_dict() methods.

**Step 2: Implement budget estimation**

Use tiktoken if available; fallback to len(text) // 4.

**Step 3: Implement candidate gathering**

Function:

~~~python
def build_context_pack(
    query: str,
    project: str | Path = ".",
    budget: int = 3000,
    files: list[str] | None = None,
    changed: bool = False,
    include_semantic: bool = True,
    language: str = "auto",
) -> ContextPack:
    ...
~~~

Ranking order:
1. Explicit files
2. Changed files from dirty flags or git diff when changed=True
3. Semantic search results if semantic index exists and include_semantic=True
4. Text search fallback for query terms
5. Project structure summary if no better candidates

Each code file item should include:
- path
- imports summary
- functions/classes with line numbers
- diagnostics summary when cheap
- call context if a symbol is confidently identified

**Step 4: Implement markdown output**

Target shape:

~~~markdown
# TLDR Context Pack

Query: fix login bug
Project: /repo
Budget: 3000 tokens
Estimated tokens: 1820

## High-Signal Files

### src/auth.py
- login(username, password) [L18]
- verify_access_token(token) [L52]
- calls: create_session, get_user

## Diagnostics
- src/auth.py: clean

## Suggested Next Reads
- Read src/auth.py offset=18 limit=80
- tldr impact verify_access_token .
~~~

**Step 5: Add tests**

Create tests/test_context_pack.py:
- explicit file produces function outline
- budget is enforced
- missing semantic index does not crash
- changed mode handles no git repo gracefully
- secret-looking files are excluded

**Step 6: Run tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_context_pack.py -q
~~~

Expected: PASS.

**Verification plan:**
- Primary: .venv/bin/python -m pytest tests/test_context_pack.py -q
- Manual after CLI wiring: .venv/bin/python -m tldr.cli pack "daemon context bug" --project . --budget 2500

---

## Task 7: Wire Hook and Pack Commands into CLI

**Parallel:** no  
**Blocked by:** Tasks 3, 4, 5, 6  
**Owned files:** tldr/cli.py, tldr/hooks/runner.py, tests/test_cli_hooks_pack.py  
**Invariants:** Existing CLI commands and help text must remain compatible. Hook runner must read stdin once, write JSON once, and exit 0 for no-op.  
**Out of scope:** Installer implementation internals; Task 8 owns installer files.

**Files:**
- Modify: tldr/cli.py
- Create: tldr/hooks/runner.py
- Create: tests/test_cli_hooks_pack.py

**Step 1: Add pack parser**

Add subparser for:
- query positional, optional
- --project
- --budget
- --file repeatable
- --changed
- --no-semantic
- --json

Dispatch to context_pack.build_context_pack and print markdown or JSON.

**Step 2: Add hooks run parser**

Add:
- tldr hooks run session-start --client claude
- tldr hooks run pre-read --client claude
- tldr hooks run pre-edit --client claude
- tldr hooks run post-edit --client claude

**Step 3: Create hook runner module**

Create tldr/hooks/runner.py:
- run_hook(event_name, payload, client)
- run_hook_from_stdin(event_name, client)
- route session-start/read/edit/post-edit to the hook modules
- render through runtime.render_hook_response

**Step 4: Add CLI tests**

Create tests/test_cli_hooks_pack.py:
- tldr pack --json on temp file returns JSON
- tldr hooks run session-start --client claude reads stdin and returns valid JSON/no-op JSON
- tldr hooks run pre-read --client claude reads stdin and returns valid JSON
- no-op hook returns {}

**Step 5: Run CLI tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_cli_hooks_pack.py -q
~~~

Expected: PASS.

**Verification plan:**
- Primary: .venv/bin/python -m pytest tests/test_cli_hooks_pack.py -q
- Manual:
  ~~~bash
  printf '{"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"file_path":"tldr/api.py"},"cwd":"."}' \
    | .venv/bin/python -m tldr.cli hooks run pre-read --client claude \
    | python -m json.tool
  ~~~

---

## Task 8: Add Hook Installer and Doctor

**Parallel:** no  
**Blocked by:** Task 7  
**Owned files:** tldr/hook_installer.py, tests/test_hook_installer.py, tldr/cli.py  
**Invariants:** Installer must merge config, not overwrite. Installer must create timestamped backups before writes. Installer must support --dry-run. Installer must not remove unrelated hooks/MCP servers. Real global installation must not be performed until Task 10 replaces the temporary session-start no-op with the full safe warm behavior.  
**Out of scope:** Actually mutating Trey's global config during tests.

**Files:**
- Create: tldr/hook_installer.py
- Create: tests/test_hook_installer.py
- Modify: tldr/cli.py

**Step 1: Define hook command builders**

Resolve hook commands at install time:
- tldr_path = shutil.which("tldr"); fail dry-run with an actionable message if missing.
- tldr_mcp_path = shutil.which("tldr-mcp"); doctor reports missing if absent.
- Write absolute commands, shell-quoted when needed, for example:
  - "/Users/treygoff/.local/bin/tldr" hooks run pre-read --client claude
  - "/Users/treygoff/.local/bin/tldr" hooks run post-edit --client codex
- Tests must assert installed hook commands use absolute paths unless a test explicitly passes a fake command path.

Commands should look like:
- tldr hooks run session-start --client claude
- tldr hooks run pre-edit --client codex

**Step 2: Define desired Claude hook config entries**

Events:
- SessionStart matcher .*
- PreToolUse matcher Read
- PreToolUse matcher Edit|Write|MultiEdit|Update
- PostToolUse matcher Edit|Write|MultiEdit|Update

Each hook is type command with timeout and command from Step 1.

**Step 3: Define desired Codex hook config entries**

Codex config shape:

~~~json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "startup|resume|clear",
      "hooks": [{
        "type": "command",
        "command": "tldr hooks run session-start --client codex",
        "timeout": 10,
        "statusMessage": "TLDR starting context"
      }]
    }],
    "PreToolUse": [{
      "matcher": "apply_patch|Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "tldr hooks run pre-edit --client codex",
        "timeout": 10,
        "statusMessage": "TLDR building edit context"
      }]
    }]
  }
}
~~~

Use events:
- SessionStart matcher startup|resume|clear
- PreToolUse matcher apply_patch|Edit|Write
- PostToolUse matcher apply_patch|Edit|Write

**Step 4: Implement merge logic**

Functions:
- load_json(path) -> dict
- backup_file(path) -> Path
- merge_hook_group(existing, desired, marker="tldr hooks run") -> dict
- install_hooks(client, scope, config_path, dry_run=False) -> InstallResult

Rules:
- If an existing TLDR hook command exists for the same event/matcher, replace it.
- Treat legacy TLDR-owned hook commands as replaceable, not unrelated:
  - commands containing /tldr-read.mjs
  - commands containing /post-edit-diagnostics.mjs
- Dry-run must report each legacy replacement explicitly as "replace legacy TLDR hook".
- Preserve unrelated hooks such as rtk hook claude, disable-vercel-plugin-hooks.mjs, permissions, plugins, statusLine, and MCP config.
- Add --keep-legacy only if there is a real need to run both generations; default must avoid duplicate TLDR behavior.
- Preserve all non-TLDR hook groups.
- Preserve JSON indentation.
- Create parent dirs if missing.
- Backup only when writing an existing file.

**Step 5: Add CLI parser for install/doctor**

Extend tldr hooks:
- tldr hooks install claude --scope global --config PATH --dry-run
- tldr hooks install codex --scope global --config PATH --dry-run
- tldr hooks doctor --client claude --client codex

Default config paths:
- Claude: ~/.claude/settings.json
- Codex: ~/.codex/hooks.json

Note: Trey's ~/.claude/settings.json is symlinked to shared config; installer should follow the symlink naturally but print the resolved path.

**Step 6: Implement doctor**

Doctor should report:
- tldr executable path
- tldr-mcp executable path
- package version
- Claude config path exists and whether TLDR hooks are present
- Codex hooks path exists and whether TLDR hooks are present
- daemon status for current project if running
- semantic index presence only; do not build it

**Step 7: Add installer tests**

Create tests/test_hook_installer.py:
- dry-run does not write
- merge preserves existing hooks
- re-running installer is idempotent
- backup created on write
- Codex output has top-level hooks
- Claude output has hooks key compatible with settings file
- Existing Claude PreToolUse Read legacy tldr-read.mjs is replaced, not duplicated
- Existing Claude PostToolUse Edit|Write|MultiEdit|Update legacy diagnostics hook is replaced, not duplicated
- Non-TLDR hooks in the same events remain unchanged
- Unrelated settings keys and schema keys remain unchanged
- Installed hook commands use absolute executable paths

**Step 8: Run tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_hook_installer.py tests/test_cli_hooks_pack.py -q
~~~

Expected: PASS.

**Step 9: Manual dry-run against real user config**

Run only after tests pass:

~~~bash
.venv/bin/python -m tldr.cli hooks install claude --scope global --dry-run
.venv/bin/python -m tldr.cli hooks install codex --scope global --dry-run
.venv/bin/python -m tldr.cli hooks doctor
~~~

Expected: prints planned changes without modifying config.

**Verification plan:**
- Primary: .venv/bin/python -m pytest tests/test_hook_installer.py -q
- Manual: dry-run commands above
- Do not perform real global install until Trey explicitly asks.

---

## Task 9: Add Dynamic MCP Project Resolution

**Parallel:** no  
**Blocked by:** Task 1  
**Owned files:** tldr/mcp_server.py, tests/test_mcp_project_resolution.py  
**Invariants:** Existing tldr-mcp --project /path behavior must keep working. --project auto must never resolve to a non-existent path silently. Tool-level explicit project args must override environment defaults.  
**Out of scope:** Hook installation.

**Files:**
- Modify: tldr/mcp_server.py
- Create: tests/test_mcp_project_resolution.py
- Modify docs later in Task 11: README.md

**Step 1: Add resolver**

In tldr/mcp_server.py:

~~~python
def _resolve_project(project: str | None = None) -> str:
    explicit = project not in (None, "", "auto")
    if explicit:
        path = Path(project).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError(f"TLDR project does not exist: {path}")
        return str(path)

    candidates = [
        os.environ.get("TLDR_PROJECT"),
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

    raise FileNotFoundError("Could not resolve TLDR project root")
~~~

**Step 2: Use resolver in every MCP tool**

For tools with project default ".", treat project="auto" as dynamic. Tool-level explicit project args must override environment defaults.

**Step 3: Add --project auto help text**

Change argparse default to auto:

~~~python
parser.add_argument(
    "--project",
    default="auto",
    help="Project root or 'auto' to resolve from TLDR_PROJECT/CLAUDE_PROJECT_DIR/CODEX_CWD/PWD",
)
~~~

Update main() so --project auto does not write a literal auto path into TLDR_PROJECT:
- if args.project == "auto", leave TLDR_PROJECT unchanged
- otherwise validate the explicit path through _resolve_project(args.project) and set TLDR_PROJECT to that resolved path

**Step 4: Add tests**

Create tests/test_mcp_project_resolution.py:
- explicit path wins
- TLDR_PROJECT wins over PWD
- missing env falls back to cwd
- nonexistent explicit path raises
- tool functions call _resolve_project
- --project auto does not set TLDR_PROJECT to <cwd>/auto
- explicit nonexistent project raises even when PWD exists
- relative file-tool arguments resolve against the resolved project root, not Path(file).parent as a daemon project

**Step 5: Run tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_mcp_project_resolution.py tests/test_daemon_mcp_contracts.py -q
~~~

Expected: PASS.

**Verification plan:**
- Primary: .venv/bin/python -m pytest tests/test_mcp_project_resolution.py -q
- Secondary: tldr-mcp --help

---

## Task 10: Add Session Warm Hook

**Parallel:** no  
**Blocked by:** Tasks 3, 7  
**Owned files:** tldr/hooks/session.py, tests/test_hooks_session.py  
**Invariants:** Session start must be cheap and non-blocking. It must not download semantic models. It must not run full expensive indexing for huge repos synchronously.  
**Out of scope:** Installer internals.

**Files:**
- Modify: tldr/hooks/session.py
- Create: tests/test_hooks_session.py

**Step 1: Implement project sizing**

Count source files respecting coarse excludes. If source files > configurable threshold (default 500), start daemon only and skip warm.

**Step 2: Implement session start behavior**

Behavior:
- ensure .tldrignore
- start daemon best-effort
- schedule background warm for small repos
- never run semantic index
- return concise hidden systemMessage, or no-op if nothing useful happened

**Step 3: Use safe background warm**

Use subprocess.Popen with:
- current Python executable
- -m tldr.cli warm <project> --lang all
- stdout/stderr to .tldr/logs/session-warm.log
- no blocking wait inside hook

**Step 4: Add tests**

- no crash on empty project
- no semantic index command
- large repo skips warm
- small repo schedules warm with monkeypatched Popen

**Step 5: Run tests**

Run:

~~~bash
.venv/bin/python -m pytest tests/test_hooks_session.py -q
~~~

Expected: PASS.

**Verification plan:**
- Primary: .venv/bin/python -m pytest tests/test_hooks_session.py -q
- Manual:
  ~~~bash
  printf '{"hook_event_name":"SessionStart","cwd":"."}' \
    | .venv/bin/python -m tldr.cli hooks run session-start --client claude \
    | python -m json.tool
  ~~~

---

## Task 11: Documentation, Dogfood Script, and Final Gates

**Parallel:** no  
**Blocked by:** Tasks 1-10  
**Owned files:** README.md, docs/TLDR.md, scripts/dogfood_agent_context.py, tests/test_readme_examples.py  
**Invariants:** Docs must not overclaim daemon auto-start or semantic behavior. Examples must match actual CLI syntax. Dogfood script must be safe and avoid global config writes by default.  
**Out of scope:** Real global installation.

**Files:**
- Modify: README.md
- Modify: docs/TLDR.md
- Create: scripts/dogfood_agent_context.py
- Create if useful: tests/test_readme_examples.py

**Step 1: Update README quickstart**

Add:

~~~bash
pipx install llm-tldr

# Manual use
tldr pack "understand auth flow" --project . --budget 3000
tldr context main --project .

# Agent integration dry-run
tldr hooks doctor
tldr hooks install claude --scope global --dry-run
tldr hooks install codex --scope global --dry-run
~~~

Clarify:
- semantic search requires explicit tldr semantic index
- session hook never downloads the model
- MCP is optional/manual
- Claude hooks are the most automatic path
- Codex hooks are supported for documented SessionStart and apply_patch edit events; MCP is the portable fallback for Codex read context

**Step 2: Update MCP docs**

Add dynamic MCP examples:

~~~json
{
  "mcpServers": {
    "tldr": {
      "command": "tldr-mcp",
      "args": ["--project", "auto"]
    }
  }
}
~~~

Note: hooks provide automatic context; MCP provides explicit tool calls.

**Step 3: Add dogfood script**

Create scripts/dogfood_agent_context.py that:
- creates a temp Python project
- runs tldr pack
- runs hooks run pre-read --client claude with a Claude fixture
- runs hooks run pre-edit --client codex with an apply_patch-shaped Codex fixture
- runs hooks run post-edit --client codex with a Codex fixture containing tool_response.filePath
- runs hooks run post-edit with simulated diagnostics if needed
- starts daemon and queries context over socket
- prints JSON summary

Do not write global config.

**Step 4: Add readme command smoke tests**

If feasible, add tests that smoke the core examples:
- tldr pack --help
- tldr hooks --help
- tldr-mcp --help through module import path if possible

**Step 5: Run targeted gates**

Run:

~~~bash
.venv/bin/python -m pytest \
  tests/test_daemon_mcp_contracts.py \
  tests/test_hooks_runtime.py \
  tests/test_hooks_read.py \
  tests/test_hooks_edit.py \
  tests/test_hooks_post_edit.py \
  tests/test_hooks_session.py \
  tests/test_context_pack.py \
  tests/test_hook_installer.py \
  tests/test_mcp_project_resolution.py \
  tests/test_cli_hooks_pack.py \
  -q
~~~

Expected: PASS.

**Step 6: Run full gate**

Run:

~~~bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check tldr tests
.venv/bin/python -m tldr.cli --version
.venv/bin/python -m tldr.cli hooks doctor
.venv/bin/python scripts/dogfood_agent_context.py
~~~

Expected: PASS. If ruff is unavailable, install dev extras or record the skipped lint gate explicitly; do not silently omit it.

**Step 7: Run dogfood separately when iterating**

Step 6 already includes the dogfood script in the full gate. Run this step separately when iterating on dogfood failures or when you want the dogfood summary without rerunning the full suite.

Run:

~~~bash
.venv/bin/python scripts/dogfood_agent_context.py
~~~

Expected output includes:
- pack: ok
- claude_pre_read: ok
- codex_apply_patch_pre_edit: ok
- codex_post_edit: ok
- post_edit: ok
- daemon_context: ok
- global_config_written: false

**Verification plan:**
- Primary: full pytest
- Secondary: dogfood script
- Manual dry-run: hook installer and doctor

---

## Global Install / Activation Plan After Implementation

Do this only after all tests pass and Trey explicitly approves global mutation.

### Step 1: Reinstall fork globally

~~~bash
pipx reinstall /Users/treygoff/Code/llm-tldr
tldr --version
tldr hooks doctor
~~~

Expected:
- tldr reports the fork version
- doctor sees tldr and tldr-mcp

### Step 2: Dry-run config changes

~~~bash
tldr hooks install claude --scope global --dry-run
tldr hooks install codex --scope global --dry-run
~~~

Expected:
- planned hook additions shown
- no files modified

### Step 3: Apply config changes

~~~bash
tldr hooks install claude --scope global
tldr hooks install codex --scope global
~~~

Expected:
- timestamped backups
- non-TLDR hooks preserved
- TLDR hook groups present

### Step 4: Optional MCP setup

Claude/Codex MCP config should use:

~~~json
{
  "mcpServers": {
    "tldr": {
      "command": "tldr-mcp",
      "args": ["--project", "auto"]
    }
  }
}
~~~

Codex CLI equivalent should be verified live with:

~~~bash
codex mcp add tldr -- tldr-mcp --project auto
codex mcp get tldr
~~~

If the installed codex mcp add --help output changes, use that help output at implementation time.

### Step 5: Restart clients

- Restart Claude Code because hooks load at session start.
- Restart Codex or reload config if supported by the current client.
- Verify:
  ~~~bash
  tldr hooks doctor
  codex mcp list
  ~~~

---

## Risk Register

| Risk | Consequence | Mitigation |
|---|---|---|
| Codex lacks a Read hook | Codex gets less automatic read context | Keep Codex MCP as the explicit read-context path |
| Semantic model downloads during hook | Slow/expensive session start | Never call semantic index/search from hooks unless cache exists and explicit config enables it |
| Hook spam annoys agent | More noise, less useful context | Silent no-op by default; emit only concise high-signal summaries |
| Config overwrite | Breaks existing Claude/Codex setup | Merge only, timestamped backups, dry-run default for docs |
| Daemon stale cache | Wrong context | Post-edit dirty notify and cache invalidation; graceful fallback to direct extraction |
| Multi-language false confidence | Bad context for less-supported languages | Label language support and use structure-first context; avoid claiming deep semantic precision |
| Large monorepos slow hooks | Session lag | file count threshold, background warm, .tldrignore, .claude/workspace.json support |

## Final Acceptance Criteria

- Full test suite passes: .venv/bin/python -m pytest -q
- Real daemon socket context query succeeds and returns JSON.
- tldr pack returns budgeted context and does not require semantic index.
- tldr hooks run pre-read --client claude returns valid Claude hook JSON for large code files.
- tldr hooks run post-edit --client claude reports current diagnostics schema correctly.
- tldr hooks run pre-edit --client codex returns valid Codex hook JSON with hookSpecificOutput.additionalContext from an apply_patch-shaped payload.
- tldr hooks run post-edit --client codex can extract edited file paths from apply_patch commands, tool_input.file_path, tool_response.filePath, and toolResponse.filePath.
- tldr hooks install claude/codex --dry-run shows merge-safe changes.
- tldr hooks doctor reports both client surfaces without mutating them.
- README accurately distinguishes CLI, hooks, MCP, daemon, and semantic search.
- No global config is modified by tests or dogfood scripts.
- Before global Codex activation, run the temp-config current CLI smoke and keep MCP documented as the Codex read-context fallback.
- Plan reviewer has no blocking findings.

## Plan Review Log

- Initial draft: plan_reviewer returned NO-GO. Blocking issues were task ordering around session-start, unsafe MCP project fallback, literal --project auto handling, duplicate legacy Claude TLDR hooks, bare hook executable paths, Codex fixture gaps, missing tool_response normalization, and daemon diagnostics schema drift.
- Revision 1: plan patched to add a Task 3 session-start no-op, strict MCP resolver semantics, --project auto main handling, legacy Claude hook replacement, absolute hook command paths, Codex payload tests/dogfood/acceptance gates, daemon diagnostics schema tests, cleanup-trapped daemon smoke, and lint/dogfood final gates. plan_reviewer returned GO WITH CHANGES with only copy/paste-safety nits.
- Revision 2: plan patched Codex MCP activation command, Task 10 session file wording, Task 7 session-start CLI test coverage, and dogfood duplicate wording. plan_reviewer final verdict: GO with no must-fix items.
