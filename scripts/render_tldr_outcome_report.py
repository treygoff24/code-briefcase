#!/usr/bin/env python3
"""Render Markdown and HTML reports from backfill outcome JSON."""

from __future__ import annotations

import argparse
import html
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def render_markdown(payload: dict[str, Any]) -> str:
    rollups = payload.get("rollups") or []
    verdicts = Counter(item.get("verdict") for item in rollups)
    match_conf = Counter(item.get("match_confidence") for item in rollups)
    attr_conf = Counter(item.get("attribution_confidence") for item in rollups)
    causal_conf = Counter(item.get("causal_confidence") for item in rollups)
    lines = [
        "# Code Briefcase outcome report",
        "",
        f"- Window: `{payload.get('window', {}).get('start')}` → `{payload.get('window', {}).get('end')}`",
        f"- Sessions: {len(rollups)}",
        "",
        "## Executive verdict",
        "",
        f"- Verdict distribution: {dict(verdicts)}",
        f"- Match confidence: {dict(match_conf)}",
        f"- Attribution confidence: {dict(attr_conf)}",
        f"- Causal confidence: {dict(causal_conf)}",
        "",
        "## Reliability",
        "",
    ]
    duration_p50s = [
        float(item.get("hook_duration_p50") or 0)
        for item in rollups
        if int(item.get("tldr_hooks") or 0) > 0
    ]
    duration_p95s = [
        float(item.get("hook_duration_p95") or 0)
        for item in rollups
        if int(item.get("tldr_hooks") or 0) > 0
    ]
    if duration_p50s:
        lines.append(
            f"- Hook duration p50/p95 (ms): {_median(duration_p50s)}/{_median(duration_p95s)}"
        )
    status_counts: Counter[str] = Counter()
    for item in rollups:
        status_counts["hooks"] += int(item.get("tldr_hooks") or 0)
        status_counts["errors"] += int(item.get("tldr_errors") or 0)
        status_counts["skips"] += int(item.get("tldr_skips") or 0)
    lines.append(f"- Hook totals: {dict(status_counts)}")
    skip_reasons: Counter[str] = Counter()
    noop_reasons: Counter[str] = Counter()
    clean_checks = 0
    for item in rollups:
        for reason, count in (item.get("tldr_skip_reason_counts") or {}).items():
            skip_reasons[reason] += int(count)
        for reason, count in (item.get("tldr_noop_reason_counts") or {}).items():
            noop_reasons[reason] += int(count)
        clean_checks += int(item.get("tldr_clean_checks") or 0)
    lines.extend(
        [
            "",
            "## Skip / clean-check reasons",
            "",
            f"- Skips by reason: {dict(skip_reasons)}",
            f"- Noops by reason: {dict(noop_reasons)}",
            f"- Clean post-edit checks: {clean_checks}",
            "",
            "## Recommendation lifecycle",
            "",
        ]
    )
    lines.append(
        f"- Candidates total/surfaced/later-used (sums): "
        f"{sum(int(r.get('candidate_files_total') or 0) for r in rollups)}/"
        f"{sum(int(r.get('candidate_files_surfaced') or 0) for r in rollups)}/"
        f"{sum(int(r.get('candidate_files_later_used') or 0) for r in rollups)}"
    )
    lines.extend(["", "## Behavior", ""])
    lines.append(
        f"- Explore before first edit (median): "
        f"{_median([float(r.get('explore_before_first_edit') or 0) for r in rollups])}"
    )
    lines.append(
        f"- Repeated file reads (median): "
        f"{_median([float(r.get('repeated_file_reads') or 0) for r in rollups])}"
    )
    lines.append(
        f"- Failed tool outputs (sum): {sum(int(r.get('failed_tool_outputs') or 0) for r in rollups)}"
    )
    lines.extend(["", "## Cost / overhead", ""])
    lines.append(
        f"- Injected bytes p50/p95: "
        f"{_median([float(r.get('injected_bytes_p50') or 0) for r in rollups])}/"
        f"{_median([float(r.get('injected_bytes_p95') or 0) for r in rollups])}"
    )
    lines.extend(["", "## Top sessions", ""])
    for item in sorted(rollups, key=lambda r: int(r.get("tldr_errors") or 0), reverse=True)[:5]:
        lines.append(
            f"- `{item.get('session_id')}` ({item.get('client')}, {item.get('project_hash')}): "
            f"verdict={item.get('verdict')}, hooks={item.get('tldr_hooks')}, errors={item.get('tldr_errors')}"
        )
    lines.append("")
    evidence = _local_evidence_samples(rollups)
    if evidence:
        lines.extend(
            [
                "## Local-rich evidence",
                "",
                "> Sensitive local-only evidence is present. Do not commit or share this report.",
                "",
                f"- Sessions with local evidence: {sum(1 for r in rollups if r.get('local_evidence'))}",
                f"- Evidence samples retained in report: {sum(len(r.get('local_evidence') or []) for r in rollups)}",
                "",
            ]
        )
        for sample in evidence[:10]:
            lines.append(
                f"- `{sample.get('session_id')}` {sample.get('kind')}: "
                f"{sample.get('summary')}"
            )
        lines.append("")
    return "\n".join(lines)


