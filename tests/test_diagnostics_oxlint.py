import json
from pathlib import Path

from tldr import diagnostics as diag


def test_parse_oxlint_fixture():
    fixture = Path("tests/fixtures/oxlint_sample.json")
    diagnostics = diag._parse_oxlint_output(fixture.read_text())

    assert diagnostics
    assert diagnostics[0]["source"] == "oxlint"
    assert diagnostics[0]["line"] == 2
    assert diagnostics[0]["column"] == 3
    assert diagnostics[0]["rule"] == "eslint(no-debugger)"
    assert "debugger" in diagnostics[0]["message"]


def test_get_diagnostics_runs_local_oxlint(tmp_path, monkeypatch, make_executable):
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source = tmp_path / "src" / "sample.ts"
    source.parent.mkdir()
    source.write_text("function main() {\n  debugger;\n}\n")

    payload = {
        "diagnostics": [
            {
                "message": "debugger statement is not allowed",
                "code": "eslint(no-debugger)",
                "severity": "warning",
                "filename": str(source),
                "labels": [{"span": {"line": 2, "column": 3}}],
            }
        ]
    }
    script = f"""#!/bin/sh
cat <<'JSON'
{json.dumps(payload)}
JSON
exit 1
"""
    make_executable(tmp_path / "node_modules" / ".bin" / "oxlint", script)

    result = diag.get_diagnostics(
        str(source),
        language="typescript",
        include_lint=True,
    )

    assert "oxlint" in result["tools"]
    assert result["warning_count"] == 1
    assert result["diagnostics"][0]["rule"] == "eslint(no-debugger)"
