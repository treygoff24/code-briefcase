from __future__ import annotations

from pathlib import Path

from code_briefcase.hooks.signature_render import (
    adapt_signature_for_language,
    collapse_signature_whitespace,
    render_signature,
    truncate_rendered_signature,
)


def test_collapse_signature_whitespace() -> None:
    raw = "async def send({\n  messages,\n}: Options) -> None"
    assert (
        collapse_signature_whitespace(raw)
        == "async def send({ messages, }: Options) -> None"
    )


def test_truncate_rendered_signature() -> None:
    text = "x" * 200
    truncated = truncate_rendered_signature(text, max_len=20)
    assert len(truncated) == 20
    assert truncated.endswith("…")


def test_adapt_signature_for_typescript() -> None:
    assert (
        adapt_signature_for_language("async def send()", "typescript")
        == "async function send()"
    )
    assert (
        adapt_signature_for_language("def constructor()", "typescript", is_method=True)
        == "constructor()"
    )


def test_render_signature_from_ts_file() -> None:
    path = Path("widget.tsx")
    rendered = render_signature("async def loadData(id: string)", path)
    assert rendered.startswith("async function loadData")
    assert "\n" not in rendered
