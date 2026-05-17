import subprocess
import sys


def test_pack_help_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "tldr.cli", "pack", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "context pack" in result.stdout.lower()


def test_hooks_help_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "tldr.cli", "hooks", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "hooks" in result.stdout.lower()


def test_tldr_mcp_help_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "tldr.mcp_server", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "--project" in result.stdout
