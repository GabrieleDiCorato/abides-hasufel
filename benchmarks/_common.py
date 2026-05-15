"""Shared helpers for the one-shot benchmark scripts under ``benchmarks/``.

These helpers exist only to keep each benchmark script tiny and consistent.
They are not a library; do not import them from production code.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"


def now_ns() -> int:
    """Monotonic high-resolution timestamp in nanoseconds."""
    return time.perf_counter_ns()


def git_commit() -> str:
    """Return the current git HEAD short SHA, or ``"unknown"``."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(BENCH_DIR.parent),
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


def peak_rss_bytes() -> int:
    """Best-effort peak resident set size in bytes for the current process.

    Cross-platform: tries ``resource`` (POSIX), then a Windows ``ctypes``
    call against ``psapi.GetProcessMemoryInfo``. Returns ``0`` if neither
    works (caller should treat this as "not measured").
    """
    if platform.system() != "Windows":
        try:
            import resource  # noqa: PLC0415

            rusage = resource.getrusage(resource.RUSAGE_SELF)  # type: ignore[attr-defined]
            # ru_maxrss is in KB on Linux, bytes on macOS
            multiplier = 1 if sys.platform == "darwin" else 1024
            return int(rusage.ru_maxrss) * multiplier
        except ImportError:
            return 0

    # Windows: query GetProcessMemoryInfo via ctypes
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    # K32GetProcessMemoryInfo lives in kernel32 since Windows 7 — preferred
    # because it avoids needing to load psapi.dll explicitly. Fall back to
    # psapi.GetProcessMemoryInfo if the kernel32 export is unavailable.
    try:
        get_pmi = ctypes.windll.kernel32.K32GetProcessMemoryInfo
    except AttributeError:
        try:
            get_pmi = ctypes.WinDLL("psapi.dll").GetProcessMemoryInfo
        except (OSError, AttributeError):
            return 0
    get_pmi.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    get_pmi.restype = wintypes.BOOL
    ok = get_pmi(handle, ctypes.byref(counters), counters.cb)
    if not ok:
        return 0
    return int(counters.PeakWorkingSetSize)


def time_iterations(
    fn: Callable[[], Any],
    *,
    n_warmup: int,
    n_iter: int,
) -> dict[str, Any]:
    """Run ``fn`` ``n_warmup + n_iter`` times and return latency stats.

    Stats reported over the ``n_iter`` measurement runs only:
    ``mean_ns``, ``p50_ns``, ``p99_ns``, ``min_ns``, ``max_ns``.
    """
    for _ in range(n_warmup):
        fn()

    samples_ns: list[int] = []
    for _ in range(n_iter):
        t0 = now_ns()
        fn()
        samples_ns.append(now_ns() - t0)

    samples_ns.sort()
    p99_idx = max(0, min(len(samples_ns) - 1, int(round(0.99 * len(samples_ns))) - 1))
    return {
        "n_iter": n_iter,
        "n_warmup": n_warmup,
        "mean_ns": int(statistics.mean(samples_ns)),
        "p50_ns": int(statistics.median(samples_ns)),
        "p99_ns": int(samples_ns[p99_idx]),
        "min_ns": int(samples_ns[0]),
        "max_ns": int(samples_ns[-1]),
    }


def record_result(script_name: str, payload: dict[str, Any]) -> Path:
    """Append one JSON line under ``benchmarks/results/<script_name>.jsonl``.

    The line is ``{commit, timestamp_iso, hostname, platform, **payload}``.
    Returns the result file path so callers can print it.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    line = {
        "commit": git_commit(),
        "timestamp_iso": datetime.now(UTC).isoformat(timespec="seconds"),
        "hostname": platform.node(),
        "platform": f"{platform.system()}-{platform.release()}-{platform.machine()}",
        "python": platform.python_version(),
        **payload,
    }
    out_path = RESULTS_DIR / f"{script_name}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + os.linesep)
    return out_path


def measure_peak_rss_during(fn: Callable[[], Any]) -> dict[str, Any]:
    """Run ``fn`` once under ``tracemalloc`` and report Python peak.

    Also reports OS-level peak RSS (process-wide, not isolated to ``fn``)
    so we capture the C-extension allocations too. ``elapsed_ns`` is the
    wall-clock duration of the call.
    """
    tracemalloc.start()
    t0 = now_ns()
    fn()
    elapsed_ns = now_ns() - t0
    _current, peak_traced = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "elapsed_ns": int(elapsed_ns),
        "peak_traced_python_bytes": int(peak_traced),
        "peak_rss_bytes": int(peak_rss_bytes()),
    }
