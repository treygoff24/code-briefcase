from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tldr.api import extract_file, get_file_tree, search
from tldr.dirty_flag import get_dirty_files
from tldr.tldrignore import IgnoreSpec

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
EXCLUDE_PARTS = {
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
SECRET_PATTERNS = re.compile(r"(^|[/_.-])(secret|secrets|credential|credentials|token|key|id_rsa|id_ed25519|\.env)([/_.-]|$)", re.I)


@dataclass
class ContextPackItem:
    path: str
    reason: str
    content: str
    estimated_tokens: int
    diagnostics: str = "not run"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "reason": self.reason,
            "content": self.content,
            "estimated_tokens": self.estimated_tokens,
            "diagnostics": self.diagnostics,
        }


@dataclass
class ContextPack:
    query: str
    project: str
    budget: int
    items: list[ContextPackItem] = field(default_factory=list)
    suggested_reads: list[str] = field(default_factory=list)

    @property
    def estimated_tokens(self) -> int:
        return sum(item.estimated_tokens for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "project": self.project,
            "budget": self.budget,
            "estimated_tokens": self.estimated_tokens,
            "items": [item.to_dict() for item in self.items],
            "suggested_reads": self.suggested_reads,
        }

    def to_markdown(self) -> str:
        lines = [
            "# TLDR Context Pack",
            "",
            f"Query: {self.query or '(changed files)'}",
            f"Project: {self.project}",
            f"Budget: {self.budget} tokens",
            f"Estimated tokens: {self.estimated_tokens}",
            "",
            "## High-Signal Files",
            "",
        ]
        if not self.items:
            lines.append("- No matching code files found.")
        for item in self.items:
            lines.extend(
                [
                    f"### {item.path}",
                    f"Reason: {item.reason}",
                    "",
                    item.content,
                    "",
                    f"Diagnostics: {item.diagnostics}",
                    "",
                ]
            )
        if self.suggested_reads:
            lines.extend(["## Suggested Next Reads", ""])
            lines.extend(f"- {read}" for read in self.suggested_reads)
        return "\n".join(lines).rstrip() + "\n"


def estimate_tokens(text: str) -> int:
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _is_secret_path(path: Path) -> bool:
    return bool(SECRET_PATTERNS.search(path.as_posix()))


