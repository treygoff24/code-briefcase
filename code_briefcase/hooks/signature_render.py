from __future__ import annotations

import re
from pathlib import Path

_SIGNATURE_MAX_LEN = 160

# The hybrid extractor normalizes function signatures to Python's
# "def name(...)" / "async def name(...)" form across all languages, so this
# module rewrites that canonical prefix to something idiomatic for the source
# language. Mapping per language: (sync_keyword, async_keyword). When either is
# the empty string, the prefix is stripped without replacement — used for class
# methods in TS/JS where the bare `name(...)` form is correct.
_PY_SYNC_PREFIX = "def "
_PY_ASYNC_PREFIX = "async def "

_LANGUAGE_KEYWORDS: dict[tuple[str, bool], tuple[str, str]] = {
    ("typescript", False): ("function ", "async function "),
    ("typescript", True): ("", ""),
    ("javascript", False): ("function ", "async function "),
    ("javascript", True): ("", ""),
    ("go", False): ("func ", "func "),
    ("go", True): ("func ", "func "),
    ("rust", False): ("fn ", "async fn "),
    ("rust", True): ("fn ", "async fn "),
}


def collapse_signature_whitespace(signature: str) -> str:
    return re.sub(r"\s+", " ", signature).strip()


def truncate_rendered_signature(
    signature: str, max_len: int = _SIGNATURE_MAX_LEN
) -> str:
    if len(signature) <= max_len:
        return signature
    return signature[: max_len - 1].rstrip() + "…"


def language_from_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".ts", ".tsx"}:
        return "typescript"
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        return "javascript"
    if suffix == ".go":
        return "go"
    if suffix == ".rs":
        return "rust"
    return "other"


def adapt_signature_for_language(
    signature: str,
    language: str,
    *,
    is_method: bool = False,
) -> str:
    text = collapse_signature_whitespace(signature)
    keywords = _LANGUAGE_KEYWORDS.get((language, is_method))
    if keywords is None:
        return text
    sync_keyword, async_keyword = keywords
    if text.startswith(_PY_ASYNC_PREFIX):
        return async_keyword + text[len(_PY_ASYNC_PREFIX) :]
    if text.startswith(_PY_SYNC_PREFIX):
        return sync_keyword + text[len(_PY_SYNC_PREFIX) :]
    return text


def render_signature(
    signature: str,
    path: Path,
    *,
    is_method: bool = False,
) -> str:
    language = language_from_path(path)
    adapted = adapt_signature_for_language(signature, language, is_method=is_method)
    return truncate_rendered_signature(adapted)
