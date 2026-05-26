from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_briefcase.hooks.outcome import event_relative_path
from code_briefcase.hooks.runtime import HookEvent

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
MARKDOWN_EXTENSIONS = {".md", ".mdx"}
STRUCTURED_EXTENSIONS = {".html", ".htm", ".sql", ".yaml", ".yml", ".json", ".sh"}
CONFIG_FILENAMES = {
    ".gitignore",
    ".prettierignore",
    ".dockerignore",
    "Dockerfile",
    "Makefile",
}
GENERATED_OR_LOCK_FILENAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "composer.lock",
    "Cargo.lock",
}
SECRET_CONFIG_FILENAMES = {".npmrc", ".pypirc"}
SECRET_CONFIG_PATTERNS = ("service-account", "credentials", "credential")
STRUCTURED_MAX_BYTES = 256 * 1024

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
    ".code-briefcase",
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


@dataclass(frozen=True)
class ContextPathDecision:
    allowed: bool
    reason: str
    file_kind: str


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
    return name.startswith("test_") or any(
        name.endswith(suffix) for suffix in BYPASS_SUFFIXES
    )


def looks_secret_path(path: Path) -> bool:
    lowered = [part.lower() for part in path.parts]
    if any(part in SECRET_PARTS for part in lowered):
        return True
    return any("secret" in part or "credential" in part for part in lowered)


def _looks_secret(path: Path) -> bool:
    return looks_secret_path(path)


def _is_secret_config_filename(name: str) -> bool:
    lowered = name.lower()
    if lowered in SECRET_CONFIG_FILENAMES:
        return True
    if lowered in GENERATED_OR_LOCK_FILENAMES:
        return True
    return any(pattern in lowered for pattern in SECRET_CONFIG_PATTERNS)


def _structured_file_size_ok(path: Path) -> bool:
    try:
        return path.stat().st_size <= STRUCTURED_MAX_BYTES
    except OSError:
        return False


def classify_context_path(
    project: Path, path: Path, *, include_tests: bool = True
) -> ContextPathDecision:
    if not _is_within_project(project, path):
        return ContextPathDecision(False, "outside_project", "unknown")

    suffix = path.suffix.lower()
    name = path.name

    if suffix in MARKDOWN_EXTENSIONS:
        return ContextPathDecision(False, "markdown_unsupported", "markdown")

    if set(path.parts) & BYPASS_PARTS:
        return ContextPathDecision(False, "excluded_dir", "unknown")

    if _looks_secret(path) or _is_secret_config_filename(name):
        return ContextPathDecision(False, "secret_like", "secret")

    try:
        exists = path.exists()
    except OSError:
        exists = False

    if name in GENERATED_OR_LOCK_FILENAMES:
        return ContextPathDecision(False, "secret_like", "generated")

    is_test = _is_test_file(path)
    if is_test and not include_tests:
        return ContextPathDecision(False, "excluded", "test")

    if suffix in CODE_EXTENSIONS:
        if not exists:
            return ContextPathDecision(False, "missing_file", "code")
        if is_test:
            return ContextPathDecision(True, "ok_test", "test")
        return ContextPathDecision(True, "ok_code", "code")

    is_structured = suffix in STRUCTURED_EXTENSIONS
    is_config = name in CONFIG_FILENAMES

    if is_structured or is_config:
        if not exists:
            return ContextPathDecision(False, "missing_file", "structured")
        if is_structured and not _structured_file_size_ok(path):
            return ContextPathDecision(False, "excluded", "structured")
        if is_config:
            return ContextPathDecision(True, "ok_config", "config")
        return ContextPathDecision(True, "ok_structured", "structured")

    return ContextPathDecision(False, "unsupported_extension", "unknown")


def should_exclude_context_path(
    project: Path, path: Path, *, include_tests: bool = True
) -> bool:
    return not classify_context_path(project, path, include_tests=include_tests).allowed


