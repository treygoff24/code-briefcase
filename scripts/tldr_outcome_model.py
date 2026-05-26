from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

MatchConfidence = Literal["none", "low", "medium", "high"]
AttributionConfidence = Literal["none", "low", "medium", "high"]
CausalConfidence = Literal[
    "proxy-only", "manual-annotation", "ab-test", "matched-baseline"
]
Verdict = Literal["helpful", "neutral", "harmful", "proxy-only", "insufficient-data"]

ALLOWED_CAUSAL_CONFIDENCE = frozenset(
    {"proxy-only", "manual-annotation", "ab-test", "matched-baseline"}
)


@dataclass
class ToolEvent:
    timestamp: datetime
    category: str
    command_hash: str
    files_read: list[str] = field(default_factory=list)
    files_edited: list[str] = field(default_factory=list)
    failed: bool = False
    failure_kind: str | None = None


@dataclass
class TldrHookEvent:
    timestamp: datetime
    event: str
    status: str
    noop_reason: str | None = None
    trigger_files: list[str] = field(default_factory=list)
    recommended_files: list[str] = field(default_factory=list)
    surfaced_files: list[str] = field(default_factory=list)
    candidate_files: list[str] = field(default_factory=list)
    injected_bytes: int = 0
    duration_ms: int = 0
    candidate_files_total: int = 0
    candidate_files_surfaced: int = 0


@dataclass
class VerificationEvent:
    timestamp: datetime
    command_hash: str
    passed: bool | None


@dataclass
class UserCorrectionEvent:
    timestamp: datetime
    kind: str


