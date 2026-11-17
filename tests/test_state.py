from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from serverwatch.engine import Monitor
from serverwatch.state import default_state, read_state, state_lock, write_state
from test_engine import process, sample


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "private/status.json"
        monitor = Monitor()
        monitor.update(sample(0, [process()]))
        self.snapshot = monitor.update(sample(2, [process(ticks=100)]))
        self.snapshot["time"] = datetime.now(timezone.utc).isoformat()

    def test_private_file_directory_and_roundtrip(self):
        write_state(self.path, self.snapshot)
        loaded = read_state(self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertFalse(loaded.pop("stale"))
        self.assertLess(abs(loaded.pop("state_age_seconds")), 2)
        self.assertEqual(loaded, self.snapshot)

    def test_failed_serialization_preserves_previous_atomic_snapshot(self):
        write_state(self.path, self.snapshot)
        original = self.path.read_bytes()
        broken = deepcopy(self.snapshot)
        broken["cpu"]["used_percent"] = float("nan")
        with self.assertRaises(ValueError):
            write_state(self.path, broken)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".status-*")), [])

    def test_failed_replace_preserves_previous_and_cleans_temp(self):
        write_state(self.path, self.snapshot)
        original = self.path.read_bytes()
        with patch("serverwatch.state.os.replace", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                write_state(self.path, self.snapshot)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".status-*")), [])

    def test_replace_symlink_does_not_overwrite_target(self):
        self.path.parent.mkdir()
        victim = self.root / "victim"
        victim.write_text("keep")
        self.path.symlink_to(victim)
        write_state(self.path, self.snapshot)
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(self.path.is_symlink())

    def test_lock_is_exclusive_and_released_after_exception(self):
        with state_lock(self.path):
            with self.assertRaisesRegex(RuntimeError, "already writing"):
                with state_lock(self.path):
                    self.fail("second writer acquired lock")
            self.assertEqual(stat.S_IMODE(Path(str(self.path) + ".lock").stat().st_mode), 0o600)
        with self.assertRaisesRegex(ValueError, "test"):
            with state_lock(self.path):
                raise ValueError("test")
        with state_lock(self.path):
            pass

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires O_NOFOLLOW")
    def test_lock_refuses_symlink(self):
        self.path.parent.mkdir()
        victim = self.root / "victim"
        victim.write_text("keep")
        Path(str(self.path) + ".lock").symlink_to(victim)
        with self.assertRaises(OSError):
            with state_lock(self.path):
                self.fail("symlink lock accepted")
        self.assertEqual(victim.read_text(), "keep")

    def test_stale_and_future_clock_are_disclosed(self):
        for offset in (-100, 100):
            with self.subTest(offset=offset):
                self.snapshot["time"] = (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()
                write_state(self.path, self.snapshot)
                self.assertTrue(read_state(self.path, max_age=15)["stale"])

    def test_naive_timestamp_and_wrong_schema_rejected(self):
        for key, value in (("time", "2026-01-01T00:00:00"), ("schema_version", 99), ("cpu", []), ("top_cpu", {})):
            with self.subTest(key=key):
                broken = deepcopy(self.snapshot)
                broken[key] = value
                write_state(self.path, broken)
                with self.assertRaises(ValueError):
                    read_state(self.path)

    def test_truncated_invalid_and_oversized_json_rejected(self):
        self.path.parent.mkdir()
        for text in ("{", "null", "[]", " " * (8 * 1024 * 1024 + 1)):
            with self.subTest(size=len(text)):
                self.path.write_text(text)
                with self.assertRaises(ValueError):
                    read_state(self.path)

    def test_incomplete_nested_schema_rejected_before_render(self):
        for key, value in (("cpu", {}), ("memory", {}), ("recent", {}), ("top_cpu", [None]),
                           ("gpu", {"devices": [None], "processes": [], "warnings": []})):
            with self.subTest(key=key, value=value):
                broken = deepcopy(self.snapshot)
                broken[key] = value
                write_state(self.path, broken)
                with self.assertRaises(ValueError):
                    read_state(self.path)

    def test_nonfinite_json_numbers_rejected(self):
        self.path.parent.mkdir()
        for number in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(number=number):
                broken = deepcopy(self.snapshot)
                broken["cpu"]["used_percent"] = number
                self.path.write_text(json.dumps(broken))
                with self.assertRaises(ValueError):
                    read_state(self.path)

    def test_default_path_respects_xdg_runtime_then_cache(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/123", "XDG_CACHE_HOME": "/tmp/cache"}):
            self.assertEqual(default_state(), Path("/run/user/123/serverwatch/status.json"))
        with patch.dict(os.environ, {"XDG_CACHE_HOME": "/tmp/cache"}):
            os.environ.pop("XDG_RUNTIME_DIR", None)
            self.assertEqual(default_state(), Path("/tmp/cache/serverwatch/status.json"))


if __name__ == "__main__":
    unittest.main()
