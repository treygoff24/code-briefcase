from tldr import diagnostics as diag


def test_javascript_diagnostics_runs_tsc_with_allow_js(tmp_path, monkeypatch, make_executable):
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source = tmp_path / "src" / "type_error.js"
    source.parent.mkdir()
    source.write_text(
        "// @ts-check\n"
        "/** @type {string} */\n"
        "const answer = 42;\n"
    )
    args_file = tmp_path / "tsc-args.txt"

    make_executable(
        tmp_path / "node_modules" / ".bin" / "tsc",
        f"""#!/bin/sh
printf '%s\n' "$@" > {args_file}
echo "$4(3,7): error TS2322: Type 'number' is not assignable to type 'string'."
exit 2
""",
    )

    result = diag.get_diagnostics(
        str(source), language="javascript", include_lint=False
    )

    assert "tsc" in result["tools"]
    assert result["error_count"] == 1
    assert result["diagnostics"][0]["rule"] == "TS2322"
    assert "--allowJs" in args_file.read_text().splitlines()