@dataclass
class SessionRollup:
    session_id: str
    client: str
    project_hash: str
    window_start: datetime | None = None
    window_end: datetime | None = None
    match_confidence: MatchConfidence = "none"
    attribution_confidence: AttributionConfidence = "none"
    causal_confidence: CausalConfidence = "proxy-only"
    tool_counts_by_category: dict[str, int] = field(default_factory=dict)
    command_hash_counts: dict[str, int] = field(default_factory=dict)
    files_read: set[str] = field(default_factory=set)
    files_edited: set[str] = field(default_factory=set)
    explore_before_first_edit: int = 0
    repeated_file_reads: int = 0
    verification_runs: int = 0
    verification_failures: int = 0
    verification_reruns: int = 0
    failed_tool_outputs: int = 0
    failure_kind_counts: dict[str, int] = field(default_factory=dict)
    user_corrections: int = 0
    user_correction_kind_counts: dict[str, int] = field(default_factory=dict)
    tldr_hooks: int = 0
    tldr_errors: int = 0
    tldr_skips: int = 0
    tldr_noops: int = 0
    tldr_skip_reason_counts: dict[str, int] = field(default_factory=dict)
    tldr_noop_reason_counts: dict[str, int] = field(default_factory=dict)
    tldr_clean_checks: int = 0
    injected_bytes_total: int = 0
    injected_bytes_samples: list[int] = field(default_factory=list)
    hook_duration_samples: list[int] = field(default_factory=list)
    trigger_files_used: int = 0
    surfaced_files_used: int = 0
    recommended_files_used: int = 0
    candidate_files_total: int = 0
    candidate_files_surfaced: int = 0
    candidate_files_later_used: int = 0
    local_evidence: list[dict[str, Any]] = field(default_factory=list)
    _explore_before_edit_pending: bool = True
    _file_read_counts: dict[str, int] = field(default_factory=dict)
    _verification_hashes: list[str] = field(default_factory=list)
    _trigger_total: int = 0
    _recommended_total: int = 0
    _surfaced_total: int = 0
    _file_use_times: dict[str, list[datetime]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.causal_confidence not in ALLOWED_CAUSAL_CONFIDENCE:
            raise ValueError(f"invalid causal_confidence: {self.causal_confidence}")

    def _record_file_use_time(self, path: str, timestamp: datetime) -> None:
        self._file_use_times.setdefault(path, []).append(timestamp)

    def _file_used_after(self, path: str, timestamp: datetime) -> bool:
        for used_at in self._file_use_times.get(path, []):
            try:
                if used_at > timestamp:
                    return True
            except TypeError:
                # Be conservative when historical logs mix naive and aware
                # datetimes: compare wall-clock values but still require a
                # strictly later tool event.
                if used_at.replace(tzinfo=None) > timestamp.replace(tzinfo=None):
                    return True
        return False

    def record_tool(self, event: ToolEvent) -> None:
        self.tool_counts_by_category[event.category] = (
            self.tool_counts_by_category.get(event.category, 0) + 1
        )
        self.command_hash_counts[event.command_hash] = (
            self.command_hash_counts.get(event.command_hash, 0) + 1
        )
        if event.category == "explore" and self._explore_before_edit_pending:
            self.explore_before_first_edit += 1
        if event.files_edited:
            self._explore_before_edit_pending = False
            self.files_edited.update(event.files_edited)
        for path in event.files_edited:
            self._record_file_use_time(path, event.timestamp)
        for path in event.files_read:
            self._file_read_counts[path] = self._file_read_counts.get(path, 0) + 1
            self.files_read.add(path)
            self._record_file_use_time(path, event.timestamp)
            if self._file_read_counts[path] > 1:
                self.repeated_file_reads += 1
        if event.failed:
            self.failed_tool_outputs += 1
            kind = event.failure_kind or "error"
            self.failure_kind_counts[kind] = self.failure_kind_counts.get(kind, 0) + 1

    def record_hook(self, event: TldrHookEvent) -> None:
        self.tldr_hooks += 1
        if event.status == "error":
            self.tldr_errors += 1
        elif event.status == "skipped":
            self.tldr_skips += 1
            if event.noop_reason:
                self.tldr_skip_reason_counts[event.noop_reason] = (
                    self.tldr_skip_reason_counts.get(event.noop_reason, 0) + 1
                )
        elif event.status == "noop":
            self.tldr_noops += 1
            if event.noop_reason == "clean_no_diagnostics":
                self.tldr_clean_checks += 1
            if event.noop_reason:
                self.tldr_noop_reason_counts[event.noop_reason] = (
                    self.tldr_noop_reason_counts.get(event.noop_reason, 0) + 1
                )
        elif event.status == "ok":
            if event.noop_reason == "clean_no_diagnostics":
                self.tldr_clean_checks += 1
                # also surface in the per-reason rollup so reports remain consistent
                self.tldr_noop_reason_counts[event.noop_reason] = (
                    self.tldr_noop_reason_counts.get(event.noop_reason, 0) + 1
                )
        self.injected_bytes_total += event.injected_bytes
        self.injected_bytes_samples.append(event.injected_bytes)
        self.hook_duration_samples.append(event.duration_ms)
        self.candidate_files_total += event.candidate_files_total
        self.candidate_files_surfaced += event.candidate_files_surfaced
        for path in event.trigger_files:
            self._trigger_total += 1
            if self._file_used_after(path, event.timestamp):
                self.trigger_files_used += 1
        for path in event.recommended_files:
            self._recommended_total += 1
            if self._file_used_after(path, event.timestamp):
                self.recommended_files_used += 1
        for path in event.surfaced_files:
            self._surfaced_total += 1
            if self._file_used_after(path, event.timestamp):
                self.surfaced_files_used += 1
        for path in event.candidate_files:
            if self._file_used_after(path, event.timestamp):
                self.candidate_files_later_used += 1

    def record_verification(self, event: VerificationEvent) -> None:
        self.verification_runs += 1
        if event.passed is False:
            self.verification_failures += 1
        if event.command_hash in self._verification_hashes:
            self.verification_reruns += 1
        self._verification_hashes.append(event.command_hash)

    def record_user_correction(self, event: UserCorrectionEvent) -> None:
        self.user_corrections += 1
        self.user_correction_kind_counts[event.kind] = (
            self.user_correction_kind_counts.get(event.kind, 0) + 1
        )

    def record_local_evidence(
        self, evidence: dict[str, Any], *, limit: int = 100
    ) -> None:
        if len(self.local_evidence) >= limit:
            return
        self.local_evidence.append(evidence)

    def recommended_file_hit_rate(self) -> float | None:
        if self._recommended_total == 0:
            return None
        return self.recommended_files_used / self._recommended_total

    def surfaced_file_hit_rate(self) -> float | None:
        if self._surfaced_total == 0:
            return None
        return self.surfaced_files_used / self._surfaced_total

    def _compute_verdict(self) -> tuple[Verdict, list[str]]:
        reasons: list[str] = []
        if self.failed_tool_outputs >= 10 or self.tldr_errors >= 3:
            if self.failed_tool_outputs >= 10:
                reasons.append("failed_tool_outputs")
            if self.tldr_errors >= 3:
                reasons.append("hook_errors")
            return "harmful", reasons or ["high_failure_rate"]
        if self.tldr_hooks == 0 and not self.tool_counts_by_category:
            return "insufficient-data", ["no_activity"]
        if self.tldr_hooks == 0:
            return "proxy-only", ["no_tldr_hooks"]
        if self.user_corrections >= 2 and self.verification_failures >= 2:
            return "harmful", ["user_corrections", "verification_failures"]
        if self.surfaced_files_used > 0 and self.repeated_file_reads == 0:
            return "helpful", ["surfaced_context_used"]
        if self.explore_before_first_edit <= 1 and self.verification_failures == 0:
            return "helpful", ["efficient_edit_path"]
        if self.causal_confidence == "proxy-only":
            return "proxy-only", ["proxy_metrics_only"]
        return "neutral", ["mixed_signals"]

    def to_dict(self, *, include_local_evidence: bool = False) -> dict[str, Any]:
        verdict, verdict_reasons = self._compute_verdict()
        injected = self.injected_bytes_samples
        hook_durations = self.hook_duration_samples
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "client": self.client,
            "project_hash": self.project_hash,
            "window_start": (
                self.window_start.isoformat() if self.window_start else None
            ),
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "match_confidence": self.match_confidence,
            "attribution_confidence": self.attribution_confidence,
            "causal_confidence": self.causal_confidence,
            "verdict": verdict,
            "verdict_reasons": verdict_reasons,
            "tool_counts_by_category": dict(self.tool_counts_by_category),
            "unique_command_shapes": len(self.command_hash_counts),
            "repeated_command_runs": sum(
                count - 1 for count in self.command_hash_counts.values() if count > 1
            ),
            "files_read_count": len(self.files_read),
            "files_edited_count": len(self.files_edited),
            "explore_before_first_edit": self.explore_before_first_edit,
            "repeated_file_reads": self.repeated_file_reads,
            "verification_runs": self.verification_runs,
            "verification_failures": self.verification_failures,
            "verification_reruns": self.verification_reruns,
            "failed_tool_outputs": self.failed_tool_outputs,
            "failure_kind_counts": dict(self.failure_kind_counts),
            "user_corrections": self.user_corrections,
            "user_correction_kind_counts": dict(self.user_correction_kind_counts),
            "tldr_hooks": self.tldr_hooks,
            "tldr_errors": self.tldr_errors,
            "tldr_skips": self.tldr_skips,
            "tldr_noops": self.tldr_noops,
            "tldr_skip_reason_counts": dict(self.tldr_skip_reason_counts),
            "tldr_noop_reason_counts": dict(self.tldr_noop_reason_counts),
            "tldr_clean_checks": self.tldr_clean_checks,
            "injected_bytes_total": self.injected_bytes_total,
            "injected_bytes_p50": statistics.median(injected) if injected else 0,
            "injected_bytes_p95": (
                sorted(injected)[int(max(0, len(injected) * 0.95 - 1))]
                if injected
                else 0
            ),
            "hook_duration_p50": (
                statistics.median(hook_durations) if hook_durations else 0
            ),
            "hook_duration_p95": (
                sorted(hook_durations)[int(max(0, len(hook_durations) * 0.95 - 1))]
                if hook_durations
                else 0
            ),
            "trigger_files_used": self.trigger_files_used,
            "surfaced_files_used": self.surfaced_files_used,
            "recommended_files_used": self.recommended_files_used,
            "candidate_files_total": self.candidate_files_total,
            "candidate_files_surfaced": self.candidate_files_surfaced,
            "candidate_files_later_used": self.candidate_files_later_used,
        }
        if include_local_evidence:
            data["local_evidence"] = list(self.local_evidence)
        return data
