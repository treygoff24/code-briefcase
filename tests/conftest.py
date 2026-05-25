from pathlib import Path

import pytest

# Env vars that bleed into tests from a developer's shell. We scrub them at
# session start so telemetry / watcher tests assert against the canonical
# defaults instead of whatever happens to be set locally.
_LEAKY_ENV_VARS = (
    "CODE_BRIEFCASE_TELEMETRY",
    "CODE_BRIEFCASE_TELEMETRY_MODE",
    "CODE_BRIEFCASE_TELEMETRY_PATH",
    "CODE_BRIEFCASE_TELEMETRY_REDACT_PATHS",
    "CODE_BRIEFCASE_TELEMETRY_LOCAL_STRING_LIMIT",
    "CODE_BRIEFCASE_WATCH_DIAGNOSTICS",
    "CODE_BRIEFCASE_WATCH_DIAGNOSTICS_BUDGET_MS",
    "TLDR_WATCH_DIAGNOSTICS",
    "CODE_BRIEFCASE_DEBUG",
)


@pytest.fixture(autouse=True)
def _scrub_leaky_env(monkeypatch):
    for name in _LEAKY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def make_executable():
    def _make(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(path.stat().st_mode | 0o111)
        return path

    return _make