def _relative_to_project(project: Path, path: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()


def _is_candidate(project: Path, path: Path, ignore_spec: IgnoreSpec) -> bool:
    rel_path = _relative_to_project(project, path)
    return path.suffix.lower() in CODE_EXTENSIONS and not (
        set(Path(rel_path).parts) & EXCLUDE_PARTS
        or _is_secret_path(Path(rel_path))
        or ignore_spec.match_file(rel_path)
    )


def _resolve_file(project: Path, file_name: str) -> Path:
    path = Path(file_name).expanduser()
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _changed_files(project: Path) -> list[Path]:
    files = [_resolve_file(project, file_name) for file_name in get_dirty_files(project)]
    for command in (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            result = subprocess.run(
                command,
                cwd=str(project),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                files.extend(
                    _resolve_file(project, line.strip())
                    for line in result.stdout.splitlines()
                    if line.strip()
                )
        except Exception:
            pass
    return files


def _semantic_files(project: Path, query: str, language: str) -> list[Path]:
    index = project / ".tldr" / "cache" / "semantic" / "index.faiss"
    if not index.exists() or not query:
        return []
    try:
        from tldr.semantic import semantic_search

        results = semantic_search(str(project), query, k=8, model=None)
    except Exception:
        return []

    paths: list[Path] = []
    for result in results:
        file_name = result.get("file") or result.get("file_path") or result.get("path")
        if file_name:
            paths.append(_resolve_file(project, file_name))
    return paths


def _text_search_files(project: Path, query: str, ignore_spec: IgnoreSpec) -> list[Path]:
    terms = [term for term in re.findall(r"[A-Za-z_][\w-]+", query) if len(term) > 2]
    if not terms:
        return []
    seen: list[Path] = []
    for term in terms[:5]:
        try:
            for match in search(
                term,
                project,
                extensions=CODE_EXTENSIONS,
                max_results=20,
                max_files=2000,
                ignore_spec=ignore_spec,
            ):
                path = _resolve_file(project, match.get("file", ""))
                if path not in seen:
                    seen.append(path)
        except Exception:
            continue
    return seen


def _outline_file(project: Path, path: Path, reason: str, ignore_spec: IgnoreSpec) -> ContextPackItem | None:
    if not path.exists() or not path.is_file() or not _is_candidate(project, path, ignore_spec):
        return None
    try:
        info = extract_file(str(path), base_path=str(project))
    except Exception:
        return None

    rel = _relative_to_project(project, path)
    lines: list[str] = []
    imports = info.get("imports") or []
    if imports:
        lines.append("Imports:")
        for imp in imports[:10]:
            names = imp.get("names") or []
            prefix = "from " if imp.get("is_from") else ""
            suffix = f": {', '.join(names)}" if names else ""
            lines.append(f"- {prefix}{imp.get('module', '')}{suffix}")
        lines.append("")

    functions = info.get("functions") or []
    classes = info.get("classes") or []
    if functions or classes:
        lines.append("Symbols:")
    for func in functions[:25]:
        lines.append(f"- {func.get('signature') or func.get('name')} [L{func.get('line_number', '?')}]")
    for cls in classes[:12]:
        lines.append(f"- {cls.get('signature') or cls.get('name')} [L{cls.get('line_number', '?')}]")
        for method in (cls.get("methods") or [])[:8]:
            lines.append(f"  - {method.get('signature') or method.get('name')} [L{method.get('line_number', '?')}]")
    calls = (info.get("call_graph") or {}).get("calls") or {}
    if calls:
        lines.extend(["", "Calls:"])
        for caller, callees in list(calls.items())[:10]:
            lines.append(f"- {caller}: {', '.join(callees[:8])}")

    content = "\n".join(lines) if lines else f"{rel}: no symbols extracted"
    return ContextPackItem(
        path=rel,
        reason=reason,
        content=content,
        estimated_tokens=estimate_tokens(content),
    )


def _structure_summary(project: Path, ignore_spec: IgnoreSpec) -> ContextPackItem:
    try:
        tree = get_file_tree(project, extensions=CODE_EXTENSIONS, ignore_spec=ignore_spec)
        content = json.dumps(tree, indent=2)[:4000]
    except Exception:
        content = f"Project root: {project}"
    return ContextPackItem(
        path=".",
        reason="project structure fallback",
        content=content,
        estimated_tokens=estimate_tokens(content),
    )


def _fit_item_to_budget(item: ContextPackItem, budget: int) -> ContextPackItem:
    while item.estimated_tokens > budget and len(item.content) > 120:
        item.content = item.content[: int(len(item.content) * 0.7)].rstrip()
        item.estimated_tokens = estimate_tokens(item.content)
    return item


def build_context_pack(
    query: str,
    project: str | Path = ".",
    budget: int = 3000,
    files: list[str] | None = None,
    changed: bool = False,
    include_semantic: bool = True,
    language: str = "auto",
) -> ContextPack:
    project_path = Path(project).expanduser().resolve()
    ignore_spec = IgnoreSpec(project_path, use_gitignore=True)
    candidates: list[tuple[Path, str]] = []

    for file_name in files or []:
        candidates.append((_resolve_file(project_path, file_name), "explicit file"))
    if changed:
        candidates.extend((path, "changed file") for path in _changed_files(project_path))
    if include_semantic:
        candidates.extend((path, "semantic match") for path in _semantic_files(project_path, query, language))
    candidates.extend((path, "text match") for path in _text_search_files(project_path, query, ignore_spec))

    pack = ContextPack(query=query, project=str(project_path), budget=budget)
    seen: set[Path] = set()
    for path, reason in candidates:
        if path in seen:
            continue
        seen.add(path)
        item = _outline_file(project_path, path, reason, ignore_spec)
        if item is None:
            continue
        if pack.estimated_tokens + item.estimated_tokens > budget and pack.items:
            continue
        if item.estimated_tokens > budget:
            item = _fit_item_to_budget(item, budget)
        pack.items.append(item)
        pack.suggested_reads.append(f"Read {item.path} offset=1 limit=120")

    if not pack.items:
        fallback = _structure_summary(project_path, ignore_spec)
        if fallback.estimated_tokens > budget:
            fallback = _fit_item_to_budget(fallback, budget)
        pack.items.append(fallback)
    return pack
