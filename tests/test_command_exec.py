from typing import Any
from code_briefcase.command_exec import expand_shebang_command


def test_empty_command() -> None:
    assert expand_shebang_command([]) == []


def test_not_a_file() -> None:
    assert expand_shebang_command(["some-nonexistent-command", "arg1"]) == [
        "some-nonexistent-command",
        "arg1",
    ]


def test_file_no_shebang(tmp_path: Any) -> None:
    script = tmp_path / "script.py"
    script.write_text("print('hello')")
    assert expand_shebang_command([str(script), "arg1"]) == [str(script), "arg1"]


def test_simple_shebang(tmp_path: Any) -> None:
    script = tmp_path / "script.py"
    script.write_text("#!/bin/bash\necho test")
    assert expand_shebang_command([str(script), "arg1"]) == [
        "/bin/bash",
        str(script),
        "arg1",
    ]


def test_env_shebang(tmp_path: Any) -> None:
    script = tmp_path / "script.py"
    script.write_text("#!/usr/bin/env python3\nprint(1)")
    assert expand_shebang_command([str(script), "arg1"]) == [
        "python3",
        str(script),
        "arg1",
    ]


def test_env_shebang_with_options(tmp_path: Any) -> None:
    script = tmp_path / "script.py"
    script.write_text("#!/usr/bin/env -i python3\nprint(1)")
    # Should strip the -i option
    assert expand_shebang_command([str(script), "arg1"]) == [
        "python3",
        str(script),
        "arg1",
    ]


def test_env_shebang_with_env_vars(tmp_path: Any) -> None:
    script = tmp_path / "script.py"
    script.write_text("#!/usr/bin/env FOO=bar python3\nprint(1)")
    # Should strip the environment variable
    assert expand_shebang_command([str(script), "arg1"]) == [
        "python3",
        str(script),
        "arg1",
    ]


def test_env_shebang_dash_s(tmp_path: Any) -> None:
    script = tmp_path / "script.js"
    script.write_text("#!/usr/bin/env -S node --loader ts-node/esm\nconsole.log(1)")
    assert expand_shebang_command([str(script), "arg1"]) == [
        "node",
        "--loader",
        "ts-node/esm",
        str(script),
        "arg1",
    ]


def test_env_shebang_dash_s_with_env_vars(tmp_path: Any) -> None:
    script = tmp_path / "script.js"
    script.write_text("#!/usr/bin/env -S VAR=val node\nconsole.log(1)")
    assert expand_shebang_command([str(script), "arg1"]) == [
        "node",
        str(script),
        "arg1",
    ]


def test_env_shebang_dash_s_with_quotes_and_spaces(tmp_path: Any) -> None:
    script = tmp_path / "script.js"
    script.write_text("#!/usr/bin/env -S node -e \"console.log('hello', 'world')\"\n")
    assert expand_shebang_command([str(script), "arg1"]) == [
        "node",
        "-e",
        "console.log('hello', 'world')",
        str(script),
        "arg1",
    ]
