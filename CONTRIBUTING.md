# Contributing to code-briefcase

Thank you for considering contributing to code-briefcase! We welcome contributions of all kinds.

## Before You Start

- **Check existing issues** - Someone may already be working on it
- **Open an issue first** for large changes to discuss the approach

## Pull Request Guidelines

### Keep PRs Focused

Each pull request should address **one logical change**. This helps with:
- Faster, more thorough review
- Easier rollback if issues arise
- Cleaner git history

If you have multiple unrelated changes, please submit them as separate PRs.

### PR Checklist

Before submitting:

1. **Rebase on `main`** to avoid merge conflicts
2. **Run the full gate**: `make check`
3. **Confirm formatting/type checks pass**: Black, Ruff, and mypy are enforced by the local hooks and CI
4. **Update docs** if you changed public APIs

### Commit Messages

Use conventional commits:
```
fix: description of bug fix
feat: description of new feature
perf: description of performance improvement
docs: description of documentation change
```

## Development Setup

```bash
git clone https://github.com/treygoff24/code-briefcase.git
cd code-briefcase
uv venv && uv pip install -e ".[dev]"
make install-hooks
```

## Local Quality Gates

Run the same gates enforced by CI and Git hooks:

```bash
make quickcheck  # Black check, Ruff, mypy
make check       # quickcheck plus pytest
```

Git hooks are stored in `.githooks/`. `make install-hooks` points this checkout
at those tracked hooks:

- `pre-commit`: `make quickcheck`
- `pre-push`: `make check`

## Questions?

Open an issue - we're happy to help!
