from __future__ import annotations

from code_briefcase.hooks.outcome import HookExecutionResult, ok, skipped
from code_briefcase.hooks.runtime import HookEvent, HookResponse
from code_briefcase.session_warm import count_source_files


def build_pre_compact_response(
    event: HookEvent, max_entries: int = 20
) -> HookExecutionResult:
    """Build cheap workspace context for compaction hooks.

    This intentionally avoids semantic search, full indexing, or daemon round trips.
    It gives a continuation summary enough project shape to remind the next model
    where Code Briefcase can provide deeper context.
    """
    project = event.cwd
    if not project.exists() or not project.is_dir():
        return skipped(reason="project_missing")

    try:
        entries = sorted(
            path.name + ("/" if path.is_dir() else "")
            for path in project.iterdir()
            if not path.name.startswith(".")
        )[:max_entries]
    except Exception:
        entries = []

    try:
        source_count = count_source_files(project, max_count=1001)
        count_text = "1000+" if source_count > 1000 else str(source_count)
    except Exception:
        count_text = "unknown"

    lines = [
        "Code Briefcase compact context:",
        f"- Project: {project.name}",
        f"- Source files: {count_text}",
        '- For targeted follow-up context, run `code-briefcase pack "<task>" --project . --budget 3000`.',
    ]
    if entries:
        lines.append("- Top-level entries: " + ", ".join(entries))

    return ok(
        HookResponse(
            additional_context="\n".join(lines),
            suppress_output=True,
        ),
    )
