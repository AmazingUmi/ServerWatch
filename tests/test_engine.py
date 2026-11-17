from copy import deepcopy
import unittest

from serverwatch.engine import Monitor, cpu_delta


def process(pid=10, start=100, ticks=0, rss=1024):
    return {"pid": pid, "start_ticks": start, "cpu_ticks": ticks,
            "rss_bytes": rss, "uid": 1000, "user": "worker", "name": "train",
            "state": "S"}


def sample(now=0, processes=None, *, total=None, idle=None, wait=0, boot="boot-a"):
    total = int(1000 + now * 200) if total is None else total
    idle = int(500 + now * 100) if idle is None else idle
    return {
        "monotonic": now, "time": f"2026-01-01T00:00:{int(now) % 60:02d}+00:00",
        "boot_id": boot, "clock_ticks": 100, "hostname": "server",
        "uptime_seconds": 1000 + now,
        "cpu": {"total_ticks": total, "idle_ticks": idle, "iowait_ticks": wait,
                "logical_cores": 2, "load_average": [1, 0.5, 0.25],
                "cores": [{"id": "cpu0", "total_ticks": total // 2,
                           "idle_ticks": idle // 2, "iowait_ticks": wait // 2}]},
        "memory": {"total_bytes": 4096, "available_bytes": 2048, "used_bytes": 2048,
                   "swap_total_bytes": 0, "swap_used_bytes": 0},
        "processes": [] if processes is None else processes, "warnings": [],
    }


class EngineTests(unittest.TestCase):
    def test_first_sample_has_unknown_cpu_not_lifetime_average(self):
        result = Monitor().update(sample(0, [process(ticks=999999)]))
        self.assertIsNone(result["cpu"]["used_percent"])
        self.assertIsNone(result["cpu"]["cores"][0]["used_percent"])
        self.assertIsNone(result["top_cpu"][0]["cpu_percent"])
        self.assertEqual(result["recent"]["processes"], [])
        self.assertEqual(result["recent"]["observed_seconds"], 0)

    def test_cpu_single_core_basis_can_exceed_hundred_percent(self):
        monitor = Monitor()
        monitor.update(sample(0, [process(ticks=100)]))
        result = monitor.update(sample(2, [process(ticks=500)], wait=40))
        self.assertEqual(result["cpu"]["used_percent"], 40)
        self.assertEqual(result["cpu"]["iowait_percent"], 10)
        self.assertEqual(result["cpu"]["cores"][0]["used_percent"], 40)
        self.assertEqual(result["top_cpu"][0]["cpu_percent"], 200)
        self.assertEqual(result["top_cpu"][0]["memory_percent"], 25)
        self.assertEqual(result["recent"]["processes"][0]["cpu_seconds"], 4)
        self.assertEqual(result["recent"]["processes"][0]["window_cpu_percent"], 200)

    def test_reused_pid_never_inherits_old_cpu_ticks(self):
        monitor = Monitor(window=10)
        monitor.update(sample(0, [process(ticks=900)]))
        monitor.update(sample(2, [process(ticks=1000)]))
        result = monitor.update(sample(4, [process(start=300, ticks=8000)]))
        self.assertIsNone(result["top_cpu"][0]["cpu_percent"])
        old = result["recent"]["processes"][0]
        self.assertEqual(old["start_ticks"], 100)
        self.assertFalse(old["running"])
        self.assertEqual(old["cpu_seconds"], 1)
        result = monitor.update(sample(6, [process(start=300, ticks=8100)]))
        self.assertEqual({p["start_ticks"] for p in result["recent"]["processes"]}, {100, 300})

    def test_rolling_window_weights_overlap_and_expires_history(self):
        monitor = Monitor(window=5)
        monitor.update(sample(0, [process()]))
        monitor.update(sample(4, [process(ticks=400)]))
        result = monitor.update(sample(8, [process(ticks=800)]))
        recent = result["recent"]
        self.assertEqual(recent["observed_seconds"], 5)
        self.assertEqual(recent["processes"][0]["cpu_seconds"], 5)
        self.assertEqual(recent["processes"][0]["window_cpu_percent"], 100)
        # The process disappears: preserve measured activity while in the window.
        result = monitor.update(sample(10, []))
        self.assertEqual(result["recent"]["processes"][0]["cpu_seconds"], 3)
        self.assertFalse(result["recent"]["processes"][0]["running"])
        self.assertIsNone(result["recent"]["processes"][0]["cpu_percent"])
        self.assertEqual(result["recent"]["processes"][0]["last_seen"], "2026-01-01T00:00:08+00:00")
        result = monitor.update(sample(13, []))
        self.assertEqual(result["recent"]["processes"], [])
        self.assertTrue(all(end > 8 for _, end, _ in monitor.intervals))

    def test_recent_peak_memory_and_cpu_order(self):
        monitor = Monitor(top=1)
        monitor.update(sample(0, [process(10, ticks=0, rss=3000), process(11)]))
        result = monitor.update(sample(2, [process(10, ticks=50, rss=100), process(11, ticks=100)]))
        self.assertEqual(result["top_memory"][0]["pid"], 11)
        self.assertEqual(result["top_cpu"][0]["pid"], 11)
        self.assertEqual(result["recent"]["processes"][0]["pid"], 11)
        monitor = Monitor(top=2)
        monitor.update(sample(0, [process(rss=3000)]))
        result = monitor.update(sample(2, [process(ticks=50, rss=100)]))
        self.assertEqual(result["recent"]["processes"][0]["peak_rss_bytes"], 3000)

    def test_unknown_new_process_sorts_after_measured_idle(self):
        monitor = Monitor()
        monitor.update(sample(0, [process(10)]))
        result = monitor.update(sample(2, [process(10), process(11, ticks=9000, rss=9999)]))
        self.assertEqual([p["pid"] for p in result["top_cpu"]], [10, 11])
        self.assertEqual(result["top_cpu"][0]["cpu_percent"], 0)
        self.assertIsNone(result["top_cpu"][1]["cpu_percent"])

    def test_negative_process_counter_is_unknown(self):
        monitor = Monitor()
        monitor.update(sample(0, [process(ticks=100)]))
        result = monitor.update(sample(2, [process(ticks=10)]))
        self.assertIsNone(result["top_cpu"][0]["cpu_percent"])
        self.assertEqual(result["recent"]["processes"], [])

    def test_boot_and_nonincreasing_clock_reset_history(self):
        for next_sample in (sample(4, [process(ticks=999)], boot="new-boot"), sample(2, [process(ticks=999)])):
            with self.subTest(next_sample=next_sample):
                monitor = Monitor()
                monitor.update(sample(0, [process()]))
                monitor.update(sample(2, [process(ticks=100)]))
                result = monitor.update(next_sample)
                self.assertEqual(result["recent"]["processes"], [])
                self.assertEqual(result["sample_seconds"], 0)
                self.assertIsNone(result["cpu"]["used_percent"])

    def test_hotplug_core_cpu_unknown_on_first_sighting(self):
        monitor = Monitor()
        monitor.update(sample())
        new = sample(2)
        new["cpu"]["cores"].append({"id": "cpu2", "total_ticks": 500, "idle_ticks": 300, "iowait_ticks": 0})
        result = monitor.update(new)
        self.assertIsNone(result["cpu"]["cores"][1]["used_percent"])

    def test_gpu_ranking_warnings_and_inputs_not_mutated(self):
        raw = sample(0, [process()])
        raw["warnings"] = ["proc warning"]
        gpu = {"devices": [], "processes": [{"pid": 10, "memory_used_bytes": None},
                                               {"pid": 11, "memory_used_bytes": 100}],
               "warnings": ["gpu warning"]}
        originals = deepcopy((raw, gpu))
        result = Monitor().update(raw, gpu)
        self.assertEqual((raw, gpu), originals)
        self.assertEqual(result["warnings"], ["proc warning", "gpu warning"])
        self.assertEqual([p["pid"] for p in result["top_gpu"]], [11, 10])

    def test_cpu_zero_interval_reset_and_iowait_decrease(self):
        base = {"total_ticks": 100, "idle_ticks": 50, "iowait_ticks": 10}
        self.assertEqual(cpu_delta(base, base), (None, None))
        self.assertEqual(cpu_delta(base, {"total_ticks": 90, "idle_ticks": 40, "iowait_ticks": 0}), (None, None))
        self.assertEqual(cpu_delta(base, {"total_ticks": 200, "idle_ticks": 49, "iowait_ticks": 10}), (None, None))
        used, wait = cpu_delta(base, {"total_ticks": 200, "idle_ticks": 70, "iowait_ticks": 5})
        self.assertEqual((used, wait), (80, 0))


if __name__ == "__main__":
    unittest.main()
