"""Small POSIX terminal lifecycle, with no curses dependency."""
import contextlib
import os
import select
import sys


class Terminal:
    def __init__(self, enabled=True):
        self.enabled = enabled and sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb'
        self.saved = None
        self.pending = b''

    def __enter__(self):
        if self.enabled:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            try:
                tty.setcbreak(self.fd)
                sys.stdout.write('\x1b[?1049h\x1b[?25l')
                sys.stdout.flush()
            except BaseException:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
                raise
        return self

    def __exit__(self, *args):
        if self.saved is not None:
            import termios
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            finally:
                with contextlib.suppress(BrokenPipeError):
                    sys.stdout.write('\x1b[?25h\x1b[?1049l')
                    sys.stdout.flush()

    def key(self, timeout):
        if not self.enabled:
            import time
            time.sleep(max(0, timeout))
            return ''
        if not self.pending and not select.select([self.fd], [], [], max(0, timeout))[0]:
            return ''
        key = self.pending or os.read(self.fd, 1)
        self.pending = b''
        if not key:
            return '\x04'
        if key == b'\x1b':
            if select.select([self.fd], [], [], 0.04)[0]:
                following = os.read(self.fd, 1)
                if following != b'[':
                    self.pending = following
                    return '\x1b'
                key += following
                if key == b'\x1b[' and select.select([self.fd], [], [], 0.04)[0]:
                    key += os.read(self.fd, 1)
        return key.decode('ascii', errors='ignore')

    def draw(self, content):
        if self.enabled:
            # Erase each row and the tail without clearing the whole screen (less flicker).
            sys.stdout.write('\x1b[H' + '\x1b[K\r\n'.join(content.splitlines()) + '\x1b[K\x1b[J')
        else:
            sys.stdout.write(content + '\n')
        sys.stdout.flush()
