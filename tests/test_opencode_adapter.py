import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from code_briefcase.hooks.opencode_adapter import generate_opencode_adapter


def _node_or_skip() -> str:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js binary not available on PATH for OpenCode adapter tests")
    return node_bin


def _write_fake_tldr(tmp_path: Path) -> Path:
    fake = tmp_path / "fake-code_briefcase.mjs"
    fake.write_text(
        """
const event = process.argv[process.argv.indexOf("run") + 1]
let stdin = ""
for await (const chunk of process.stdin) {
  stdin += chunk
}
const payload = stdin ? JSON.parse(stdin) : {}
if (process.env.CODE_BRIEFCASE_CAPTURE) {
  const fs = await import("node:fs")
  fs.appendFileSync(process.env.CODE_BRIEFCASE_CAPTURE, JSON.stringify({ event, payload }) + "\\n")
}
switch (process.env.CODE_BRIEFCASE_FAKE_MODE) {
  case "empty":
    process.exit(0)
  case "invalid":
    process.stdout.write("{not-json")
    process.exit(0)
  case "nonzero":
    process.stderr.write("boom")
    process.exit(7)
  case "timeout":
    await new Promise((resolve) => setTimeout(resolve, 3000))
    process.exit(0)
  case "deny":
    console.log(JSON.stringify({
      hookSpecificOutput: {
        permissionDecision: "deny",
        permissionDecisionReason: "dangerous command"
      }
    }))
    process.exit(0)
  case "context":
    console.log(JSON.stringify({
      hookSpecificOutput: {
        additionalContext: "Code Briefcase context"
      }
    }))
    process.exit(0)
  case "updated":
    console.log(JSON.stringify({
      hookSpecificOutput: {
        updatedInput: { command: "echo sanitized" }
      }
    }))
    process.exit(0)
  default:
    console.log("{}")
    process.exit(0)
}
""".strip()
    )
    return fake


