import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from serverwatch.proc import CollectorError, ProcCollector, parse_process_stat


def stat_record(pid=123, name="trainer", *, utime=25, stime=5, start=900, rss=10):
    fields = ["S"] + ["0"] * 49
    fields[11], fields[12], fields[19], fields[21] = map(str, (utime, stime, start, rss))
    # Child CPU time should never be charged to the parent in the collector.
    fields[13], fields[14] = "9999", "8888"
    return f"{pid} ({name}) " + " ".join(fields)


class ProcTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "stat").write_text(
            "cpu 100 20 50 300 20 5 5 10 80 15\n"
            "cpu0 50 10 25 150 10 2 3 5 40 8\n"
            "cpu1 50 10 25 150 10 3 2 5 40 7\n"
        )
        (self.root / "meminfo").write_text(
            "MemTotal: 1000 kB\nMemAvailable: 600 kB\n"
            "MemFree: 100 kB\nBuffers: 20 kB\nCached: 300 kB\n"
            "SReclaimable: 80 kB\nShmem: 30 kB\n"
            "SwapTotal: 200 kB\nSwapFree: 150 kB\n"
        )
        (self.root / "uptime").write_text("1234.50 1500.00\n")
        (self.root / "loadavg").write_text("1.25 0.75 0.50 1/200 123\n")
        (self.root / "123").mkdir()
        (self.root / "123/stat").write_text(stat_record())
        self.collector = ProcCollector(self.root, clock_ticks=100, page_size=4096)

    def test_counters_units_and_no_guest_double_count(self):
        sample = self.collector.sample()
        self.assertEqual(sample["cpu"]["total_ticks"], 510)
        self.assertEqual(sample["cpu"]["idle_ticks"], 300)
        self.assertEqual(sample["cpu"]["iowait_ticks"], 20)
        self.assertEqual(sample["cpu"]["logical_cores"], 2)
        self.assertEqual(sample["cpu"]["cores"], [
            {"id": "cpu0", "total_ticks": 255, "idle_ticks": 150, "iowait_ticks": 10},
            {"id": "cpu1", "total_ticks": 255, "idle_ticks": 150, "iowait_ticks": 10},
        ])
        self.assertEqual(sample["cpu"]["load_average"], [1.25, 0.75, 0.5])
        self.assertEqual(sample["uptime_seconds"], 1234.5)
        self.assertEqual(sample["memory"]["used_bytes"], 400 * 1024)
        self.assertEqual(sample["memory"]["swap_used_bytes"], 50 * 1024)
        process = sample["processes"][0]
        self.assertEqual(process["cpu_ticks"], 30)
        self.assertEqual(process["rss_bytes"], 10 * 4096)
        self.assertEqual(process["start_ticks"], 900)
        self.assertEqual(process["uid"], os.getuid())
        self.assertNotIn("cmdline", process)
        self.assertEqual(sample["warnings"], [])

    def test_parentheses_spaces_newlines_in_process_name(self):
        process = parse_process_stat(stat_record(name="a ) tricky (\nname)"))
        self.assertEqual(process["name"], "a ) tricky (\nname)")
        self.assertEqual(process["cpu_ticks"], 30)
        self.assertEqual(process["rss_pages"], 10)

    def test_truncated_and_invalid_stat_rejected(self):
        for text in ("123 abc S 1", "123 (name) S 1", "0 (x) " + "0 " * 30):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_process_stat(text)

    def test_memory_legacy_estimate_is_explicit(self):
        path = self.root / "meminfo"
        path.write_text(path.read_text().replace("MemAvailable: 600 kB\n", ""))
        sample = self.collector.sample()
        self.assertEqual(sample["memory"]["available_bytes"], 470 * 1024)
        self.assertIn("estimate", " ".join(sample["warnings"]))

    def test_missing_memory_optional_fields_not_zero(self):
        (self.root / "meminfo").write_text("MemTotal: 1000 kB\n")
        sample = self.collector.sample()
        for key in ("available_bytes", "used_bytes", "swap_total_bytes", "swap_used_bytes"):
            self.assertIsNone(sample["memory"][key])
        self.assertEqual(len(sample["warnings"]), 2)

    def test_exited_process_ignored_denied_process_disclosed(self):
        (self.root / "124").mkdir()
        original = self.collector._process
        def process(pid):
            if pid == "123":
                raise PermissionError(errno.EACCES, "permission denied")
            return original(pid)
        with patch.object(self.collector, "_process", side_effect=process):
            sample = self.collector.sample()
        self.assertEqual(sample["processes"], [])
        self.assertEqual(sample["warnings"], ["Permission denied for 1 process(es); rankings are partial"])

    def test_malformed_record_and_mismatched_pid_not_reported(self):
        (self.root / "123/stat").write_text(stat_record(pid=999))
        sample = self.collector.sample()
        self.assertEqual(sample["processes"], [])
        self.assertIn("Skipped 1", sample["warnings"][0])

    def test_missing_optional_system_metrics_are_null(self):
        (self.root / "loadavg").write_text("nan 0 0")
        (self.root / "uptime").write_text("")
        sample = self.collector.sample()
        self.assertIsNone(sample["cpu"]["load_average"])
        self.assertIsNone(sample["uptime_seconds"])
        self.assertEqual(len(sample["warnings"]), 2)

    def test_missing_mandatory_metrics_raise_clear_error(self):
        (self.root / "stat").unlink()
        with self.assertRaisesRegex(CollectorError, "Cannot read.*stat"):
            self.collector.sample()

    def test_invalid_mandatory_metrics_raise_clear_error(self):
        (self.root / "stat").write_text("cpu 1 2 invalid 3\ncpu0 1 2 3 4\n")
        with self.assertRaisesRegex(CollectorError, "Invalid CPU"):
            self.collector.sample()
        (self.root / "stat").write_text("cpu 1 2 3 4\ncpu0 1 2 3 4\n")
        (self.root / "meminfo").write_text("MemTotal: 0 kB\n")
        with self.assertRaisesRegex(CollectorError, "Invalid MemTotal"):
            self.collector.sample()

    def test_username_lookup_cached(self):
        with patch("serverwatch.proc.pwd.getpwuid", side_effect=KeyError) as lookup:
            sample = self.collector.sample()
            self.collector.sample()
        self.assertEqual(sample["processes"][0]["user"], str(os.getuid()))
        self.assertEqual(lookup.call_count, 1)

    def test_pinned_process_directory_owner_and_stat(self):
        original_open = os.open
        def observe_open(path, flags, *args, **kwargs):
            if path == "stat":
                self.assertIsInstance(kwargs["dir_fd"], int)
            return original_open(path, flags, *args, **kwargs)
        with patch("serverwatch.proc.os.open", side_effect=observe_open) as mocked:
            self.collector.sample()
        self.assertEqual(mocked.call_count, 2)


if __name__ == "__main__":
    unittest.main()
