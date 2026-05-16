import json

from tldr import diagnostics as diag


def test_project_typescript_runs_oxlint_and_oxfmt(tmp_path, monkeypatch, make_executable):
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source = tmp_path / "src" / "sample.ts"
    source.parent.mkdir()
    source.write_text("function main() {\n  debugger;\n}\n")
    args_file = tmp_path / "oxlint-args.txt"
    fmt_args_file = tmp_path / "oxfmt-args.txt"

    oxlint_payload = {
        "diagnostics": [
            {
                "message": "debugger statement is not allowed",
                "code": "eslint(no-debugger)",
                "severity": "warning",
                "filename": "src/sample.ts",
                "labels": [{"span": {"line": 2, "column": 3}}],
            }
        ]
    }

    make_executable(
        tmp_path / "node_modules" / ".bin" / "tsc",
        "#!/bin/sh\nexit 0\n",
    )
    make_executable(
        tmp_path / "node_modules" / ".bin" / "oxlint",
        f"""#!/bin/sh
printf '%s\n' "$@" > {args_file}
cat <<'JSON'
{json.dumps(oxlint_payload)}
JSON
exit 1
""",
    )
    make_executable(
        tmp_path / "node_modules" / ".bin" / "oxfmt",
        f"""#!/bin/sh
printf '%s\n' "$@" > {fmt_args_file}
exit 1
""",
    )

    result = diag.get_project_diagnostics(str(tmp_path), language="typescript")

    assert result["tools"] == ["tsc", "oxlint", "oxfmt"]
    assert {item["source"] for item in result["diagnostics"]} == {
        "oxlint",
        "oxfmt",
    }
    oxlint_args = args_file.read_text().splitlines()
    assert "." in oxlint_args
    assert "--ignore-pattern=node_modules/**" in oxlint_args
    assert "--no-error-on-unmatched-pattern" in oxlint_args
    assert fmt_args_file.read_text().splitlines() == [
        "--check",
        ".",
        "!**/*.d.ts",
    ]


def test_project_javascript_runs_tsc_with_allow_js(tmp_path, monkeypatch, make_executable):
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source = tmp_path / "src" / "sample.js"
    source.parent.mkdir()
    source.write_text("// @ts-check\nconst answer = 42;\n")
    tsc_args_file = tmp_path / "tsc-args.txt"

    make_executable(
        tmp_path / "node_modules" / ".bin" / "tsc",
        f"""#!/bin/sh
printf '%s\n' "$@" > {tsc_args_file}
exit 0
""",
    )
    make_executable(
        tmp_path / "node_modules" / ".bin" / "oxlint",
        """#!/bin/sh
echo '{"diagnostics":[]}'
exit 0
""",
    )
    make_executable(
        tmp_path / "node_modules" / ".bin" / "oxfmt",
        "#!/bin/sh\nexit 0\n",
    )

    result = diag.get_project_diagnostics(str(tmp_path), language="javascript")

    assert result["tools"] == ["tsc", "oxlint", "oxfmt"]
    assert "--allowJs" in tsc_args_file.read_text().splitlines()


def test_project_oxfmt_skips_all_declaration_files(tmp_path, monkeypatch, make_executable):
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source = tmp_path / "types.d.ts"
    source.write_text("declare const answer:{value:number}\n")

    make_executable(
        tmp_path / "node_modules" / ".bin" / "oxfmt",
        """#!/bin/sh
echo 'Expected at least one target file.'
exit 2
""",
    )

    assert diag._run_project_oxfmt(tmp_path) == []
    result = diag.get_project_diagnostics(str(tmp_path), language="typescript")
    assert "oxfmt" not in result["tools"]
    assert not any(d["source"] == "oxfmt" for d in result["diagnostics"])
