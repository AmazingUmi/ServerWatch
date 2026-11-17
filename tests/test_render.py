import re
import unittest

from serverwatch.render import cell_width, clip, render, safe


def snapshot():
    proc = {"pid": 1234, "user": "alice", "name": "python-worker", "cpu_percent": 201.5,
            "rss_bytes": 1024**3, "cpu_seconds": 54.5, "window_cpu_percent": 110.0,
            "peak_rss_bytes": 2 * 1024**3, "running": True}
    return {"hostname": "compute-01", "time": "2026-11-17T09:20:00Z", "uptime_seconds": 90000,
            "sample_seconds": 2, "cpu": {"used_percent": 45.2, "iowait_percent": 2.1, "logical_cores": 32,
              "load_average": [5.2, 4.1, 2.6], "cores": [{"id": f"cpu{i}", "used_percent": i * 3.0} for i in range(32)]},
            "memory": {"used_bytes": 16 * 1024**3, "total_bytes": 64 * 1024**3,
              "available_bytes": 48 * 1024**3, "swap_used_bytes": 0, "swap_total_bytes": 4 * 1024**3},
            "gpu": {"devices": [{"id": f"nvidia:{i}", "name": "NVIDIA Test", "utilization_percent": 85,
              "memory_used_bytes": 4 * 1024**3, "memory_total_bytes": 24 * 1024**3, "temperature_c": 65, "power_w": 180} for i in range(4)],
              "age_seconds": 1.2}, "recent": {"window_seconds": 60, "observed_seconds": 49.5, "processes": [proc]},
            "top_cpu": [proc], "top_memory": [proc], "top_gpu": [{"pid": 1234, "gpu_id": "nvidia:0", "name": "python", "memory_used_bytes": 4 * 1024**3}],
            "process_count": 123, "warnings": []}


