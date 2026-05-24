import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tldr import diagnostics as diag
from tldr import tsc_cache


def _fake_tsc(path: Path, args_file: Path, make_executable, version: str = "") -> Path:
    version_expr = version or "${FAKE_TSC_VERSION:-Version 5.9.0}"
    return make_executable(
        path,
        f"""#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "{version_expr}"
  exit 0
fi
printf '%s\\n' "$@" > {args_file}
exit 0
""",
    )


def _single_file_project(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "src" / "sample.ts"
    source.parent.mkdir()
    source.write_text("const answer: string = 42;\n")
    tsconfig = tmp_path / "tsconfig.json"
    tsconfig.write_text('{"compilerOptions":{"strict":true}}\n')
    return source, tsconfig


def _project_arg(args_file: Path) -> Path:
    args = args_file.read_text().splitlines()
    return Path(args[args.index("--project") + 1])


def test_phase0_cache_reuses_stable_config_and_buildinfo(
    tmp_path, monkeypatch, make_executable
):
    cache_root = tmp_path / "home-cache" / "tsc"
    monkeypatch.setenv("TLDR_TSC_CACHE_ROOT", str(cache_root))
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source, tsconfig = _single_file_project(tmp_path)
    args_file = tmp_path / "tsc-args.txt"
    _fake_tsc(tmp_path / "node_modules" / ".bin" / "tsc", args_file, make_executable)

    first = diag.get_diagnostics(str(source), language="typescript", include_lint=False)
    first_config = _project_arg(args_file)
    second = diag.get_diagnostics(str(source), language="typescript", include_lint=False)
    second_config = _project_arg(args_file)

    assert first["tools"] == ["tsc"]
    assert second["tools"] == ["tsc"]
    assert first_config == second_config
    assert str(first_config).startswith(str(cache_root))
    assert first_config.name == "tsconfig.json"

    payload = json.loads(first_config.read_text())
    assert payload["extends"] == str(tsconfig.resolve())
    assert payload["files"] == [str(source.resolve())]
    assert payload["compilerOptions"]["incremental"] is True
    assert payload["compilerOptions"]["tsBuildInfoFile"] == str(
        first_config.parent / "buildinfo"
    )

    meta = json.loads((first_config.parent / "meta.json").read_text())
    assert meta["owner"] == "phase0"
    assert meta["tsc_version"] == "Version 5.9.0"
    assert meta["tsconfig_mtime"] == tsconfig.stat().st_mtime_ns


def test_phase0_cache_invalidates_buildinfo_on_tsconfig_mtime_change(
    tmp_path, monkeypatch, make_executable
):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TLDR_TSC_CACHE_ROOT", str(cache_root))
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source, tsconfig = _single_file_project(tmp_path)
    args_file = tmp_path / "tsc-args.txt"
    _fake_tsc(tmp_path / "node_modules" / ".bin" / "tsc", args_file, make_executable)

    diag.get_diagnostics(str(source), language="typescript", include_lint=False)
    config_path = _project_arg(args_file)
    buildinfo = config_path.parent / "buildinfo"
    buildinfo.write_text("old build info")

    time.sleep(0.01)
    tsconfig.write_text('{"compilerOptions":{"strict":false}}\n')
    diag.get_diagnostics(str(source), language="typescript", include_lint=False)

    assert _project_arg(args_file) == config_path
    assert not buildinfo.exists()
    meta = json.loads((config_path.parent / "meta.json").read_text())
    assert meta["tsconfig_mtime"] == tsconfig.stat().st_mtime_ns


def test_phase0_cache_key_changes_with_tsc_version(
    tmp_path, monkeypatch, make_executable
):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TLDR_TSC_CACHE_ROOT", str(cache_root))
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source, _tsconfig = _single_file_project(tmp_path)
    args_file = tmp_path / "tsc-args.txt"
    tsc_path = _fake_tsc(
        tmp_path / "node_modules" / ".bin" / "tsc", args_file, make_executable
    )

    monkeypatch.setenv("FAKE_TSC_VERSION", "Version 5.8.0")
    diag.get_diagnostics(str(source), language="typescript", include_lint=False)
    first_config = _project_arg(args_file)

    # Simulate an npm install replacing the binary: bump mtime so the version
    # probe re-runs (the probe is memoized on (path, mtime) to keep the hot
    # path cheap; env-only version changes don't model reality).
    monkeypatch.setenv("FAKE_TSC_VERSION", "Version 5.9.0")
    new_mtime_ns = tsc_path.stat().st_mtime_ns + 1_000_000_000
    os.utime(tsc_path, ns=(new_mtime_ns, new_mtime_ns))
    diag.get_diagnostics(str(source), language="typescript", include_lint=False)
    second_config = _project_arg(args_file)

    assert first_config != second_config
    assert first_config.parent.parent == second_config.parent.parent
    assert first_config.exists()
    assert second_config.exists()


def test_phase0_lock_contention_falls_back_to_ephemeral_config(
    tmp_path, monkeypatch, make_executable
):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TLDR_TSC_CACHE_ROOT", str(cache_root))

    source, tsconfig = _single_file_project(tmp_path)
    args_file = tmp_path / "tsc-args.txt"
    tsc = _fake_tsc(
        tmp_path / "node_modules" / ".bin" / "tsc",
        args_file,
        make_executable,
        version="Version 5.9.0",
    )
    paths = tsc_cache.phase0_cache_paths(tsconfig, str(tsc), "Version 5.9.0")
    ready = tmp_path / "lock-ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path\n"
                "import sys, time\n"
                "from tldr.tsc_cache import CacheLock\n"
                "lock = CacheLock(Path(sys.argv[1]))\n"
                "if not lock.acquire(timeout_seconds=0):\n"
                "    raise SystemExit(1)\n"
                "Path(sys.argv[2]).write_text('ready')\n"
                "time.sleep(1.4)\n"
                "lock.release()\n"
            ),
            str(paths.lock_path),
            str(ready),
        ],
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        deadline = time.time() + 2
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.025)
        assert ready.exists()

        started = time.monotonic()
        config_ctx = diag._write_single_file_tsconfig(
            tsconfig,
            source,
            allow_js=False,
            tsc_path=str(tsc),
        )
        try:
            elapsed = time.monotonic() - started
            assert elapsed >= 0.9
            assert not str(config_ctx.name).startswith(str(cache_root))
        finally:
            config_ctx.cleanup()
    finally:
        holder.terminate()
        holder.wait(timeout=3)


