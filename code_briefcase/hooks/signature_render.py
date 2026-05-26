from __future__ import annotations

import re
from pathlib import Path

_SIGNATURE_MAX_LEN = 160


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
    if language in {"typescript", "javascript"}:
        if is_method:
            if text.startswith("async def "):
                return text[len("async def ") :]
            if text.startswith("def "):
                return text[len("def ") :]
            return text
        if text.startswith("async def "):
            return "async function " + text[len("async def ") :]
        if text.startswith("def "):
            return "function " + text[len("def ") :]
        return text
    if language == "go":
        if text.startswith("async def "):
            return "func " + text[len("async def ") :]
        if text.startswith("def "):
            return "func " + text[len("def ") :]
        return text
    if language == "rust":
        if text.startswith("async def "):
            return "async fn " + text[len("async def ") :]
        if text.startswith("def "):
            return "fn " + text[len("def ") :]
        return text
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
