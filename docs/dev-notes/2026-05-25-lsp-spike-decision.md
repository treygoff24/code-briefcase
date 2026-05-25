# Phase 1.5 LSP spike decision

Date: 2026-05-25

Decision: keep Phase 1 on `tsc --watch`; do not lock the adapter API around LSP until a separate prototype proves the value.

Why:

- `tsc --watch` directly addresses the current hot path: cold TypeScript typechecking in post-edit hooks.
- It has no new required dependency beyond `tsc`, which repos already need for TypeScript diagnostics.
- The current hook contract only needs file-scoped diagnostics, not completions, hover, references, or code actions.
- LSP becomes more attractive for Phase 3+ when adding pyright/gopls/rust-analyzer-style adapters and richer structured diagnostics.

Phase 1.5 exit criteria before adopting LSP:

1. Prototype `typescript-language-server` against a real repo.
2. Measure startup, first diagnostics, warm recheck, and shutdown behavior.
3. Verify diagnostics can be scoped to the edited file without leaking unrelated project errors.
4. Confirm process lifecycle, JSON-RPC framing, and cancellation behavior are simpler or more reliable than `tsc --watch`.
5. Keep `WatchAdapter` abstractions broad enough for both `COMPILER_WATCH_TEXT` and `LSP_DIAGNOSTICS`.

Current recommendation: ship the compiler-watch adapter and telemetry first, then evaluate LSP with real latency and lifecycle evidence.

