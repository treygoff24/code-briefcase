"""Shared contracts for daemon-owned diagnostics watchers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import time
from typing import Any


class AdapterCapability(str, Enum):
    COMPILER_WATCH_TEXT = "compiler_watch_text"
    LSP_DIAGNOSTICS = "lsp_diagnostics"
    ONE_SHOT_CACHED = "one_shot_cached"


class QueryStatus(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    PENDING = "pending"
    FALLBACK_REQUIRED = "fallback_required"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class AdapterKey:
    language: str
    tool_path: Path
    config_path: Path
    mode: str

    def stable_id(self) -> str:
        return "|".join(
            [
                self.language,
                str(self.tool_path.resolve()),
                str(self.config_path.resolve()),
                self.mode,
            ]
        )


@dataclass(frozen=True)
class FileVersion:
    mtime_ns: int
    content_sha256: str | None = None


@dataclass
class AdapterHealth:
    status: str
    message: str | None = None
    pid: int | None = None
    batch_seq: int | None = None
    started_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "pid": self.pid,
            "batch_seq": self.batch_seq,
            "started_at": self.started_at,
        }


@dataclass
class CanStartResult:
    ok: bool
    key: AdapterKey | None = None
    reason: str | None = None
    version: str | None = None


@dataclass
class QueryResponse:
    status: QueryStatus
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    batch_seq: int | None = None
    last_check_at: float | None = None
    age_ms: int | None = None
    wait_ms: int = 0
    fallback_reason: str | None = None
    backend: str = "watcher"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "diagnostics": self.diagnostics,
            "batch_seq": self.batch_seq,
            "last_check_at": self.last_check_at,
            "age_ms": self.age_ms,
            "wait_ms": self.wait_ms,
            "fallback_reason": self.fallback_reason,
            "backend": self.backend,
            "error_count": sum(1 for d in self.diagnostics if d.get("severity") == "error"),
            "warning_count": sum(
                1 for d in self.diagnostics if d.get("severity") == "warning"
            ),
        }


def file_version(path: Path) -> FileVersion:
    try:
        return FileVersion(mtime_ns=path.stat().st_mtime_ns)
    except OSError:
        return FileVersion(mtime_ns=time.time_ns())


class WatchAdapter(ABC):
    CAPABILITY: AdapterCapability
    LANGUAGE: str

    @abstractmethod
    def start(self) -> None:
        """Start background watcher work without blocking the caller."""

    @abstractmethod
    def notify_edit(self, file_path: Path, version: FileVersion) -> None:
        """Record that file_path changed at version."""

    @abstractmethod
    def query(
        self,
        file_path: Path,
        version: FileVersion,
        *,
        budget_ms: int,
    ) -> QueryResponse:
        """Return current diagnostics, waiting up to budget_ms for fresh data."""

    @abstractmethod
    def stop(self, grace_ms: int = 3000) -> None:
        """Stop the watcher and any child process."""

    @abstractmethod
    def health(self) -> AdapterHealth:
        """Return health/status information for operator-facing commands."""
