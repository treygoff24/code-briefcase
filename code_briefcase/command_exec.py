from __future__ import annotations

import shlex
from pathlib import Path


def expand_shebang_command(command: list[str]) -> list[str]:
    """Return a subprocess command that explicitly invokes script interpreters.

    Some agent runtimes can hang when executing local shebang shims directly
    (for example node_modules/.bin shell or Node.js wrappers). Running the
    declared interpreter explicitly keeps validation and diagnostics deterministic
    while leaving native binaries untouched.
    """
    if not command:
        return command

    executable = Path(command[0]).expanduser()
    try:
        if not executable.is_file():
            return command
        with executable.open("rb") as handle:
            first_line = handle.readline(256).decode("utf-8", "ignore")
    except OSError:
        return command

    if not first_line.startswith("#!"):
        return command

    try:
        shebang = shlex.split(first_line[2:].strip())
    except ValueError:
        return command
    if not shebang:
        return command

    interpreter = shebang[0]
    interpreter_args = shebang[1:]
    if Path(interpreter).name == "env":
        interpreter_args = _parse_env_shebang_args(interpreter_args)
        if not interpreter_args:
            return command
        return [*interpreter_args, str(executable), *command[1:]]

    return [interpreter, *interpreter_args, str(executable), *command[1:]]


def _parse_env_shebang_args(args: list[str]) -> list[str]:
    if args[:1] == ["-S"]:
        try:
            parsed = shlex.split(shlex.join(args[1:]))
        except ValueError:
            parsed = []
    else:
        parsed = list(args)

    remaining = parsed
    while remaining:
        current = remaining[0]
        if current == "--":
            remaining.pop(0)
            break
        if current.startswith("-"):
            remaining.pop(0)
            continue
        if "=" in current and not current.startswith(("/", ".")):
            remaining.pop(0)
            continue
        break
    return remaining
