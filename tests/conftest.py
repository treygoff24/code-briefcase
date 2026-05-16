from pathlib import Path

import pytest


@pytest.fixture
def make_executable():
    def _make(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(path.stat().st_mode | 0o111)
        return path

    return _make