def _html_text(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _local_evidence_samples(rollups: list[dict[str, Any]]) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for rollup in rollups:
        session_id = str(rollup.get("session_id") or "")
        for evidence in rollup.get("local_evidence") or []:
            if not isinstance(evidence, dict):
                continue
            kind = str(evidence.get("kind") or evidence.get("event") or "evidence")
            command = evidence.get("command")
            tool_name = evidence.get("tool_name")
            status = evidence.get("status")
            if command:
                summary = str(command)
            elif tool_name:
                summary = f"tool={tool_name}"
            elif status:
                summary = f"status={status}"
            else:
                summary = json.dumps(evidence, sort_keys=True)[:240]
            if len(summary) > 240:
                summary = summary[:237].rstrip() + "..."
            samples.append({"session_id": session_id, "kind": kind, "summary": summary})
    return samples


def render_html(payload: dict[str, Any], markdown: str) -> str:
    rollups = payload.get("rollups") or []
    verdicts = Counter(item.get("verdict") for item in rollups)
    rows = "".join(
        f"<tr><td>{_html_text(item.get('session_id'))}</td><td>{_html_text(item.get('client'))}</td>"
        f"<td>{_html_text(item.get('verdict'))}</td><td>{_html_text(item.get('tldr_hooks'))}</td>"
        f"<td>{_html_text(item.get('tldr_errors'))}</td></tr>"
        for item in rollups[:20]
    )
    evidence = _local_evidence_samples(rollups)
    evidence_html = ""
    if evidence:
        evidence_rows = "".join(
            f"<tr><td>{_html_text(item.get('session_id'))}</td>"
            f"<td>{_html_text(item.get('kind'))}</td>"
            f"<td><code>{_html_text(item.get('summary'))}</code></td></tr>"
            for item in evidence[:20]
        )
        evidence_html = f"""
  <h2>Local-rich evidence</h2>
  <p><strong>Sensitive local-only evidence is present.</strong> Do not commit or share this report.</p>
  <table>
    <thead><tr><th>Session</th><th>Kind</th><th>Summary</th></tr></thead>
    <tbody>{evidence_rows}</tbody>
  </table>
"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Code Briefcase outcome report</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #111; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th, td {{ border: 1px solid #ddd; padding: 0.5rem; text-align: left; }}
    pre {{ background: #f6f8fa; padding: 1rem; overflow-x: auto; }}
  </style>
</head>
<body>
  <h1>Code Briefcase outcome report</h1>
  <p>Verdict distribution: {_html_text(dict(verdicts))}</p>
  <table>
    <thead><tr><th>Session</th><th>Client</th><th>Verdict</th><th>Hooks</th><th>Errors</th></tr></thead>
    <tbody>{rows}</tbody>
	  </table>
  {evidence_html}
	  <h2>Markdown summary</h2>
  <pre>{html.escape(markdown)}</pre>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Code Briefcase outcome report.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--markdown-out", required=True)
    parser.add_argument("--html-out", required=True)
    args = parser.parse_args()

    payload = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    markdown = render_markdown(payload)
    html = render_html(payload, markdown)

    md_path = Path(args.markdown_out).expanduser()
    html_path = Path(args.html_out).expanduser()
    md_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
