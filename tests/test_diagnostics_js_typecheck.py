from code_briefcase import diagnostics as diag


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
echo "{source}(3,7): error TS2322: Type 'number' is not assignable to type 'string'."
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


def test_single_file_typescript_uses_project_config_and_filters_output(
    tmp_path, monkeypatch, make_executable
):
    monkeypatch.setenv("CODE_BRIEFCASE_TSC_CACHE_ROOT", str(tmp_path / "tldr-cache"))
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source = tmp_path / "src" / "sample.ts"
    other = tmp_path / "src" / "other.ts"
    source.parent.mkdir()
    source.write_text("import { value } from '@/value';\nconst answer: string = 42;\n")
    other.write_text("const other: string = 42;\n")
    tsconfig = tmp_path / "tsconfig.json"
    tsconfig.write_text(
        '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["src/*"]}}}\n'
    )
    args_file = tmp_path / "tsc-args.txt"
    cwd_file = tmp_path / "tsc-cwd.txt"

    make_executable(
        tmp_path / "node_modules" / ".bin" / "tsc",
        f"""#!/bin/sh
pwd > {cwd_file}
printf '%s\n' "$@" > {args_file}
cat <<'OUT'
src/sample.ts(2,7): error TS2322: Type 'number' is not assignable to type 'string'.
src/other.ts(1,7): error TS2322: Type 'number' is not assignable to type 'string'.
OUT
exit 2
""",
    )

    result = diag.get_diagnostics(
        str(source),
        language="typescript",
        include_lint=False,
    )
    args = args_file.read_text().splitlines()

    assert result["tools"] == ["tsc"]
    assert result["error_count"] == 1
    assert result["diagnostics"][0]["file"] == str(source.resolve())
    assert result["diagnostics"][0]["line"] == 2
    assert cwd_file.read_text().strip() == str(tmp_path)
    assert "--project" in args
    assert args[args.index("--project") + 1].endswith("tsconfig.json")
    assert args[args.index("--project") + 1] != str(tsconfig)
    assert str(source) not in args


def test_single_file_javascript_uses_project_config_without_direct_file_arg(
    tmp_path, monkeypatch, make_executable
):
    monkeypatch.setenv("CODE_BRIEFCASE_TSC_CACHE_ROOT", str(tmp_path / "tldr-cache"))
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source = tmp_path / "src" / "type-error.js"
    other = tmp_path / "src" / "other.js"
    source.parent.mkdir()
    source.write_text("// @ts-check\ntakesString(42);\n")
    other.write_text("// @ts-check\ntakesString(42);\n")
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"allowJs":true,"checkJs":true}}\n'
    )
    args_file = tmp_path / "tsc-args.txt"

    make_executable(
        tmp_path / "node_modules" / ".bin" / "tsc",
        f"""#!/bin/sh
printf '%s\n' "$@" > {args_file}
cat <<'OUT'
src/type-error.js(2,13): error TS2345: Argument of type 'number' is not assignable to parameter of type 'string'.
src/other.js(2,13): error TS2345: Argument of type 'number' is not assignable to parameter of type 'string'.
OUT
exit 2
""",
    )

    result = diag.get_diagnostics(
        str(source),
        language="javascript",
        include_lint=False,
    )
    args = args_file.read_text().splitlines()

    assert result["tools"] == ["tsc"]
    assert result["error_count"] == 1
    assert result["diagnostics"][0]["file"] == str(source.resolve())
    assert result["diagnostics"][0]["rule"] == "TS2345"
    assert "--project" in args
    assert str(source) not in args