class RenderTests(unittest.TestCase):
    def test_overview_compact_recent_rank_and_gpu_summary(self):
        result = render(snapshot())
        for text in ("ServerWatch", "compute-01", "OVERVIEW", "CPU [1]", "MEMORY [2]", "GPU [3]",
                     "TOP TASKS", "recent CPU", "python-worker", "201.5", "4 GPUs"):
            self.assertIn(text, result)
        for text in ("Load 1/5/15m", "I/O wait", "Swap", "CPU(s)", "GPU sample age", "100 = one core"):
            self.assertNotIn(text, result)
        self.assertNotIn("\x1b", result)
        self.assertLess(len(result.splitlines()), 21)

    def test_overview_partial_gpu_metrics_do_not_become_complete_totals(self):
        data = snapshot()
        for device, used in zip(data["gpu"]["devices"], (2, 3, 4, 5)):
            device["memory_used_bytes"] = used * 1024**3
        complete = render(data)
        self.assertIn("14.0/96.0GiB", complete)
        data["gpu"]["devices"][1]["memory_used_bytes"] = None
        data["gpu"]["devices"][1]["utilization_percent"] = None
        incomplete = render(data)
        self.assertIn("4 GPUs  N/A", incomplete)
        self.assertNotIn("11.0/96.0GiB", incomplete)
        self.assertNotIn("85.0%", incomplete)
        self.assertIn("85.0%", render(data, "gpu"))

    def test_overview_narrow_layout_keeps_all_resource_sections(self):
        result = render(snapshot(), width=35, height=24, interactive=True)
        for resource in ("CPU [1]", "MEMORY [2]", "GPU [3]"):
            self.assertIn(resource, result)
        self.assertIn("python-worker", result)
        self.assertTrue(all(cell_width(line) <= 35 for line in result.splitlines()))

    def test_all_panels_fit_tiny_and_normal_terminals(self):
        for panel in ("overview", "cpu", "memory", "gpu", "tasks"):
            for width, height in ((35, 10), (80, 24), (100, 30), (1, 1), (12, 4)):
                with self.subTest(panel=panel, width=width, height=height):
                    result = render(snapshot(), panel, width, height, interactive=True)
                    self.assertLessEqual(len(result.splitlines()), height)
                    self.assertTrue(all(cell_width(line) <= width for line in result.splitlines()))
                    self.assertNotIn("\x1b", result)
                    self.assertNotRegex(result, r"\[[.#?]+\]")

    def test_secondary_panels_have_distinct_details(self):
        data = snapshot()
        cpu = render(data, "cpu")
        for metric in ("LOGICAL CORES", "cpu31", "45.2%", "2.1%", "5.20", "4.10", "2.60", "201.5"):
            self.assertIn(metric, cpu)
        memory = render(data, "memory")
        for metric in ("25.0%", "64.0GiB", "48.0GiB", "4.0GiB", "1.0GiB", "python-worker"):
            self.assertIn(metric, memory)
        for width in (50, 60):
            with self.subTest(panel="memory", width=width):
                narrow = render(data, "memory", width=width)
                for metric in ("16.0GiB", "64.0GiB", "48.0GiB", "4.0GiB"):
                    self.assertIn(metric, narrow)
        gpu = render(data, "gpu")
        for metric in ("nvidia:3", "85.0%", "65C", "180.0W", "24.0GiB", "VRAM", "1234"):
            self.assertIn(metric, gpu)
        tasks = render(data, "tasks")
        for metric in ("PEAK RSS", "AVG%", "49.5", "60", "54.50", "110.0", "2.0GiB", "python-worker"):
            self.assertIn(metric, tasks)
        for panel in ("overview", "cpu", "memory", "gpu", "tasks"):
            with self.subTest(panel=panel):
                result = render(data, panel)
                self.assertNotRegex(result, r"\[[.#?]+\]")
                self.assertIn("09:20:00 UTC", result)
                self.assertIn("─", result)
                self.assertNotIn("2026-11-17T", result)

    def test_scroll_preserves_header_footer_and_reveals_later_rows(self):
        first = render(snapshot(), "cpu", 80, 10, interactive=True)
        last = render(snapshot(), "cpu", 80, 10, interactive=True, scroll=999)
        self.assertEqual(first.splitlines()[:3], last.splitlines()[:3])
        self.assertEqual(first.splitlines()[-1], last.splitlines()[-1])
        self.assertIn("Rows", first)
        self.assertNotIn("python-worker", first)
        self.assertIn("python-worker", last)

    def test_escape_injection_and_wide_unicode_are_safe(self):
        data = snapshot()
        data["hostname"] = "evil\x1b[2J\r\n服务器\x9b31m"
        data["top_cpu"][0]["name"] = "任务\u202e\x1b]0;title\x07" * 10
        data["warnings"] = ["danger\x1b[?25l\nnext"]
        result = render(data, width=35)
        self.assertNotIn("\x1b", result)
        self.assertNotIn("\x9b", result)
        self.assertNotIn("\u202e", result)
        self.assertTrue(all(cell_width(line) <= 35 for line in result.splitlines()))
        self.assertEqual(cell_width("服务器"), 6)
        self.assertEqual(clip("服务器", 5), "服务~")
        self.assertEqual(safe("a\tb\nc"), "a b c")

    def test_colors_are_added_after_clipping(self):
        data = snapshot()
        plain = render(data, width=35, height=10)
        colored = render(data, width=35, height=10, color=True)
        self.assertIn("\x1b[", colored)
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", colored), plain)

    def test_unknown_metrics_and_no_processes(self):
        data = {"cpu": {}, "memory": {}, "gpu": {}, "recent": {}}
        for panel in ("overview", "cpu", "memory", "gpu", "tasks"):
            result = render(data, panel)
            self.assertIn("N/A", result)
        self.assertIn("TOP TASKS", render(data))
        self.assertRegex(render(data), r"(?i)(no.*(?:process|task)|waiting)")
        self.assertRegex(render(data, "gpu"), r"(?i)no.*telemetry")
        self.assertEqual(render(data, height=0), "")

    def test_unavailable_optional_proc_metrics_render_as_na(self):
        data = snapshot()
        data["cpu"]["load_average"] = None
        data["uptime_seconds"] = None
        for field in ("available_bytes", "used_bytes", "swap_total_bytes", "swap_used_bytes"):
            data["memory"][field] = None
        for panel in ("overview", "cpu", "memory", "gpu", "tasks"):
            with self.subTest(panel=panel):
                result = render(data, panel)
                self.assertIn("up N/A", result)
                self.assertNotIn("None", result)
                if panel == "cpu":
                    self.assertIn("LOAD", result)
                    self.assertNotIn("5.20", result)
                if panel == "memory":
                    self.assertIn("AVAILABLE", result)
                    self.assertIn("SWAP", result)
                    self.assertNotIn("48.0GiB", result)
                    self.assertNotRegex(result, r"(?<![\d.])4\.0GiB\b")
                    self.assertNotIn("Disabled", result)

    def test_overview_notices_are_compact_but_detail_is_complete(self):
        data = snapshot()
        data["warnings"] = ["driver issue", "permission issue"]
        self.assertIn("2 notices", render(data))
        self.assertNotIn("driver issue", render(data))
        self.assertIn("driver issue", render(data, "gpu"))
        self.assertIn("permission issue", render(data, "gpu"))

    def test_stale_and_ended_process_are_explicit(self):
        data = snapshot()
        data["stale"] = True
        data["state_age_seconds"] = 75.2
        data["recent"]["processes"][0]["running"] = False
        result = render(data, "tasks")
        self.assertIn("STALE", result)
        self.assertIn("cache 75.2s old", result)
        self.assertIn("ended", result)
        self.assertIn("ended", render(data))
        self.assertIn("STALE", render(data))
        data["hostname"] = "very-long-hostname-" * 10
        for width in (35, 50):
            with self.subTest(width=width):
                self.assertIn("STALE", render(data, width=width).splitlines()[0])


if __name__ == "__main__":
    unittest.main()
