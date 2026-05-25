import json
from pathlib import Path

from code_briefcase.daemon.core import TLDRDaemon
from code_briefcase.mcp_server import _decode_socket_response


def test_daemon_context_response_is_json_serializable(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text(
        "def helper():\n"
        "    return 1\n\n"
        "def main():\n"
        "    return helper()\n"
    )

    daemon = TLDRDaemon(tmp_path)
    response = daemon.handle_command(
        {"cmd": "context", "entry": "main", "language": "python", "depth": 1}
    )

    json.dumps(response)
    assert response["status"] == "ok"
    assert "main" in json.dumps(response)


def test_daemon_loads_call_graph_from_cache_dir(tmp_path: Path):
    cache_dir = tmp_path / ".code-briefcase" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "call_graph.json").write_text(
        json.dumps(
            {
                "edges": [
                    {
                        "from_file": "app.py",
                        "from_func": "main",
                        "to_file": "util.py",
                        "to_func": "helper",
                    }
                ],
                "languages": ["python"],
                "timestamp": 1,
            }
        )
    )

    daemon = TLDRDaemon(tmp_path)
    daemon._ensure_call_graph_loaded()

    assert daemon.indexes["call_graph"]["edges"][0]["to_func"] == "helper"


def test_daemon_impact_uses_current_edge_shape(tmp_path: Path):
    cache_dir = tmp_path / ".code-briefcase" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "call_graph.json").write_text(
        json.dumps(
            {
                "edges": [
                    {
                        "from_file": "app.py",
                        "from_func": "main",
                        "to_file": "util.py",
                        "to_func": "helper",
                    }
                ],
                "languages": ["python"],
                "timestamp": 1,
            }
        )
    )

    daemon = TLDRDaemon(tmp_path)
    response = daemon.handle_command({"cmd": "impact", "func": "helper"})

    assert response["status"] == "ok"
    payload = json.dumps(response)
    assert "main" in payload
    assert "helper" in payload


def test_mcp_decode_socket_response_does_not_duplicate_chunks():
    payload = {"status": "ok", "result": "abc"}
    raw = json.dumps(payload).encode()
    midpoint = len(raw) // 2

    assert _decode_socket_response([raw[:midpoint], raw[midpoint:]]) == payload


def test_daemon_diagnostics_uses_current_schema(tmp_path: Path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("def main():\n    return 1\n")

    def fake_get_diagnostics(path, language=None, include_lint=True):
        return {
            "file": path,
            "language": "python",
            "tools": ["pyright"],
            "diagnostics": [],
            "error_count": 0,
            "warning_count": 0,
        }

    monkeypatch.setattr("code_briefcase.diagnostics.get_diagnostics", fake_get_diagnostics)

    daemon = TLDRDaemon(tmp_path)
    response = daemon.handle_command(
        {"cmd": "diagnostics", "file": str(source), "language": "python"}
    )

    assert response["status"] == "ok"
    assert "diagnostics" in response
    assert "error_count" in response
    assert "warning_count" in response
    assert "summary" not in response
