#!/usr/bin/env python3
"""Measure proc collector + rolling-window costs against synthetic regular files.

This is a local regression aid, not a Linux procfs or GPU performance claim.
Fixture creation and simulated counter updates are excluded from timing.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import platform
import resource
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from serverwatch.engine import Monitor
from serverwatch.proc import ProcCollector


def create_fixture(root: Path, processes: int) -> None:
    (root / "stat").write_text(
        "cpu 10000 0 1000 20000 0 0 0 0 0 0\n"
        + "".join(f"cpu{i} 625 0 62 1250 0 0 0 0 0 0\n" for i in range(16)),
    )
    (root / "meminfo").write_text(
        "MemTotal: 33554432 kB\nMemAvailable: 16777216 kB\n"
        "SwapTotal: 0 kB\nSwapFree: 0 kB\n",
    )
    (root / "uptime").write_text("20000.00 120000.00\n")
    (root / "loadavg").write_text("2.0 1.0 0.5 1/1000 9999\n")
    for pid in range(100, processes + 100):
        directory = root / str(pid)
        directory.mkdir()
        fields = ["S"] + ["0"] * 49
        fields[11], fields[12], fields[19], fields[21] = "100", "0", "100", "12800"
        (directory / "stat").write_text(f"{pid} (synthetic-worker) " + " ".join(fields))


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "median_ms": round(statistics.median(values) * 1000, 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processes", type=int, default=1000)
    parser.add_argument("--samples", type=int, default=40)
    args = parser.parse_args()
    if args.processes < 1 or args.samples < 1:
        parser.error("--processes and --samples must be positive")
    interval, window = 2.0, 60.0
    warmup_samples = math.ceil(window / interval) + 1
    collect_times, engine_times, cpu_times = [], [], []
    with tempfile.TemporaryDirectory(prefix="serverwatch-benchmark-") as temporary:
        root = Path(temporary)
        create_fixture(root, args.processes)
        collector = ProcCollector(root, clock_ticks=100, page_size=4096)
        monitor = Monitor(window=window, top=5)
        for step in range(warmup_samples + args.samples):
            start_cpu = time.process_time()
            start = time.perf_counter()
            raw = collector.sample()
            elapsed_collect = time.perf_counter() - start
            used_cpu = time.process_time() - start_cpu
            # Advance simulated time and activity; no sleep or fixture rewrite.
            raw["monotonic"] = 100 + step * interval
            raw["cpu"]["total_ticks"] += step * 3200
            raw["cpu"]["idle_ticks"] += step * 1600
            for process in raw["processes"]:
                process["cpu_ticks"] += step
            start_cpu = time.process_time()
            start = time.perf_counter()
            monitor.update(raw)
            elapsed_engine = time.perf_counter() - start
            used_cpu += time.process_time() - start_cpu
            # Fill the whole history window before measuring sustained costs.
            if step >= warmup_samples:
                collect_times.append(elapsed_collect)
                engine_times.append(elapsed_engine)
                cpu_times.append(used_cpu)
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak_rss *= 1024
    result = {
        "kind": "synthetic regular-file fixture; excludes GPU and rendering",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processes": args.processes,
        "measured_samples": len(collect_times),
        "warmup_samples": warmup_samples,
        "simulated_interval_seconds": interval,
        "window_seconds": window,
        "collector": distribution(collect_times),
        "engine": distribution(engine_times),
        "mean_cpu_ms_per_sample": round(statistics.mean(cpu_times) * 1000, 3),
        "estimated_one_core_percent_at_interval": round(statistics.mean(cpu_times) / interval * 100, 3),
        "process_peak_rss_mib_including_fixture_setup": round(peak_rss / 1024**2, 2),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