def resolve_event_path(event: HookEvent, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = event.cwd / path
    return path.resolve()


_TS_JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_TS_JS_SOURCE_SUFFIXES = {suffix for suffix in _TS_JS_SUFFIXES}
_MAX_IMPORT_MODULE_LEN = 256


def _candidate_allowed(event: HookEvent, candidate: Path) -> bool:
    return candidate.exists() and not should_exclude_context_path(
        event.cwd, candidate, include_tests=True
    )


def _resolve_import_module_python(
    event: HookEvent, source: Path, module: str
) -> Path | None:
    if module.startswith("."):
        base = source.parent
        parts = [part for part in module.split(".") if part]
        for part in parts:
            if part:
                base = base / part
        candidates = [base.with_suffix(".py"), base / "__init__.py"]
        for candidate in candidates:
            if _candidate_allowed(event, candidate):
                return candidate
        return None
    stem = module.split(".")[-1]
    same_dir = source.parent / f"{stem}.py"
    if _candidate_allowed(event, same_dir):
        return same_dir
    return None


def _ts_js_import_candidates(base: Path) -> list[Path]:
    resolved = base.resolve()
    suffix = resolved.suffix.lower()
    candidates: list[Path] = []
    if suffix in _TS_JS_SOURCE_SUFFIXES:
        candidates.append(resolved)
        return candidates
    for ext in _TS_JS_SUFFIXES:
        candidates.append(Path(f"{resolved}{ext}"))
    for ext in _TS_JS_SUFFIXES:
        candidates.append(resolved / f"index{ext}")
    return candidates


def _resolve_import_module_ts_js(
    event: HookEvent, source: Path, module: str
) -> Path | None:
    if not module.startswith(("./", "../")):
        return None
    base = (source.parent / module).resolve()
    for candidate in _ts_js_import_candidates(base):
        if _candidate_allowed(event, candidate):
            return candidate
    return None


def _resolve_import_module_other(
    event: HookEvent, source: Path, module: str
) -> Path | None:
    if module.startswith((".", "/")) or "/" in module:
        return None
    ext = source.suffix.lower()
    if not ext:
        return None
    candidate = source.parent / f"{module}{ext}"
    if _candidate_allowed(event, candidate):
        return candidate
    return None


def _resolve_import_module(event: HookEvent, source: Path, module: str) -> Path | None:
    module = (module or "").strip()
    if not module or module.startswith(("http:", "https:")):
        return None
    if len(module) > _MAX_IMPORT_MODULE_LEN:
        return None

    suffix = source.suffix.lower()
    if suffix == ".py":
        return _resolve_import_module_python(event, source, module)
    if suffix in _TS_JS_SOURCE_SUFFIXES:
        return _resolve_import_module_ts_js(event, source, module)
    return _resolve_import_module_other(event, source, module)


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
    suffix = source.suffix.lower()
    if suffix in _TS_JS_SOURCE_SUFFIXES:
        candidates = [
            parent / f"{stem}.test.ts",
            parent / f"{stem}.test.tsx",
            parent / f"{stem}.spec.ts",
            parent / f"{stem}.spec.tsx",
            parent / "__tests__" / f"{stem}.test.ts",
            parent / "__tests__" / f"{stem}.test.tsx",
        ]
    else:
        candidates = [
            parent / f"test_{stem}.py",
            parent / f"{stem}_test.py",
            parent.parent / "tests" / f"test_{stem}.py",
        ]
    for candidate in candidates:
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
                add(
                    _resolve_import_module(event, file_path, named_module),
                    "import",
                    1.0,
                )

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
        decision = classify_context_path(event.cwd, path, include_tests=True)
        excluded_reason = None
        if not decision.allowed:
            if decision.reason == "markdown_unsupported":
                excluded_reason = "markdown_unsupported"
            elif _is_test_file(path) and decision.reason == "excluded":
                excluded_reason = "test_default_excluded"
            elif decision.reason == "missing_file":
                excluded_reason = "missing_file"
            else:
                excluded_reason = decision.reason
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