def test_prune_removes_phase0_entries_but_skips_recent_watchers(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TLDR_TSC_CACHE_ROOT", str(cache_root))
    now = time.time_ns()

    phase0_dir = cache_root / "proj" / "phase0"
    watcher_dir = cache_root / "proj" / "watcher"
    for directory in (phase0_dir, watcher_dir):
        directory.mkdir(parents=True)
        (directory / "buildinfo").write_text("x" * 100)
        (directory / "lockfile").touch()

    (phase0_dir / "meta.json").write_text(
        json.dumps(
            {
                "tsc_version": "Version 5.9.0",
                "tsconfig_mtime": 1,
                "last_use_ns": now - 10_000_000_000,
                "owner": "phase0",
            }
        )
        + "\n"
    )
    (watcher_dir / "meta.json").write_text(
        json.dumps(
            {
                "tsc_version": "Version 5.9.0",
                "tsconfig_mtime": 1,
                "last_use_ns": now,
                "owner": "watcher",
            }
        )
        + "\n"
    )

    result = tsc_cache.clean_tsc_cache(force=False)

    assert result["removed"] == 1
    assert not phase0_dir.exists()
    assert watcher_dir.exists()
    assert result["skipped"] == 1


def test_prune_skips_locked_cache_dirs(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TLDR_TSC_CACHE_ROOT", str(cache_root))
    locked_dir = cache_root / "proj" / "locked"
    locked_dir.mkdir(parents=True)
    (locked_dir / "buildinfo").write_text("x" * 100)
    (locked_dir / "meta.json").write_text(
        json.dumps(
            {
                "tsc_version": "Version 5.9.0",
                "tsconfig_mtime": 1,
                "last_use_ns": 1,
                "owner": "phase0",
            }
        )
        + "\n"
    )

    lock = tsc_cache.CacheLock(locked_dir / "lockfile")
    assert lock.acquire(timeout_seconds=0)
    try:
        result = tsc_cache.prune_tsc_cache(max_bytes=0)
    finally:
        lock.release()

    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert locked_dir.exists()


def test_cache_clean_force_ignores_recent_watcher_skip(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TLDR_TSC_CACHE_ROOT", str(cache_root))
    watcher_dir = cache_root / "proj" / "watcher"
    watcher_dir.mkdir(parents=True)
    (watcher_dir / "buildinfo").write_text("x" * 100)
    (watcher_dir / "lockfile").touch()
    (watcher_dir / "meta.json").write_text(
        json.dumps(
            {
                "tsc_version": "Version 5.9.0",
                "tsconfig_mtime": 1,
                "last_use_ns": time.time_ns(),
                "owner": "watcher",
            }
        )
        + "\n"
    )

    result = tsc_cache.clean_tsc_cache(force=True)

    assert result["removed"] == 1
    assert not watcher_dir.exists()


def test_cache_clean_cli_prunes_tsc_cache(tmp_path):
    cache_root = tmp_path / "cache"
    entry = cache_root / "proj" / "entry"
    entry.mkdir(parents=True)
    (entry / "buildinfo").write_text("x" * 100)
    (entry / "lockfile").touch()
    (entry / "meta.json").write_text(
        json.dumps(
            {
                "tsc_version": "Version 5.9.0",
                "tsconfig_mtime": 1,
                "last_use_ns": 1,
                "owner": "phase0",
            }
        )
        + "\n"
    )

    env = os.environ.copy()
    env["TLDR_TSC_CACHE_ROOT"] = str(cache_root)
    home = tmp_path / "home"
    home.mkdir()
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, "-m", "tldr.cli", "cache", "clean", "--json"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["removed"] == 1
    assert not entry.exists()


def test_tsc_version_is_memoized_until_binary_mtime_changes(
    tmp_path, monkeypatch, make_executable
):
    tsc_cache._TSC_VERSION_CACHE.clear()
    calls = tmp_path / "call-count"
    calls.write_text("0")
    tsc = make_executable(
        tmp_path / "node_modules" / ".bin" / "tsc",
        f"""#!/bin/sh
n=$(cat {calls})
echo $((n + 1)) > {calls}
echo "Version 5.9.$n"
""",
    )

    first = tsc_cache.tsc_version(str(tsc))
    second = tsc_cache.tsc_version(str(tsc))
    assert first == second == "Version 5.9.0"
    assert calls.read_text().strip() == "1"

    new_mtime_ns = tsc.stat().st_mtime_ns + 1_000_000_000
    os.utime(tsc, ns=(new_mtime_ns, new_mtime_ns))

    third = tsc_cache.tsc_version(str(tsc))
    assert third == "Version 5.9.1"
    assert calls.read_text().strip() == "2"


def test_unknown_tsc_version_returns_none_and_skips_cache(
    tmp_path, monkeypatch, make_executable
):
    tsc_cache._TSC_VERSION_CACHE.clear()
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TLDR_TSC_CACHE_ROOT", str(cache_root))

    source, tsconfig = _single_file_project(tmp_path)
    # tsc that prints nothing → version probe returns None → callers fall back
    # to the legacy tempdir path rather than write "unknown" into the cache key.
    tsc = make_executable(
        tmp_path / "node_modules" / ".bin" / "tsc",
        "#!/bin/sh\nexit 0\n",
    )

    assert tsc_cache.tsc_version(str(tsc)) is None
    cached = tsc_cache.prepare_phase0_single_file_tsconfig(
        tsconfig, source, tsc_path=str(tsc), allow_js=False
    )
    assert cached is None
    assert not cache_root.exists()


def test_prune_uses_size_bytes_from_meta_without_rescanning(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TLDR_TSC_CACHE_ROOT", str(cache_root))
    entry = cache_root / "proj" / "entry"
    entry.mkdir(parents=True)
    # Real bytes on disk are 1; the cached size_bytes says 100 — prune should
    # trust the meta and act as if the entry costs 100 bytes.
    (entry / "buildinfo").write_text("x")
    (entry / "lockfile").touch()
    (entry / "meta.json").write_text(
        json.dumps(
            {
                "tsc_version": "Version 5.9.0",
                "tsconfig_mtime": 1,
                "last_use_ns": 1,
                "owner": "phase0",
                "size_bytes": 100,
            }
        )
        + "\n"
    )

    entries = tsc_cache._cache_entries(cache_root)
    assert len(entries) == 1
    assert entries[0]["size"] == 100

    # Force prune to act: budget below the cached size triggers eviction.
    result = tsc_cache.prune_tsc_cache(max_bytes=50)
    assert result["removed"] == 1
    assert not entry.exists()


def test_phase0_cache_hit_skips_prune_side_effect(
    tmp_path, monkeypatch, make_executable
):
    tsc_cache._TSC_VERSION_CACHE.clear()
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TLDR_TSC_CACHE_ROOT", str(cache_root))

    source, tsconfig = _single_file_project(tmp_path)
    args_file = tmp_path / "tsc-args.txt"
    tsc = _fake_tsc(
        tmp_path / "node_modules" / ".bin" / "tsc",
        args_file,
        make_executable,
        version="Version 5.9.0",
    )

    # Prime the cache.
    first = tsc_cache.prepare_phase0_single_file_tsconfig(
        tsconfig, source, tsc_path=str(tsc), allow_js=False
    )
    assert first is not None
    first.cleanup()

    # Second call hits the existing entry; should NOT trigger prune.
    prune_calls = []
    monkeypatch.setattr(
        tsc_cache,
        "prune_tsc_cache",
        lambda *a, **kw: prune_calls.append((a, kw)) or {},
    )
    second = tsc_cache.prepare_phase0_single_file_tsconfig(
        tsconfig, source, tsc_path=str(tsc), allow_js=False
    )
    assert second is not None
    second.cleanup()
    assert prune_calls == []
