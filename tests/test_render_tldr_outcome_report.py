from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_render_report_writes_markdown_and_html_without_secrets(tmp_path):
    payload = {
        "window": {"start": "2026-05-20T00:00:00+00:00", "end": "2026-05-21T00:00:00+00:00"},
        "rollups": [
            {
                "session_id": "s1",
                "client": "codex",
                "project_hash": "abc12345",
                "verdict": "proxy-only",
                "tldr_hooks": 2,
                "tldr_errors": 0,
            }
        ],
    }
    input_path = tmp_path / "rollups.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    md_path = tmp_path / "report.md"
    html_path = tmp_path / "report.html"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "render_tldr_outcome_report.py"),
            "--input",
            str(input_path),
            "--markdown-out",
            str(md_path),
            "--html-out",
            str(html_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    md = md_path.read_text(encoding="utf-8")
    html = html_path.read_text(encoding="utf-8")
    assert md_path.exists() and html_path.exists()
    assert "SECRET_FIXTURE_COMMAND" not in md
    assert "SECRET_FIXTURE_USER_TEXT" not in html
    assert "proxy-only" in md


def test_render_report_includes_hook_duration_summary():
    from scripts.render_tldr_outcome_report import render_markdown  # noqa: E402

    payload = {
        "window": {"start": "2026-05-20T00:00:00+00:00", "end": "2026-05-21T00:00:00+00:00"},
        "rollups": [
            {
                "session_id": "s1",
                "client": "codex",
                "project_hash": "abc12345",
                "verdict": "proxy-only",
                "tldr_hooks": 2,
                "tldr_errors": 0,
                "hook_duration_p50": 12,
                "hook_duration_p95": 18,
            }
        ],
    }

    markdown = render_markdown(payload)

    assert "Hook duration p50/p95 (ms): 12.0/18.0" in markdown


def test_render_html_escapes_table_and_verdict_values(tmp_path):
    from scripts.render_tldr_outcome_report import render_html, render_markdown  # noqa: E402

    payload = {
        "window": {"start": "2026-05-20T00:00:00+00:00", "end": "2026-05-21T00:00:00+00:00"},
        "rollups": [
            {
                "session_id": "<script>alert(1)</script>",
                "client": "codex&co",
                "project_hash": "abc12345",
                "verdict": "proxy<script>only",
                "tldr_hooks": 1,
                "tldr_errors": 0,
            }
        ],
    }
    markdown = render_markdown(payload)
    html = render_html(payload, markdown)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "codex&amp;co" in html
    assert "proxy&lt;script&gt;only" in html


def test_render_html_escapes_markdown_pre_block():
    from scripts.render_tldr_outcome_report import render_html  # noqa: E402

    payload = {"rollups": []}
    markdown = "A & B <tag> C > D"
    html = render_html(payload, markdown)

    assert "<pre>A &amp; B &lt;tag&gt; C &gt; D</pre>" in html
    assert "<tag>" not in html.split("<pre>", 1)[1]


def test_render_report_includes_and_escapes_local_rich_evidence():
    from scripts.render_tldr_outcome_report import render_html, render_markdown  # noqa: E402

    payload = {
        "rollups": [
            {
                "session_id": "s1",
                "client": "codex",
                "project_hash": "abc12345",
                "verdict": "helpful",
                "tldr_hooks": 1,
                "tldr_errors": 0,
                "local_evidence": [
                    {
                        "kind": "tool_call",
                        "command": "rg -n '<script>' src/app.py",
                    }
                ],
            }
        ]
    }

    markdown = render_markdown(payload)
    html = render_html(payload, markdown)

    assert "Local-rich evidence" in markdown
    assert "rg -n '<script>' src/app.py" in markdown
    assert "&lt;script&gt;" in html
    assert "Sensitive local-only evidence is present" in html
