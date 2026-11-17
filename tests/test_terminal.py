import contextlib
import fcntl
import io
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

from serverwatch.render import cell_width
from serverwatch.state import write_state
from serverwatch.terminal import Terminal
from test_cli import current_snapshot

PROJECT = Path(__file__).resolve().parents[1]


class TtyStream(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        return 42


class TerminalUnitTests(unittest.TestCase):
    def test_key_decoding_arrow_escape_and_eof(self):
        terminal = Terminal(enabled=False)
        terminal.enabled = True
        terminal.fd = 42
        with patch("serverwatch.terminal.select.select", return_value=([42], [], [])), \
             patch("serverwatch.terminal.os.read", side_effect=[b"\x1b", b"[", b"B"]):
            self.assertEqual(terminal.key(0.1), "\x1b[B")
        with patch("serverwatch.terminal.select.select", side_effect=[([42], [], []), ([], [], [])]), \
             patch("serverwatch.terminal.os.read", return_value=b"\x1b"):
            self.assertEqual(terminal.key(0.1), "\x1b")
        with patch("serverwatch.terminal.select.select", return_value=([42], [], [])), \
             patch("serverwatch.terminal.os.read", return_value=b""):
            self.assertEqual(terminal.key(0.1), "\x04", "EOF must end watch rather than busy-loop")

    def test_escape_preserves_immediately_following_printable_key(self):
        for following in (b"0", b"q"):
            with self.subTest(following=following):
                terminal = Terminal(enabled=False)
                terminal.enabled = True
                terminal.fd = 42
                with patch("serverwatch.terminal.select.select", return_value=([42], [], [])), \
                     patch("serverwatch.terminal.os.read", side_effect=[b"\x1b", following]) as read:
                    self.assertEqual(terminal.key(0.1), "\x1b")
                    self.assertEqual(terminal.key(0.1), following.decode("ascii"))
                self.assertEqual(read.call_count, 2, "pending input must not require another read")

    def test_terminal_restores_attributes_and_cursor_after_exception(self):
        stdin, stdout = TtyStream(), TtyStream()
        with patch("serverwatch.terminal.sys.stdin", stdin), patch("serverwatch.terminal.sys.stdout", stdout), \
             patch.dict(os.environ, {"TERM": "xterm"}), patch("termios.tcgetattr", return_value=["saved"]) as get, \
             patch("termios.tcsetattr") as restore, patch("tty.setcbreak") as cbreak:
            with self.assertRaisesRegex(RuntimeError, "test exception"):
                with Terminal() as terminal:
                    self.assertTrue(terminal.enabled)
                    raise RuntimeError("test exception")
        cbreak.assert_called_once_with(42)
        restore.assert_called_once_with(42, termios.TCSADRAIN, ["saved"])
        self.assertIn("\x1b[?1049h\x1b[?25l", stdout.getvalue())
        self.assertTrue(stdout.getvalue().endswith("\x1b[?25h\x1b[?1049l"))

    def test_dumb_terminal_and_piped_output_do_not_enter_raw_mode(self):
        with patch("serverwatch.terminal.sys.stdin", TtyStream()), patch("serverwatch.terminal.sys.stdout", TtyStream()), \
             patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertFalse(Terminal().enabled)
        with patch("serverwatch.terminal.sys.stdin", TtyStream()), patch("serverwatch.terminal.sys.stdout", io.StringIO()):
            self.assertFalse(Terminal().enabled)


@unittest.skipUnless(os.name == "posix", "PTY integration requires POSIX")
class TerminalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "status.json"
        data = current_snapshot()
        data["cpu"]["cores"] = [{"id": f"cpu{i}", "used_percent": float(i), "iowait_percent": 0}
                                  for i in range(32)]
        write_state(self.path, data)
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.original_attrs = termios.tcgetattr(self.slave)
        self.resize(100, 24)
        env = dict(os.environ, TERM="xterm-256color", NO_COLOR="1")
        env.pop("COLUMNS", None)
        env.pop("LINES", None)
        self.child = subprocess.Popen([sys.executable, "-B", "-m", "serverwatch", "watch", "--state", str(self.path),
                                       "--interval", "0.1", "--max-age", "3600", "--no-color"],
                                      stdin=self.slave, stdout=self.slave, stderr=self.slave, cwd=PROJECT, env=env)
        self.addCleanup(self.stop_child)
        self.output = b""
        self.until(b"\x1b[J")

    def stop_child(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=3)

    def resize(self, columns, rows):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def until(self, marker, start=0, timeout=5):
        deadline = time.monotonic() + timeout
        while marker not in self.output[start:]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"Timed out waiting for {marker!r}; output tail={self.output[-500:]!r}")
            if select.select([self.master], [], [], min(remaining, 0.2))[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    self.fail(f"PTY ended waiting for {marker!r}; output tail={self.output[-500:]!r}")
                self.output += chunk
        return self.output[start:]

    def send(self, key, expected):
        start = len(self.output)
        os.write(self.master, key)
        return self.until(expected, start)

    def assert_terminal_restored(self):
        restored = termios.tcgetattr(self.slave)
        original = list(self.original_attrs)
        # macOS may set the transient PENDIN bit as queued PTY input changes;
        # it is not a user terminal mode controlled by ServerWatch.
        transient = getattr(termios, "PENDIN", 0)
        restored[3] &= ~transient
        original[3] &= ~transient
        self.assertEqual(restored, original)

    def test_navigation_resize_quit_and_termios_restore(self):
        self.assertIn(b"\x1b[?1049h", self.output)
        for key, panel in ((b"1", b"CPU"), (b"2", b"MEMORY"), (b"3", b"GPU"),
                           (b"4", b"TASKS"), (b"0", b"OVERVIEW"), (b"1", b"CPU"), (b"\x1b", b"OVERVIEW")):
            self.send(key, b"|  " + panel)
        self.send(b"1", b"|  CPU")
        for key in (b"j", b"k", b"\x1b[B", b"\x1b[A"):
            self.send(key, b"\x1b[J")
        start = len(self.output)
        self.resize(35, 10)
        os.kill(self.child.pid, signal.SIGWINCH)
        resized = self.until(b"\x1b[J", start)
        # A concurrent old frame may have arrived just before resize. Wait for
        # another fresh frame and check the last complete rendered screen.
        start = len(self.output)
        resized = self.until(b"\x1b[J", start)
        frame = resized.split(b"\x1b[H")[-1].split(b"\x1b[J")[0]
        plain = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", frame).decode("utf-8").replace("\r", "")
        self.assertLessEqual(len(plain.splitlines()), 10)
        self.assertTrue(all(cell_width(line) <= 34 for line in plain.splitlines()), plain)
        self.send(b"q", b"\x1b[?1049l")
        self.assertEqual(self.child.wait(timeout=3), 0)
        self.assert_terminal_restored()
        self.assertIn(b"\x1b[?25h", self.output)

    def test_sigterm_restores_terminal(self):
        start = len(self.output)
        self.child.terminate()
        self.until(b"\x1b[?1049l", start)
        self.assertEqual(self.child.wait(timeout=3), 0)
        self.assert_terminal_restored()


if __name__ == "__main__":
    unittest.main()
