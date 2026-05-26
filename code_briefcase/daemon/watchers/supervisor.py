"""Supervisor for per-project diagnostics watcher adapters."""

from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Any

from code_briefcase.diagnostics import _detect_language

from .base import QueryResponse, QueryStatus, file_version
from .typescript import (
    TypeScriptWatchAdapter,
    can_start_typescript,
    sweep_orphan_watchers,
)

MAX_ADAPTERS_ENV = "CODE_BRIEFCASE_WATCH_DIAGNOSTICS_MAX_ADAPTERS"
LEGACY_MAX_ADAPTERS_ENV = "TLDR_WATCH_DIAGNOSTICS_MAX_ADAPTERS"


class WatchSupervisor:
    def __init__(self, project: Path) -> None:
        self.project = project.resolve()
        self._lock = threading.RLock()
        self._adapters: dict[str, TypeScriptWatchAdapter] = {}
        sweep_orphan_watchers(self.project)

    def handle(self, command: dict[str, Any]) -> dict[str, Any]:
        action = command.get("action") or "status"
        if action == "query":
            response = self.query(
                Path(str(command.get("file") or "")),
                language=command.get("language"),
                budget_ms=int(command.get("budget_ms") or 150),
            )
            return self._daemon_response(response)
        if action == "notify":
            return self.notify(
                Path(str(command.get("file") or "")),
                language=command.get("language"),
            )
        if action == "status":
            return self.status()
        if action == "start":
            file_arg = command.get("file")
            if not file_arg:
                return {
                    "status": "error",
                    "message": "Missing required parameter: file",
                }
            response = self.query(
                Path(str(file_arg)),
                language=command.get("language"),
                budget_ms=int(command.get("budget_ms") or 0),
            )
            return self._daemon_response(response)
        if action == "stop":
            return self.stop()
        return {"status": "error", "message": f"Unknown watcher action: {action}"}

    def _daemon_response(self, response: QueryResponse) -> dict[str, Any]:
        payload = response.to_dict()
        watcher_status = payload.pop("status")
        return {"status": "ok", "watcher_status": watcher_status, **payload}

    def notify(self, file_path: Path, *, language: str | None = None) -> dict[str, Any]:
        resolved = self._resolve_project_path(file_path)
        version = file_version(resolved)
        adapter_response = self._adapter_for_file(resolved, language=language)
        if isinstance(adapter_response, QueryResponse):
            return {"status": "ok", **adapter_response.to_dict()}
        adapter_response.start()
        adapter_response.notify_edit(resolved, version)
        return {
            "status": "ok",
            "watcher_status": "notified",
            "backend": "tsc-watch",
        }

    def query(
        self,
        file_path: Path,
        *,
        language: str | None = None,
        budget_ms: int = 150,
    ) -> QueryResponse:
        resolved = self._resolve_project_path(file_path)
        version = file_version(resolved)
        adapter_response = self._adapter_for_file(resolved, language=language)
        if isinstance(adapter_response, QueryResponse):
            return adapter_response
        adapter_response.start()
        adapter_response.notify_edit(resolved, version)
        return adapter_response.query(resolved, version, budget_ms=budget_ms)

    def status(self) -> dict[str, Any]:
        with self._lock:
            adapters = []
            for adapter_id, adapter in self._adapters.items():
                health = adapter.health().to_dict()
                health["id"] = adapter_id
                health["language"] = adapter.key.language
                health["config_path"] = str(adapter.key.config_path)
                health["tool_path"] = str(adapter.key.tool_path)
                adapters.append(health)
        return {
            "status": "ok",
            "watchers": adapters,
            "count": len(adapters),
        }

    def stop(self) -> dict[str, Any]:
        with self._lock:
            adapters = list(self._adapters.values())
            self._adapters.clear()
        for adapter in adapters:
            adapter.stop()
        return {"status": "ok", "stopped": len(adapters)}

    def _adapter_for_file(
        self,
        file_path: Path,
        *,
        language: str | None,
    ) -> TypeScriptWatchAdapter | QueryResponse:
        lang = language or _detect_language(str(file_path))
        if lang not in {"typescript", "javascript"}:
            return QueryResponse(
                status=QueryStatus.FALLBACK_REQUIRED,
                fallback_reason="unsupported_language",
                backend="watcher",
            )

        start = can_start_typescript(
            file_path,
            allow_js=lang == "javascript",
            project=self.project,
        )
        if not start.ok or start.key is None:
            return QueryResponse(
                status=QueryStatus.FALLBACK_REQUIRED,
                fallback_reason=start.reason or "cannot_start",
                backend="tsc-watch",
            )

        adapter_id = start.key.stable_id()
        with self._lock:
            adapter = self._adapters.get(adapter_id)
            if adapter is None:
                if len(self._adapters) >= _max_adapters():
                    return QueryResponse(
                        status=QueryStatus.FALLBACK_REQUIRED,
                        fallback_reason="watcher_limit_exceeded",
                        backend="tsc-watch",
                    )
                adapter = TypeScriptWatchAdapter(start.key, project=self.project)
                self._adapters[adapter_id] = adapter
            return adapter

    def _resolve_project_path(self, file_path: Path) -> Path:
        path = file_path.expanduser()
        if not path.is_absolute():
            path = self.project / path
        return path.resolve()


def _max_adapters() -> int:
    raw = os.environ.get(MAX_ADAPTERS_ENV) or os.environ.get(LEGACY_MAX_ADAPTERS_ENV)
    if raw is None:
        return 4
    try:
        return min(64, max(1, int(raw)))
    except ValueError:
        return 4
