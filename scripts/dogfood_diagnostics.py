#!/usr/bin/env python3
"""Dogfood Code Briefcase JS/TS diagnostics against synthetic and real repositories.

The synthetic repo deliberately includes:
- a TS path alias that only works when tsc runs project-aware
- unrelated project errors that must not leak into single-file diagnostics
- JS type-checking via allowJs/checkJs
- oxlint and oxfmt findings
- a .d.ts file that must not be checked by oxfmt
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "dogfood-output"
FAKE_REPO = OUTPUT_ROOT / "fake-js-ts-repo"


class DogfoodFailure(AssertionError):
    """Raised when a dogfood scenario violates the expected product contract."""


def run(
    cmd: list[str],
    *,
    cwd: Path = REPO_ROOT,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise DogfoodFailure(
            "Command failed\n"
            f"cwd={cwd}\n"
            f"cmd={' '.join(cmd)}\n"
            f"exit={result.returncode}\n"
            f"stdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )
    return result


def run_tldr(args: list[str], *, timeout: int = 180) -> dict[str, Any]:
    result = run([sys.executable, "-m", "code_briefcase.cli", *args], timeout=timeout)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DogfoodFailure(
            f"Code Briefcase did not emit JSON for args={args}\n"
            f"stdout={result.stdout}\n"
            f"stderr={result.stderr}"
        ) from exc
    if not isinstance(payload, dict):
        raise DogfoodFailure(
            f"Code Briefcase emitted non-object JSON for args={args}\n"
            f"stdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )
    return payload


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def prepare_fake_repo() -> Path:
    if OUTPUT_ROOT.exists() and not OUTPUT_ROOT.is_dir():
        raise DogfoodFailure(f"{OUTPUT_ROOT} exists and is not a directory")
    OUTPUT_ROOT.mkdir(exist_ok=True)

    if FAKE_REPO.exists():
        shutil.rmtree(FAKE_REPO)
    FAKE_REPO.mkdir(parents=True)

    write(
        FAKE_REPO / "package.json",
        json.dumps(
            {
                "private": True,
                "type": "module",
                "devDependencies": {
                    "typescript": "6.0.3",
                    "oxlint": "1.65.0",
                    "oxfmt": "0.50.0",
                },
            },
            indent=2,
        )
        + "\n",
    )
    write(
        FAKE_REPO / "tsconfig.json",
        json.dumps(
            {
                "compilerOptions": {
                    "allowJs": True,
                    "baseUrl": ".",
                    "checkJs": True,
                    "ignoreDeprecations": "6.0",
                    "jsx": "react-jsx",
                    "module": "ESNext",
                    "moduleResolution": "Bundler",
                    "noEmit": True,
                    "paths": {"@/*": ["src/*"]},
                    "strict": True,
                    "target": "ES2022",
                },
                "include": ["src/**/*.ts", "src/**/*.tsx", "src/**/*.js"],
            },
            indent=2,
        )
        + "\n",
    )
    write(FAKE_REPO / "src" / "value.ts", 'export const value: string = "ok";\n')
    write(
        FAKE_REPO / "src" / "clean.ts",
        'import { value } from "@/value";\n\nexport const clean: string = value;\n',
    )
    write(
        FAKE_REPO / "src" / "type-error.ts",
        'import { value } from "@/value";\n\nexport const answer: number = value;\n',
    )
    write(FAKE_REPO / "src" / "other-error.ts", "export const other: string = 42;\n")
    write(
        FAKE_REPO / "src" / "type-error.js",
        "// @ts-check\n" "/** @type {string} */\n" "export const jsAnswer = 42;\n",
    )
    write(
        FAKE_REPO / "src" / "lint.ts",
        "export function lintProbe() {\n  debugger;\n}\n",
    )
    write(FAKE_REPO / "src" / "drifted.ts", "export const drifted={value:42}\n")
    write(FAKE_REPO / "src" / "types.d.ts", "declare const answer:{value:number}\n")

    run(
        ["npm", "install", "--no-audit", "--no-fund", "--silent"],
        cwd=FAKE_REPO,
        timeout=300,
    )
    return FAKE_REPO


def sources(result: dict[str, Any]) -> set[str]:
    return {item.get("source", "") for item in result.get("diagnostics", [])}


def rules(result: dict[str, Any]) -> set[str]:
    return {item.get("rule", "") for item in result.get("diagnostics", [])}


def assert_tools(result: dict[str, Any], expected: list[str], label: str) -> None:
    actual = result.get("tools")
    if actual != expected:
        raise DogfoodFailure(
            f"{label}: tools mismatch; expected={expected}, actual={actual}"
        )


def assert_no_diagnostics(result: dict[str, Any], label: str) -> None:
    if result.get("diagnostics"):
        raise DogfoodFailure(f"{label}: expected no diagnostics, got={result}")


def assert_only_file(result: dict[str, Any], expected_file: Path, label: str) -> None:
    expected = str(expected_file.resolve())
    for diagnostic in result.get("diagnostics", []):
        if diagnostic.get("source") == "oxfmt":
            # Single-file oxfmt diagnostics are already attached to the target.
            pass
        if str(Path(diagnostic.get("file", "")).resolve()) != expected:
            raise DogfoodFailure(
                f"{label}: diagnostic leaked from another file; "
                f"expected={expected}, diagnostic={diagnostic}"
            )


def run_fake_suite() -> dict[str, Any]:
    repo = prepare_fake_repo()
    report: dict[str, Any] = {"fake_repo": str(repo), "scenarios": {}}

    def check(name: str, rel: str, *, lang: str | None = None) -> dict[str, Any]:
        args = ["diagnostics", str(repo / rel), "--format", "json"]
        if lang:
            args.extend(["--lang", lang])
        result = run_tldr(args)
        report["scenarios"][name] = result
        return result

    clean = check("single_file_clean_alias", "src/clean.ts")
    assert_tools(clean, ["tsc", "oxlint", "oxfmt"], "single_file_clean_alias")
    assert_no_diagnostics(clean, "single_file_clean_alias")

    ts_error = check("single_file_ts_error_filtered", "src/type-error.ts")
    assert_tools(ts_error, ["tsc", "oxlint", "oxfmt"], "single_file_ts_error_filtered")
    if ts_error.get("error_count") != 1 or "TS2322" not in rules(ts_error):
        raise DogfoodFailure(
            f"single_file_ts_error_filtered: expected one TS2322, got={ts_error}"
        )
    assert_only_file(
        ts_error, repo / "src" / "type-error.ts", "single_file_ts_error_filtered"
    )

    js_error = check("single_file_js_typecheck", "src/type-error.js")
    assert_tools(js_error, ["tsc", "oxlint", "oxfmt"], "single_file_js_typecheck")
    if js_error.get("error_count") != 1 or "TS2322" not in rules(js_error):
        raise DogfoodFailure(
            f"single_file_js_typecheck: expected one TS2322, got={js_error}"
        )
    assert_only_file(
        js_error, repo / "src" / "type-error.js", "single_file_js_typecheck"
    )

    lint = check("single_file_oxlint", "src/lint.ts")
    assert_tools(lint, ["tsc", "oxlint", "oxfmt"], "single_file_oxlint")
    if "oxlint" not in sources(lint) or "eslint(no-debugger)" not in rules(lint):
        raise DogfoodFailure(
            f"single_file_oxlint: expected no-debugger warning, got={lint}"
        )
    assert_only_file(lint, repo / "src" / "lint.ts", "single_file_oxlint")

    drift = check("single_file_oxfmt", "src/drifted.ts")
    assert_tools(drift, ["tsc", "oxlint", "oxfmt"], "single_file_oxfmt")
    if "oxfmt" not in sources(drift) or drift.get("warning_count", 0) < 1:
        raise DogfoodFailure(f"single_file_oxfmt: expected oxfmt warning, got={drift}")
    assert_only_file(drift, repo / "src" / "drifted.ts", "single_file_oxfmt")

    declaration = check("single_file_declaration_skips_oxfmt", "src/types.d.ts")
    assert_tools(declaration, ["tsc", "oxlint"], "single_file_declaration_skips_oxfmt")
    if "oxfmt" in sources(declaration):
        raise DogfoodFailure(
            f"single_file_declaration_skips_oxfmt: got oxfmt diagnostic={declaration}"
        )

    project = run_tldr(
        [
            "diagnostics",
            str(repo),
            "--project",
            "--lang",
            "typescript",
            "--format",
            "json",
        ],
        timeout=300,
    )
    report["scenarios"]["project_typescript"] = project
    assert_tools(project, ["tsc", "oxlint", "oxfmt"], "project_typescript")
    if not {"tsc", "oxlint", "oxfmt"}.issubset(sources(project)):
        raise DogfoodFailure(
            f"project_typescript: expected all diagnostic sources, got={project}"
        )
    if project.get("error_count", 0) < 2 or project.get("warning_count", 0) < 2:
        raise DogfoodFailure(
            f"project_typescript: expected project errors and warnings, got={project}"
        )

    project_js = run_tldr(
        [
            "diagnostics",
            str(repo),
            "--project",
            "--lang",
            "javascript",
            "--format",
            "json",
        ],
        timeout=300,
    )
    report["scenarios"]["project_javascript"] = project_js
    assert_tools(project_js, ["tsc", "oxlint", "oxfmt"], "project_javascript")
    if "tsc" not in sources(project_js) or project_js.get("error_count", 0) < 1:
        raise DogfoodFailure(
            f"project_javascript: expected JS tsc errors, got={project_js}"
        )

    return report


def summarize_real_result(label: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": label,
        "tools": result.get("tools"),
        "error_count": result.get("error_count"),
        "warning_count": result.get("warning_count"),
        "file_count": result.get("file_count"),
        "sources": sorted(sources(result)),
        "first_diagnostics": result.get("diagnostics", [])[:10],
    }


def run_real_suite(paths: list[Path]) -> dict[str, Any]:
    report: dict[str, Any] = {"real_repos": []}
    for path in paths:
        project = run_tldr(
            [
                "diagnostics",
                str(path),
                "--project",
                "--lang",
                "typescript",
                "--format",
                "json",
            ],
            timeout=300,
        )
        report["real_repos"].append(summarize_real_result(f"{path}:project", project))

        first_file = next(
            (
                item
                for pattern in ("*.tsx", "*.ts", "*.jsx", "*.js")
                for item in sorted(path.rglob(pattern))
                if "node_modules" not in item.parts
                and ".next" not in item.parts
                and "dist" not in item.parts
                and not item.name.endswith(".d.ts")
                and not item.name.endswith(".config.ts")
            ),
            None,
        )
        if first_file:
            single = run_tldr(
                ["diagnostics", str(first_file), "--format", "json"], timeout=300
            )
            report["real_repos"].append(
                summarize_real_result(f"{first_file}:single", single)
            )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real-repo",
        action="append",
        type=Path,
        default=[],
        help="Real repo path to smoke after the fake suite. Can be repeated.",
    )
    parser.add_argument(
        "--skip-fake",
        action="store_true",
        help="Only run real repo smoke checks.",
    )
    args = parser.parse_args()

    OUTPUT_ROOT.mkdir(exist_ok=True)
    report: dict[str, Any] = {}

    if not args.skip_fake:
        report.update(run_fake_suite())
    if args.real_repo:
        report.update(run_real_suite([path.resolve() for path in args.real_repo]))

    report_path = OUTPUT_ROOT / "diagnostics-dogfood-report.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nWrote {report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
