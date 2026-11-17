import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from serverwatch.gpu import GpuCollector, _number


def command_result(output="", returncode=0):
    return subprocess.CompletedProcess([], returncode, output, "driver details")


class NvidiaTests(unittest.TestCase):
    def collector(self, backend="nvidia"):
        with patch("serverwatch.gpu.shutil.which", return_value="/usr/bin/nvidia-smi"):
            return GpuCollector(backend)

    def test_csv_units_and_compute_process_mapping(self):
        collector = self.collector()
        devices = '0, GPU-test, "NVIDIA, Test", 81, 512.5, 8192, 64, 125.50\n'
        processes = 'GPU-test, 1234, "python,worker", 128\n'
        with patch("serverwatch.gpu.subprocess.run", side_effect=[command_result(devices), command_result(processes)]) as run:
            result = collector.sample()
        self.assertEqual(result["warnings"], [])
        device = result["devices"][0]
        self.assertEqual(device["name"], "NVIDIA, Test")
        self.assertEqual(device["memory_used_bytes"], int(512.5 * 1024**2))
        self.assertEqual(device["utilization_percent"], 81)
        self.assertEqual(device["power_w"], 125.5)
        process = result["processes"][0]
        self.assertEqual(process["gpu_id"], "nvidia:0")
        self.assertEqual(process["memory_used_bytes"], 128 * 1024**2)
        self.assertEqual(process["name"], "python,worker")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["timeout"], 1.5)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_unavailable_and_invalid_numbers_remain_null(self):
        collector = self.collector()
        devices = "0, GPU-a, A100, N/A, [Not Supported], 8192, NaN, inf\n"
        processes = "MIG-unmapped, 12, python, N/A\n"
        with patch("serverwatch.gpu.subprocess.run", side_effect=[command_result(devices), command_result(processes)]):
            result = collector.sample()
        for key in ("utilization_percent", "memory_used_bytes", "temperature_c", "power_w"):
            self.assertIsNone(result["devices"][0][key])
        self.assertIsNone(result["processes"][0]["memory_used_bytes"])
        self.assertEqual(result["processes"][0]["gpu_id"], "MIG-unmapped")

    def test_no_gpu_and_explicit_missing_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("serverwatch.gpu.shutil.which", return_value=None):
                auto = GpuCollector("auto", directory)
                explicit = GpuCollector("nvidia", directory)
            self.assertEqual(auto.sample(), {"devices": [], "processes": [], "warnings": []})
            self.assertIn("not found", explicit.sample()["warnings"][0])

    def test_timeout_and_nonzero_exit_do_not_break_sampling(self):
        collector = self.collector()
        with patch("serverwatch.gpu.subprocess.run", side_effect=subprocess.TimeoutExpired("nvidia-smi", 1.5)) as run:
            result = collector.sample()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["devices"], [])
        self.assertIn("timed out", result["warnings"][0])
        with patch("serverwatch.gpu.subprocess.run", return_value=command_result(returncode=9)):
            self.assertIn("exit 9", collector.sample()["warnings"][0])

    def test_invalid_rows_skipped_and_process_failure_preserves_devices(self):
        collector = self.collector()
        devices = "not csv\n0, GPU-a, A100, 20, 100, 8192, 35, 40\n"
        with patch("serverwatch.gpu.subprocess.run", side_effect=[command_result(devices), command_result(returncode=3)]):
            result = collector.sample()
        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(result["processes"], [])
        self.assertEqual(len(result["warnings"]), 2)

    def test_malformed_csv_is_reported(self):
        collector = self.collector()
        with patch("serverwatch.gpu.subprocess.run", return_value=command_result('0, "unterminated')):
            result = collector.sample()
        self.assertEqual(result["devices"], [])
        self.assertIn("malformed CSV", result["warnings"][0])


class AmdTests(unittest.TestCase):
    def test_sysfs_units_and_connector_filtering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "class/drm/card2/device"
            hwmon = device / "hwmon/hwmon0"
            hwmon.mkdir(parents=True)
            values = {"vendor": "0x1002", "product_name": "AMD Test", "gpu_busy_percent": "73",
                      "mem_info_vram_used": "1073741824", "mem_info_vram_total": "8589934592"}
            for name, value in values.items():
                (device / name).write_text(value)
            (hwmon / "temp1_input").write_text("55500")
            (hwmon / "power1_average").write_text("143500000")
            connector = root / "class/drm/card2-DP-1/device"
            connector.mkdir(parents=True)
            (connector / "vendor").write_text("0x1002")
            result = GpuCollector("amd", root).sample()
        self.assertEqual(len(result["devices"]), 1)
        gpu = result["devices"][0]
        self.assertEqual(gpu["id"], "amd:card2")
        self.assertEqual(gpu["temperature_c"], 55.5)
        self.assertEqual(gpu["power_w"], 143.5)
        self.assertEqual(gpu["memory_used_bytes"], 1024**3)
        self.assertEqual(gpu["utilization_percent"], 73)
        self.assertIn("per-process", result["warnings"][0])

    def test_missing_fields_and_power_input_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / "class/drm/card0/device"
            hwmon = device / "hwmon/hwmon9"
            hwmon.mkdir(parents=True)
            (device / "vendor").write_text("0x1002")
            (device / "gpu_busy_percent").write_text("123")
            (hwmon / "power1_input").write_text("25000000")
            result = GpuCollector("amd", directory).sample()
        gpu = result["devices"][0]
        self.assertIsNone(gpu["memory_used_bytes"])
        self.assertIsNone(gpu["utilization_percent"])
        self.assertIsNone(gpu["temperature_c"])
        self.assertEqual(gpu["power_w"], 25)

    def test_none_backend_does_no_probe(self):
        with patch("serverwatch.gpu.shutil.which") as which:
            collector = GpuCollector("none")
        with patch("serverwatch.gpu.subprocess.run") as run:
            self.assertEqual(collector.sample(), {"devices": [], "processes": [], "warnings": []})
        which.assert_not_called()
        run.assert_not_called()

    def test_validation(self):
        for timeout in (0, -1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                GpuCollector("none", timeout=timeout)
        with self.assertRaises(ValueError):
            GpuCollector("invalid")
        self.assertIsNone(_number("-1"))


if __name__ == "__main__":
    unittest.main()
