from __future__ import annotations

from pathlib import Path

import pytest

from code_briefcase.hybrid_extractor import HybridExtractor


IMPORT_FIXTURE = """
import "polyfill";
import React from "react";
import fs, { readFile } from "node:fs";
import * as path from "node:path";
import {
  type ActiveIntakeRequest,
  isTerminalRunStatus,
} from "@llm-council/contracts";
import type { Config } from "./types";
"""


@pytest.fixture
def extractor() -> HybridExtractor:
    return HybridExtractor()


def test_ts_import_extraction_variants(
    tmp_path: Path, extractor: HybridExtractor
) -> None:
    source_path = tmp_path / "sample.ts"
    source_path.write_text(IMPORT_FIXTURE, encoding="utf-8")

    info = extractor.extract(source_path)
    imports = info.imports
    assert len(imports) == 6

    side_effect = imports[0]
    assert side_effect.module == "polyfill"
    assert side_effect.names == []
    assert side_effect.is_from is False

    default_import = imports[1]
    assert default_import.module == "react"
    assert default_import.names == ["React"]
    assert default_import.is_from is True

    mixed_import = imports[2]
    assert mixed_import.module == "node:fs"
    assert mixed_import.names == ["fs", "readFile"]
    assert mixed_import.is_from is True

    namespace_import = imports[3]
    assert namespace_import.module == "node:path"
    assert namespace_import.names == ["* as path"]
    assert namespace_import.is_from is True

    destructured = imports[4]
    assert destructured.module == "@llm-council/contracts"
    assert destructured.names == ["ActiveIntakeRequest", "isTerminalRunStatus"]
    assert destructured.is_from is True

    type_import = imports[5]
    assert type_import.module == "./types"
    assert type_import.names == ["Config"]
    assert type_import.is_from is True
