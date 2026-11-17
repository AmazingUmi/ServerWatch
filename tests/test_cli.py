import contextlib
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from serverwatch import cli
from serverwatch.engine import Monitor
from serverwatch.proc import CollectorError
from serverwatch.state import read_state, state_lock, write_state
from test_engine import process, sample

PROJECT = Path(__file__).resolve().parents[1]


def current_snapshot():
    monitor = Monitor()
    monitor.update(sample(0, [process()]))
    result = monitor.update(sample(2, [process(ticks=150)]))
    result["time"] = datetime.now(timezone.utc).isoformat()
    return result


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "status.json"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "serverwatch", *map(str, args)],
                              cwd=PROJECT, text=True, capture_output=True, timeout=8)

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_module_help_and_version(self):
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("watch", help_result.stdout)
        self.assertIn("serve", help_result.stdout)
        version = self.run_cli("--version")
        self.assertEqual(version.returncode, 0)
        self.assertTrue(version.stdout.startswith("ServerWatch "))

    def test_status_fresh_snapshot_exact_json_stdout(self):
        write_state(self.path, current_snapshot())
        result = self.run_cli("status", "--state", self.path, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        loaded = json.loads(result.stdout)
        self.assertEqual(loaded["top_cpu"][0]["cpu_percent"], 75)
        self.assertFalse(loaded["stale"])
        self.assertIn("state_age_seconds", loaded)

    def test_status_stale_has_usable_json_and_exit_two(self):
        data = current_snapshot()
        data["time"] = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        write_state(self.path, data)
        result = self.run_cli("status", "--state", self.path, "--json", "--max-age", "1")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(json.loads(result.stdout)["stale"])
        self.assertEqual(result.stderr, "")

    def test_status_malformed_and_missing_cache_are_clean_errors(self):
        for content in (None, "{", '{"schema_version": 99}', 'null'):
            with self.subTest(content=content):
                if content is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(content)
                result = self.run_cli("status", "--state", self.path)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.startswith("serverwatch:"))
                self.assertNotIn("Traceback", result.stderr)

    def test_numeric_flags_reject_unbounded_and_nonfinite_settings(self):
        invalid = (("--interval", "0"), ("--interval", "nan"), ("--interval", "inf"),
                   ("--top", "0"), ("--top", "101"), ("--top", "1.2"),
                   ("--window", "0.1", "--interval", "1"),
                   ("--window", "3600", "--interval", "0.1"))
        for flags in invalid:
            with self.subTest(flags=flags):
                result = self.run_cli("once", *flags)
                self.assertEqual(result.returncode, 2)
                self.assertIn("error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_once_samples_interval_cpu_and_emits_only_json(self):
        with patch.object(cli.sys, "platform", "linux"), patch.object(cli, "ProcCollector") as proc, \
             patch.object(cli, "GpuCollector") as gpu, patch.object(cli.time, "sleep") as sleep:
            proc.return_value.sample.side_effect = [sample(0, [process()]), sample(2, [process(ticks=250)])]
            gpu.return_value.sample.return_value = {"devices": [], "processes": [], "warnings": []}
            code, stdout, stderr = self.invoke(["once", "--interval", "2", "--gpu", "none", "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.splitlines()), 1)
        result = json.loads(stdout)
        self.assertEqual(result["top_cpu"][0]["cpu_percent"], 125)
        self.assertEqual(result["sample_seconds"], 2)
        self.assertEqual(result["gpu"]["age_seconds"], 0)
        sleep.assert_called_once_with(2.0)

    def test_nonlinux_collection_and_collector_failure_are_actionable(self):
        with patch.object(cli.sys, "platform", "darwin"):
            code, stdout, stderr = self.invoke(["once", "--gpu", "none"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("requires Linux", stderr)
        with patch.object(cli.sys, "platform", "linux"), patch.object(cli, "ProcCollector", side_effect=CollectorError("no proc mount")):
            code, stdout, stderr = self.invoke(["once"])
        self.assertEqual(code, 2)
        self.assertIn("no proc mount", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_serve_writes_sample_cache_and_releases_lock_on_exit(self):
        term = MagicMock()
        term.enabled = False
        term.__enter__.return_value = term
        term.key.side_effect = ["", "q"]
        gpu_instance = MagicMock()
        gpu_instance.poll.return_value = {"devices": [], "processes": [], "warnings": []}
        gpu_instance.wait_seconds.return_value = 2.0
        with patch.object(cli.sys, "platform", "linux"), patch.object(cli, "Terminal", return_value=term), \
             patch.object(cli, "GpuSampler", return_value=gpu_instance), patch.object(cli, "ProcCollector") as proc, \
             patch.object(cli.time, "monotonic", side_effect=[0, 0, 0, 0, 0, 2, 2, 2, 2]):
            proc.return_value.sample.side_effect = [sample(0, [process()]), sample(2, [process(ticks=100)])]
            code, stdout, stderr = self.invoke(["serve", "--state", str(self.path), "--gpu", "none"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(read_state(self.path)["top_cpu"][0]["cpu_percent"], 50)
        gpu_instance.close.assert_called_once()
        with state_lock(self.path):
            pass

    def test_cached_watch_ignores_collection_window_constraints(self):
        with patch.object(cli, "loop", return_value=0) as loop:
            code, stdout, stderr = self.invoke(["watch", "--state", str(self.path),
                                               "--interval", "120", "--window", "1"])
        self.assertEqual(code, 0, stderr)
        loop.assert_called_once()

    def test_gpu_delayed_consumption_keeps_measurement_age_and_deadline(self):
        pool = MagicMock()
        first, second = Future(), Future()
        pool.submit.side_effect = [first, second]
        with patch.object(cli, "ThreadPoolExecutor", return_value=pool), patch.object(cli, "GpuCollector"):
            sampler = cli.GpuSampler("none", interval=5)
        self.assertIsNone(sampler.poll(10)["age_seconds"])
        first.set_result(({"devices": [], "processes": [], "warnings": []}, 10, "measurement-start"))
        result = sampler.poll(18)
        self.assertEqual(result["age_seconds"], 8)
        self.assertEqual(result["sampled_at"], "measurement-start")
        self.assertEqual(sampler.next_time, 15)
        self.assertEqual(pool.submit.call_count, 2, "overdue GPU query should restart immediately")
        self.assertEqual(sampler.wait_seconds(18), 0.1)
        second.set_result(({"devices": [], "processes": [], "warnings": []}, 19, "second-start"))
        self.assertEqual(sampler.poll(19.5)["age_seconds"], 0.5)
        self.assertEqual(sampler.wait_seconds(21), 3)
        sampler.close()
        pool.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

    def test_gpu_collect_timestamps_before_collector_work(self):
        with patch.object(cli, "ThreadPoolExecutor"), patch.object(cli, "GpuCollector") as collector:
            sampler = cli.GpuSampler("none", 5)
        collector.return_value.sample.return_value = {"devices": [], "processes": [], "warnings": []}
        with patch.object(cli.time, "monotonic", return_value=12.5) as clock:
            data, measured_at, stamp = sampler.collect()
        self.assertEqual(measured_at, 12.5)
        self.assertIsNotNone(datetime.fromisoformat(stamp).tzinfo)
        self.assertEqual(data["devices"], [])
        clock.assert_called_once()

    def test_gpu_deadline_wakes_independently_of_long_cpu_interval(self):
        now, gpu_started, waits = [0.0], [], []
        term = MagicMock()
        term.enabled = False
        term.__enter__.return_value = term
        def wait(timeout):
            waits.append(timeout)
            now[0] += timeout
            return "q" if now[0] >= 5.1 else ""
        term.key.side_effect = wait
        def collect():
            gpu_started.append(now[0])
            return {"devices": [], "processes": [], "warnings": []}
        pool = MagicMock()
        def submit(function):
            future = Future()
            future.set_result(function())
            return future
        pool.submit.side_effect = submit
        with patch.object(cli.sys, "platform", "linux"), patch.object(cli, "Terminal", return_value=term), \
             patch.object(cli, "ThreadPoolExecutor", return_value=pool), patch.object(cli, "GpuCollector") as gpu, \
             patch.object(cli, "ProcCollector") as proc, patch.object(cli.time, "monotonic", side_effect=lambda: now[0]):
            gpu.return_value.sample.side_effect = collect
            proc.return_value.sample.return_value = sample()
            code, stdout, stderr = self.invoke(["serve", "--state", str(self.path), "--interval", "60", "--window", "60",
                                               "--gpu-interval", "5", "--gpu", "none"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(gpu_started, [0.0, 5.0])
        self.assertEqual(proc.return_value.sample.call_count, 1)
        self.assertEqual(waits, [0.1, 4.9, 0.1])

    def test_serve_write_error_closes_sampler_and_releases_lock(self):
        term = MagicMock()
        term.enabled = False
        term.__enter__.return_value = term
        gpu_instance = MagicMock()
        gpu_instance.poll.return_value = {"devices": [], "processes": [], "warnings": []}
        gpu_instance.wait_seconds.return_value = 2.0
        with patch.object(cli.sys, "platform", "linux"), patch.object(cli, "Terminal", return_value=term), \
             patch.object(cli, "GpuSampler", return_value=gpu_instance), patch.object(cli, "ProcCollector") as proc, \
             patch.object(cli, "write_state", side_effect=PermissionError("cache is read-only")):
            proc.return_value.sample.return_value = sample()
            code, stdout, stderr = self.invoke(["serve", "--state", str(self.path), "--gpu", "none"])
        self.assertEqual(code, 2)
        self.assertIn("cache is read-only", stderr)
        gpu_instance.close.assert_called_once()
        with state_lock(self.path):
            pass


if __name__ == "__main__":
    unittest.main()