def _run_node(tmp_path: Path, source: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    node_bin = _node_or_skip()
    runner = tmp_path / "runner.mjs"
    runner.write_text(source)
    return subprocess.run(
        [node_bin, str(runner.name)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _write_adapter(tmp_path: Path, *, enable_tool_guard: bool = True, enable_compact_context: bool = True) -> Path:
    node_bin = _node_or_skip()
    fake = _write_fake_tldr(tmp_path)
    adapter = tmp_path / "code-briefcase-hooks.mjs"
    adapter.write_text(
        generate_opencode_adapter(
            [node_bin, str(fake)],
            enable_tool_guard=enable_tool_guard,
            enable_compact_context=enable_compact_context,
        )
    )
    return adapter


def test_generate_opencode_adapter_default_callbacks():
    tldr_path = "/usr/local/bin/tldr"
    js = generate_opencode_adapter(tldr_path)

    # 1. Default callbacks must be present
    assert '"session.created"' in js
    assert '"tool.execute.before"' in js
    assert '"tool.execute.after"' in js
    assert '"file.edited"' in js

    # 2. Opt-in callbacks must be absent by default
    assert '"permission.asked"' not in js
    assert '"experimental.session.compacting"' not in js

    # 3. Timeout default is 1500ms
    assert "CODE_BRIEFCASE_TIMEOUT_MS = 1500" in js
    assert 'child.stdin.on("error"' in js
    assert 'child.on("close"' in js

    # 4. Resolved absolute Code Briefcase command path is present
    assert '"/usr/local/bin/tldr"' in js

    # 5. Dependency-free: no external dependency imports
    # Only standard Node.js imports like spawn from node:child_process
    for line in js.splitlines():
        if line.strip().startswith("import"):
            assert "node:child_process" in line or "node:" in line


def test_generate_opencode_adapter_opt_in_callbacks():
    tldr_path = "/usr/local/bin/tldr"

    # Opt-in tool guard
    js_tool = generate_opencode_adapter(tldr_path, enable_tool_guard=True)
    assert '"permission.asked"' in js_tool
    assert '"experimental.session.compacting"' not in js_tool

    # Opt-in compact context
    js_compact = generate_opencode_adapter(tldr_path, enable_compact_context=True)
    assert '"permission.asked"' not in js_compact
    assert '"experimental.session.compacting"' in js_compact

    # Both opt-ins
    js_both = generate_opencode_adapter(tldr_path, enable_tool_guard=True, enable_compact_context=True)
    assert '"permission.asked"' in js_both
    assert '"experimental.session.compacting"' in js_both


def test_generate_opencode_adapter_list_tldr_command():
    tldr_cmd = ["/path/to/python", "-m", "code_briefcase.cli"]
    js = generate_opencode_adapter(tldr_cmd)
    
    assert "/path/to/python" in js
    assert "-m" in js
    assert "code_briefcase.cli" in js
    assert '["code-briefcase"]' not in js


def test_node_smoke_parsing(tmp_path):
    _write_adapter(tmp_path)

    test_js = """
    import("./code-briefcase-hooks.mjs").then((m) => {
        if (typeof m.TLDRHooks !== "function") {
            process.exit(2);
        }
        return m.TLDRHooks({ directory: "/foo/bar", project: { id: "test-sess" } });
    }).then((hooks) => {
        const expected = [
            "session.created",
            "tool.execute.before",
            "tool.execute.after",
            "file.edited",
            "permission.asked",
            "experimental.session.compacting"
        ];
        for (const name of expected) {
            if (typeof hooks[name] !== "function") {
                console.error("Missing callback:", name);
                process.exit(3);
            }
        }
        process.exit(0);
    }).catch((err) => {
        console.error(err);
        process.exit(1);
    });
    """
    runner_path = tmp_path / "run-test.mjs"
    runner_path.write_text(test_js)

    result = subprocess.run([_node_or_skip(), "run-test.mjs"], cwd=tmp_path, capture_output=True, text=True)

    assert result.returncode == 0, f"Node.js parsing failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"


def test_node_callback_event_normalization(tmp_path):
    capture = tmp_path / "capture.jsonl"
    _write_adapter(tmp_path, enable_tool_guard=False, enable_compact_context=False)
    result = _run_node(
        tmp_path,
        """
import { TLDRHooks } from "./code-briefcase-hooks.mjs"
const hooks = await TLDRHooks({ directory: "/repo", project: { id: "session-1" } })
await hooks["session.created"]({}, {})
await hooks["tool.execute.before"]({ tool: "read" }, { args: { filePath: "README.md" } })
await hooks["tool.execute.before"]({ tool: "edit" }, { args: { filePath: "README.md" } })
await hooks["tool.execute.after"]({ tool: "edit" }, { args: { filePath: "README.md" }, result: { ok: true } })
await hooks["file.edited"]({ filePath: "README.md" }, {})
""",
        env={"CODE_BRIEFCASE_CAPTURE": str(capture), "CODE_BRIEFCASE_FAKE_MODE": "default"},
    )

    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [line["event"] for line in lines] == [
        "session-start",
        "pre-read",
        "pre-edit",
        "post-edit",
        "post-edit",
    ]
    assert all(line["payload"]["cwd"] == "/repo" for line in lines)
    assert lines[1]["payload"]["tool_name"] == "read"
    assert lines[2]["payload"]["tool_name"] == "edit"
    assert lines[4]["payload"]["tool_name"] == "Edit"
    assert lines[4]["payload"]["tool_input"]["file_path"] == "README.md"


def test_node_callbacks_treat_empty_parse_failure_subprocess_failure_and_timeout_as_noop(tmp_path):
    _write_adapter(tmp_path, enable_tool_guard=False, enable_compact_context=False)
    result = _run_node(
        tmp_path,
        """
import { TLDRHooks } from "./code-briefcase-hooks.mjs"
const hooks = await TLDRHooks({ directory: "/repo", project: { id: "session-1" } })
for (const mode of ["empty", "invalid", "nonzero", "timeout"]) {
  process.env.CODE_BRIEFCASE_FAKE_MODE = mode
  await hooks["tool.execute.before"]({ tool: "bash" }, { args: { command: "echo ok" } })
}
""",
    )

    assert result.returncode == 0, result.stderr


def test_node_tool_before_applies_updated_input(tmp_path):
    _write_adapter(tmp_path, enable_tool_guard=False, enable_compact_context=False)
    result = _run_node(
        tmp_path,
        """
import { TLDRHooks } from "./code-briefcase-hooks.mjs"
const hooks = await TLDRHooks({ directory: "/repo", project: { id: "session-1" } })
const output = { args: { command: "rm -rf /" } }
process.env.CODE_BRIEFCASE_FAKE_MODE = "updated"
await hooks["tool.execute.before"]({ tool: "bash" }, output)
if (output.args.command !== "echo sanitized") {
  throw new Error(`updatedInput was not applied: ${JSON.stringify(output.args)}`)
}
""",
    )

    assert result.returncode == 0, result.stderr


def test_node_permission_and_tool_guards_throw_on_deny(tmp_path):
    _write_adapter(tmp_path, enable_tool_guard=True, enable_compact_context=False)
    result = _run_node(
        tmp_path,
        """
import { TLDRHooks } from "./code-briefcase-hooks.mjs"
const hooks = await TLDRHooks({ directory: "/repo", project: { id: "session-1" } })
process.env.CODE_BRIEFCASE_FAKE_MODE = "deny"
for (const name of ["tool.execute.before", "permission.asked"]) {
  let threw = false
  try {
    await hooks[name]({ tool: "bash" }, { args: { command: "rm -rf /" } })
  } catch (err) {
    threw = err.message.includes("dangerous command")
  }
  if (!threw) {
    throw new Error(`${name} did not throw the Code Briefcase denial`)
  }
}
""",
    )

    assert result.returncode == 0, result.stderr


def test_node_compaction_callback_pushes_context(tmp_path):
    _write_adapter(tmp_path, enable_tool_guard=False, enable_compact_context=True)
    result = _run_node(
        tmp_path,
        """
import { TLDRHooks } from "./code-briefcase-hooks.mjs"
const hooks = await TLDRHooks({ directory: "/repo", project: { id: "session-1" } })
const output = { context: [] }
process.env.CODE_BRIEFCASE_FAKE_MODE = "context"
await hooks["experimental.session.compacting"]({}, output)
if (output.context[0] !== "Code Briefcase context") {
  throw new Error(`context was not appended: ${JSON.stringify(output.context)}`)
}
""",
    )

    assert result.returncode == 0, result.stderr


def test_node_session_created_appends_context_when_output_supports_it(tmp_path):
    _write_adapter(tmp_path, enable_tool_guard=False, enable_compact_context=False)
    result = _run_node(
        tmp_path,
        """
import { TLDRHooks } from "./code-briefcase-hooks.mjs"
const hooks = await TLDRHooks({ directory: "/repo", project: { id: "session-1" } })
const output = { context: [] }
process.env.CODE_BRIEFCASE_FAKE_MODE = "context"
await hooks["session.created"]({}, output)
if (output.context[0] !== "Code Briefcase context") {
  throw new Error(`session context was not appended: ${JSON.stringify(output.context)}`)
}
""",
    )

    assert result.returncode == 0, result.stderr
