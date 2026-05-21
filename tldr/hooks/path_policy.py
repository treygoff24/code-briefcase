from __future__ import annotations

from pathlib import Path
from typing import Any

from tldr.hooks.outcome import event_relative_path
from tldr.hooks.runtime import HookEvent

CODE_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".cxx",
    ".hpp",
    ".rb",
    ".php",
    ".kt",
    ".swift",
    ".cs",
    ".scala",
    ".ex",
    ".exs",
    ".lua",
    ".luau",
}
BYPASS_SUFFIXES = (
    ".test.py",
    "_test.py",
    ".spec.ts",
    ".test.ts",
    ".spec.tsx",
    ".test.tsx",
    ".spec.js",
    ".test.js",
    ".spec.jsx",
    ".test.jsx",
)
BYPASS_PARTS = {
    ".git",
    ".tldr",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
}
SECRET_PARTS = {".env", "secrets", "secret", "credentials", "id_rsa", "id_ed25519"}

MAX_CANDIDATES = 8
MAX_SURFACED = 3


def _is_within_project(project: Path, path: Path) -> bool:
    try:
        project_root = project.expanduser().resolve()
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        candidate.resolve().relative_to(project_root)
    except (OSError, ValueError):
        return False
    return True


def _is_test_file(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith("test_") or any(name.endswith(suffix) for suffix in BYPASS_SUFFIXES)


def _looks_secret(path: Path) -> bool:
    lowered = [part.lower() for part in path.parts]
    return any(part in SECRET_PARTS for part in lowered) or any(
        "secret" in part or "credential" in part for part in lowered
    )


def should_exclude_context_path(
    project: Path, path: Path, *, include_tests: bool = False
) -> bool:
    if not _is_within_project(project, path):
        return True
    if path.suffix.lower() not in CODE_EXTENSIONS:
        return True
    if set(path.parts) & BYPASS_PARTS:
        return True
    if _looks_secret(path):
        return True
    if not include_tests and _is_test_file(path):
        return True
    try:
        if not path.exists():
            return True
    except OSError:
        return True
    return False


def resolve_event_path(event: HookEvent, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = event.cwd / path
    return path.resolve()


def _resolve_import_module(event: HookEvent, source: Path, module: str) -> Path | None:
    module = (module or "").strip()
    if not module or module.startswith(("http:", "https:")):
        return None
    if module.startswith("."):
        base = source.parent
        parts = [part for part in module.split(".") if part]
        for part in parts:
            if part:
                base = base / part
        candidates = [base.with_suffix(".py"), base / "__init__.py"]
        for candidate in candidates:
            if candidate.exists() and not should_exclude_context_path(event.cwd, candidate):
                return candidate
        return None
    stem = module.split(".")[-1]
    same_dir = source.parent / f"{stem}.py"
    if same_dir.exists() and not should_exclude_context_path(event.cwd, same_dir):
        return same_dir
    return None


def _relative_import_name_modules(module: str, names: Any) -> list[str]:
    if module.strip() not in {"", "."}:
        return []
    if not isinstance(names, list):
        return []
    modules: list[str] = []
    for name in names:
        text = str(name).strip()
        if not text or text == "*" or "." in text:
            continue
        modules.append(f".{text}")
    return modules


def _test_neighbor(source: Path) -> Path | None:
    stem = source.stem
    parent = source.parent
    for candidate in (
        parent / f"test_{stem}.py",
        parent / f"{stem}_test.py",
        parent.parent / "tests" / f"test_{stem}.py",
    ):
        if candidate.exists():
            return candidate
    return None


def discover_related_candidates(
    event: HookEvent,
    file_path: Path,
    info: dict[str, Any],
    *,
    context_kind: str,
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    """Return candidate metadata, recommended paths, and surfaced paths."""
    seen: set[str] = set()
    ordered: list[tuple[Path, str, float]] = []

    def add(path: Path | None, reason: str, score: float) -> None:
        if path is None:
            return
        rel = event_relative_path(event, path)
        if rel is None or rel in seen:
            return
        seen.add(rel)
        ordered.append((path, reason, score))

    for imp in info.get("imports") or []:
        module = str(imp.get("module") or "")
        add(_resolve_import_module(event, file_path, module), "import", 1.0)
        if imp.get("is_from"):
            for named_module in _relative_import_name_modules(module, imp.get("names")):
                add(_resolve_import_module(event, file_path, named_module), "import", 1.0)

    for sibling in sorted(file_path.parent.glob(f"{file_path.stem}.*")):
        if sibling == file_path:
            continue
        if sibling.suffix.lower() in CODE_EXTENSIONS:
            add(sibling, "same_directory", 0.8)

    test_path = _test_neighbor(file_path)
    if test_path is not None:
        add(test_path, "test_neighbor", 0.5)

    candidates: list[dict[str, object]] = []
    recommended: list[str] = []
    surfaced: list[str] = []
    surfaced_count = 0

    for rank, (path, reason, score) in enumerate(ordered[:MAX_CANDIDATES], start=1):
        rel = event_relative_path(event, path)
        if rel is None:
            continue
        excluded_reason = None
        include_tests = False
        if should_exclude_context_path(event.cwd, path, include_tests=include_tests):
            if _is_test_file(path):
                excluded_reason = "test_default_excluded"
            elif not path.exists():
                excluded_reason = "missing_file"
            else:
                excluded_reason = "excluded"
        will_surface = False
        if excluded_reason is None and surfaced_count < MAX_SURFACED:
            will_surface = True
            surfaced_count += 1
            surfaced.append(rel)
        entry: dict[str, object] = {
            "path": rel,
            "reason": reason,
            "rank": rank,
            "score": score,
            "surfaced": will_surface,
        }
        if excluded_reason:
            entry["excluded_reason"] = excluded_reason
        candidates.append(entry)
        recommended.append(rel)

    return candidates, recommended, surfaced


def format_related_files_section(surfaced_paths: list[str]) -> str:
    if not surfaced_paths:
        return ""
    lines = ["", "Related files:"]
    for rel in surfaced_paths[:MAX_SURFACED]:
        lines.append(f"- {rel}")
    return "\n".join(lines)
