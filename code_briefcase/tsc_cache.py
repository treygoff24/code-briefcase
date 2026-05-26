"""Persistent TypeScript buildinfo cache shared by diagnostics and watchers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import TracebackType
from typing import Any, BinaryIO, TypedDict, cast

from .command_exec import expand_shebang_command

DEFAULT_TSC_CACHE_MAX_BYTES = 500 * 1024 * 1024
DEFAULT_LOCK_TIMEOUT_SECONDS = 1.0
WATCHER_RECENCY_NS = 30 * 60 * 1_000_000_000
TSC_CACHE_ROOT_ENV = "CODE_BRIEFCASE_TSC_CACHE_ROOT"
TSC_CACHE_MAX_MB_ENV = "CODE_BRIEFCASE_TSC_CACHE_MAX_MB"


@dataclass(frozen=True)
class TscCachePaths:
    """Canonical Phase 0/Phase 1 cache paths for one TypeScript config key."""

    root: Path
    project_dir: Path
    config_dir: Path
    config_path: Path
    buildinfo_path: Path
    meta_path: Path
    lock_path: Path


class CacheEntry(TypedDict):
    path: Path
    meta: dict[str, Any]
    last_use_ns: int
    size: int


class CacheLock:
    """Small cross-process exclusive lock around a cache config directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None
        self.acquired = False

    def acquire(self, *, timeout_seconds: float | None = None) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        deadline = None
        if timeout_seconds is not None:
            deadline = time.monotonic() + timeout_seconds

        while True:
            if self._try_lock():
                self.acquired = True
                return True
            if deadline is not None and time.monotonic() >= deadline:
                self.release()
                return False
            time.sleep(0.025)

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if self.acquired:
                self._unlock()
        finally:
            self.acquired = False
            self._file.close()
            self._file = None

    def _try_lock(self) -> bool:
        if self._file is None:
            return False
        if os.name == "nt":
            import msvcrt

            msvcrt_module = cast(Any, msvcrt)
            try:
                msvcrt_module.locking(self._file.fileno(), msvcrt_module.LK_NBLCK, 1)
                return True
            except OSError:
                return False

        import fcntl

        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(self) -> None:
        if self._file is None:
            return
        if os.name == "nt":
            import msvcrt

            msvcrt_module = cast(Any, msvcrt)
            self._file.seek(0)
            msvcrt_module.locking(self._file.fileno(), msvcrt_module.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> "CacheLock":
        if not self.acquire(timeout_seconds=None):
            raise RuntimeError(f"failed to acquire lock: {self.path}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class CachedTsconfig:
    """Compatibility wrapper matching tempfile.TemporaryDirectory's surface."""

    def __init__(
        self,
        paths: TscCachePaths,
        lock: CacheLock,
        *,
        tsc_version: str,
        tsconfig_mtime: int,
        prune_after_cleanup: bool = True,
    ) -> None:
        self.paths = paths
        self.name = str(paths.config_dir)
        self._lock = lock
        self._tsc_version = tsc_version
        self._tsconfig_mtime = tsconfig_mtime
        self._prune_after_cleanup = prune_after_cleanup
        self._cleaned = False

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            write_phase0_meta(
                self.paths,
                tsc_version=self._tsc_version,
                tsconfig_mtime=self._tsconfig_mtime,
            )
        finally:
            self._lock.release()
        if self._prune_after_cleanup:
            prune_tsc_cache()


def tsc_cache_root() -> Path:
    override = os.environ.get(TSC_CACHE_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".code-briefcase" / "cache" / "tsc"


def configured_tsc_cache_max_bytes() -> int:
    raw = os.environ.get(TSC_CACHE_MAX_MB_ENV)
    if raw is None:
        return DEFAULT_TSC_CACHE_MAX_BYTES
    try:
        return max(0, int(raw)) * 1024 * 1024
    except ValueError:
        return DEFAULT_TSC_CACHE_MAX_BYTES


# Keyed on (resolved_path, mtime_ns) so a node_modules rebuild that swaps the
# binary invalidates the cached version automatically. Forking tsc --version
# costs ~10 ms; this lives on the post-edit hot path so memoization matters.
_TSC_VERSION_CACHE: dict[tuple[str, int], str] = {}


def tsc_version(tsc_path: str) -> str | None:
    """Return tsc's --version string, or None if probing failed.

    None signals callers to skip the persistent cache rather than write a
    placeholder string into the cache key — a placeholder would orphan the
    entry forever once the real version starts resolving.
    """
    try:
        resolved = str(Path(tsc_path).resolve())
        mtime = Path(resolved).stat().st_mtime_ns
    except OSError:
        return None

    cache_key = (resolved, mtime)
    cached = _TSC_VERSION_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        result = subprocess.run(
            expand_shebang_command([tsc_path, "--version"]),
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    first_line = output.strip().splitlines()[0] if output.strip() else ""
    if not first_line:
        return None

    _TSC_VERSION_CACHE[cache_key] = first_line
    return first_line


def phase0_cache_paths(
    project_config: Path,
    tsc_path: str,
    version: str,
    *,
    root: Path | None = None,
) -> TscCachePaths:
    cache_root = root or tsc_cache_root()
    project_root = project_config.parent.resolve()
    config_abs = project_config.resolve()
    tsc_abs = Path(tsc_path).resolve()
    projhash = hashlib.sha256(str(project_root).encode()).hexdigest()[:8]
    key_material = f"{config_abs}\0{version}\0{tsc_abs}"
    configkey = hashlib.sha256(key_material.encode()).hexdigest()[:12]
    config_dir = cache_root / projhash / configkey
    return TscCachePaths(
        root=cache_root,
        project_dir=cache_root / projhash,
        config_dir=config_dir,
        config_path=config_dir / "tsconfig.json",
        buildinfo_path=config_dir / "buildinfo",
        meta_path=config_dir / "meta.json",
        lock_path=config_dir / "lockfile",
    )


def prepare_phase0_single_file_tsconfig(
    project_config: Path,
    target_file: Path,
    *,
    tsc_path: str,
    allow_js: bool,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> CachedTsconfig | None:
    version = tsc_version(tsc_path)
    if version is None:
        # Version probe failed; fall back to ephemeral tempdir rather than
        # write a sentinel ("unknown") into the cache key and orphan the dir.
        return None
    paths = phase0_cache_paths(project_config, tsc_path, version)
    paths.config_dir.mkdir(parents=True, exist_ok=True)

    lock = CacheLock(paths.lock_path)
    if not lock.acquire(timeout_seconds=lock_timeout_seconds):
        return None

    tsconfig_mtime = project_config.stat().st_mtime_ns
    try:
        # Fresh entry = no meta.json yet. Prune is worth running on cache
        # misses (cache just grew); skip it on hits to keep the hot path cheap.
        is_new_entry = not paths.meta_path.exists()
        meta = read_meta(paths.meta_path)
        if _cache_invalid(meta, tsc_version=version, tsconfig_mtime=tsconfig_mtime):
            _remove_buildinfo(paths)

        _write_single_file_config(
            paths,
            project_config=project_config,
            target_file=target_file,
            allow_js=allow_js,
        )
        write_phase0_meta(
            paths,
            tsc_version=version,
            tsconfig_mtime=tsconfig_mtime,
        )
        return CachedTsconfig(
            paths,
            lock,
            tsc_version=version,
            tsconfig_mtime=tsconfig_mtime,
            prune_after_cleanup=is_new_entry,
        )
    except Exception:
        lock.release()
        raise


def read_meta(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return cast(dict[str, Any], data)


def write_phase0_meta(
    paths: TscCachePaths,
    *,
    tsc_version: str,
    tsconfig_mtime: int,
    last_use_ns: int | None = None,
) -> None:
    payload = {
        "tsc_version": tsc_version,
        "tsconfig_mtime": tsconfig_mtime,
        "last_use_ns": last_use_ns or time.time_ns(),
        "owner": "phase0",
        # Capture size at write time so prune doesn't have to rglob every
        # cache dir to enforce the LRU budget. Falls back to a real scan in
        # _cache_entries if the field is missing (older entry or write race).
        "size_bytes": _dir_size(paths.config_dir),
    }
    _atomic_write_json_line(paths.meta_path, payload)


def prune_tsc_cache(
    max_bytes: int | None = None, *, force: bool = False
) -> dict[str, int | str]:
    root = tsc_cache_root()
    if not root.exists():
        return {"root": str(root), "removed": 0, "skipped": 0, "bytes_removed": 0}

    limit = configured_tsc_cache_max_bytes() if max_bytes is None else max_bytes
    if force:
        return _remove_all_cache_dirs(root)

    entries = _cache_entries(root)
    total = sum(entry["size"] for entry in entries)
    removed = 0
    skipped = 0
    bytes_removed = 0

    for entry in sorted(entries, key=lambda item: item["last_use_ns"]):
        if total <= limit:
            break
        path = entry["path"]
        lock = CacheLock(path / "lockfile")
        if not lock.acquire(timeout_seconds=0):
            skipped += 1
            continue
        try:
            if _is_recent_watcher(entry["meta"]):
                skipped += 1
                continue
            size = entry["size"]
            shutil.rmtree(path, ignore_errors=True)
            total -= size
            removed += 1
            bytes_removed += size
        finally:
            lock.release()

    _remove_empty_project_dirs(root)
    return {
        "root": str(root),
        "removed": removed,
        "skipped": skipped,
        "bytes_removed": bytes_removed,
    }


def clean_tsc_cache(*, force: bool = False) -> dict[str, int | str]:
    return prune_tsc_cache(max_bytes=0, force=force)


def _cache_invalid(
    meta: dict[str, Any], *, tsc_version: str, tsconfig_mtime: int
) -> bool:
    if not meta:
        return False
    return (
        meta.get("tsc_version") != tsc_version
        or meta.get("tsconfig_mtime") != tsconfig_mtime
    )


def _write_single_file_config(
    paths: TscCachePaths,
    *,
    project_config: Path,
    target_file: Path,
    allow_js: bool,
) -> None:
    compiler_options: dict[str, object] = {
        "incremental": True,
        "noEmit": True,
        "tsBuildInfoFile": str(paths.buildinfo_path),
    }
    if allow_js:
        compiler_options["allowJs"] = True

    payload: dict[str, object] = {
        "extends": str(project_config.resolve()),
        "compilerOptions": compiler_options,
        "files": [str(target_file.resolve())],
        "include": [],
    }
    text = json.dumps(payload, indent=2) + "\n"
    _write_text_if_changed(paths.config_path, text)


def _write_text_if_changed(path: Path, text: str) -> None:
    try:
        if path.read_text() == text:
            return
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _atomic_write_json_line(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        Path(tmp_name).replace(path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _remove_buildinfo(paths: TscCachePaths) -> None:
    try:
        paths.buildinfo_path.unlink()
    except FileNotFoundError:
        pass


def _cache_entries(root: Path) -> list[CacheEntry]:
    entries: list[CacheEntry] = []
    for meta_path in root.glob("*/*/meta.json"):
        config_dir = meta_path.parent
        meta = read_meta(meta_path)
        cached_size = meta.get("size_bytes")
        size = (
            int(cached_size) if isinstance(cached_size, int) else _dir_size(config_dir)
        )
        entries.append(
            {
                "path": config_dir,
                "meta": meta,
                "last_use_ns": int(meta.get("last_use_ns") or 0),
                "size": size,
            }
        )
    return entries


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _is_recent_watcher(meta: dict[str, Any]) -> bool:
    if meta.get("owner") != "watcher":
        return False
    last_use_ns = int(meta.get("last_use_ns") or 0)
    return time.time_ns() - last_use_ns < WATCHER_RECENCY_NS


def _remove_all_cache_dirs(root: Path) -> dict[str, int | str]:
    bytes_removed = _dir_size(root)
    removed = sum(1 for path in root.glob("*/*") if path.is_dir())
    shutil.rmtree(root, ignore_errors=True)
    return {
        "root": str(root),
        "removed": removed,
        "skipped": 0,
        "bytes_removed": bytes_removed,
    }


def _remove_empty_project_dirs(root: Path) -> None:
    for project_dir in list(root.glob("*")):
        try:
            if project_dir.is_dir() and not any(project_dir.iterdir()):
                project_dir.rmdir()
        except OSError:
            continue
