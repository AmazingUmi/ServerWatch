"""ServerWatch command line and sampling loop."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import time

from . import __version__
from .engine import Monitor
from .gpu import GpuCollector
from .proc import ProcCollector, CollectorError
from .render import render
from .state import default_state, read_state, state_lock, write_state
from .terminal import Terminal

PANELS = ('overview', 'cpu', 'memory', 'gpu', 'tasks')


def positive(value):
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be a number') from exc
    if not math.isfinite(result) or result < 0.1 or result > 3600:
        raise argparse.ArgumentTypeError('must be between 0.1 and 3600 seconds')
    return result


def top_count(value):
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be an integer') from exc
    if not 1 <= result <= 100:
        raise argparse.ArgumentTypeError('must be between 1 and 100')
    return result


def parser():
    p = argparse.ArgumentParser(prog='serverwatch', description='Lightweight Linux server monitor with two-level terminal panels.')
    p.add_argument('--version', action='version', version='ServerWatch ' + __version__)
    sub = p.add_subparsers(dest='command', required=True)
    for command, help_text in [('watch', 'live interactive dashboard'), ('once', 'sample one snapshot'), ('serve', 'write a private snapshot cache'), ('status', 'read the service snapshot')]:
        s = sub.add_parser(command, help=help_text)
        s.add_argument('--json', action='store_true', help='JSON snapshot (NDJSON for watch)')
        s.add_argument('--no-color', action='store_true')
        s.add_argument('--panel', choices=PANELS, default='overview')
        if command in ('watch', 'serve', 'status'):
            s.add_argument('--state', type=Path, default=default_state() if command != 'watch' else None,
                           help='cache path; watch with this flag reads the service cache')
        if command in ('watch', 'status'):
            s.add_argument('--max-age', type=positive, default=15.0, help='cache staleness threshold in seconds')
        if command != 'status':
            s.add_argument('--interval', type=positive, default=2.0, help='CPU sampling interval in seconds (default: 2)')
            s.add_argument('--window', type=positive, default=60.0, help='recent CPU window in seconds (default: 60)')
            s.add_argument('--top', type=top_count, default=5, help='rows retained per ranking (default: 5)')
            s.add_argument('--gpu', choices=('auto', 'nvidia', 'amd', 'none'), default='auto')
            s.add_argument('--gpu-interval', type=positive, default=5.0, help='GPU refresh interval in seconds (default: 5)')
    return p


def emit(snapshot, args, terminal=None, panel=None, scroll=0):
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(',', ':')), flush=True)
        return
    size = shutil.get_terminal_size((100, 30))
    interactive = bool(terminal and terminal.enabled)
    content = render(snapshot, panel=panel or args.panel, width=max(1, size.columns - (1 if interactive else 0)),
                     height=size.lines if interactive else None,
                     color=sys.stdout.isatty() and not args.no_color and 'NO_COLOR' not in os.environ,
                     interactive=interactive, scroll=scroll)
    if terminal:
        terminal.draw(content)
    else:
        print(content)


class GpuSampler:
    def __init__(self, backend, interval):
        self.collector = GpuCollector(backend=backend)
        self.interval = interval
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='serverwatch-gpu')
        self.future = None
        self.next_time = 0.0
        self.sample_time = None
        self.cached = {'devices': [], 'processes': [], 'warnings': ['GPU discovery in progress'] if backend != 'none' else []}

    def collect(self):
        # Timestamp the measurement, not the later moment the UI consumes it.
        started = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()
        return self.collector.sample(), started, timestamp

    def wait_seconds(self, now):
        return 0.1 if self.future is not None else max(0.0, self.next_time - now)

    def poll(self, now):
        if self.future is not None and self.future.done():
            try:
                self.cached, self.sample_time, stamp = self.future.result()
                self.cached['sampled_at'] = stamp
            except Exception as exc:
                self.cached = {'devices': [], 'processes': [], 'warnings': ['GPU collection failed: ' + str(exc)]}
                self.sample_time = now
                self.cached['sampled_at'] = datetime.now(timezone.utc).isoformat()
            self.future = None
            self.next_time = self.sample_time + self.interval
        if self.future is None and now >= self.next_time:
            self.future = self.pool.submit(self.collect)
        result = dict(self.cached)
        result['age_seconds'] = round(max(0.0, now - self.sample_time), 2) if self.sample_time is not None else None
        return result

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


def loop(args):
    cached_mode = args.command == 'watch' and args.state is not None
    monitor = None if cached_mode else Monitor(window=args.window, top=args.top)
    proc = None if cached_mode else ProcCollector()
    gpu = None if cached_mode else GpuSampler(args.gpu, args.gpu_interval)
    panel, scroll = args.panel, 0
    snapshot = None
    next_sample = time.monotonic()
    previous_size = None
    try:
        with Terminal(enabled=args.command == 'watch' and not args.json) as terminal:
            while True:
                now = time.monotonic()
                changed = False
                gpu_data = gpu.poll(now) if gpu else None
                if now >= next_sample:
                    if cached_mode:
                        snapshot = read_state(args.state, args.max_age)
                    else:
                        snapshot = monitor.update(proc.sample(), gpu_data)
                    if args.command == 'serve':
                        write_state(args.state, snapshot)
                    changed = True
                    next_sample = time.monotonic() + args.interval
                size = shutil.get_terminal_size((100, 30))
                if snapshot and args.command == 'watch' and (changed or (terminal.enabled and size != previous_size)):
                    emit(snapshot, args, terminal, panel, scroll)
                    previous_size = size
                timeout = max(0.0, next_sample - time.monotonic())
                if gpu:
                    timeout = min(timeout, gpu.wait_seconds(time.monotonic()))
                key = terminal.key(min(timeout, 0.2) if terminal.enabled else timeout)
                if key in ('q', 'Q', '\x04'):
                    return 0
                if key in ('0', '1', '2', '3', '4', '\x1b', 'j', 'k', '\x1b[A', '\x1b[B'):
                    if key == '\x1b':
                        panel, scroll = 'overview', 0
                    elif key in '01234':
                        panel, scroll = PANELS[int(key)], 0
                    elif key in ('j', '\x1b[B'):
                        scroll += 1
                    else:
                        scroll = max(0, scroll - 1)
                    if snapshot:
                        emit(snapshot, args, terminal, panel, scroll)
    finally:
        if gpu:
            gpu.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith('-') and argv[0] not in ('-h', '--help', '--version')):
        argv.insert(0, 'watch' if sys.stdout.isatty() else 'once')
    p = parser()
    args = p.parse_args(argv)
    cached_mode = args.command == 'status' or (args.command == 'watch' and args.state is not None)
    if not cached_mode and args.window < args.interval:
        p.error('--window must be at least --interval')
    if not cached_mode and args.window / args.interval > 3600:
        p.error('--window / --interval must not exceed 3600 samples')
    if not cached_mode and not sys.platform.startswith('linux'):
        print('serverwatch: live collection requires Linux /proc; run this command on your server via SSH.', file=sys.stderr)
        return 2
    previous_handler = signal.getsignal(signal.SIGTERM)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        if args.command == 'status':
            snapshot = read_state(args.state, args.max_age)
            emit(snapshot, args)
            return 2 if snapshot['stale'] else 0
        if args.command == 'once':
            proc = ProcCollector()
            monitor = Monitor(window=args.window, top=args.top)
            monitor.update(proc.sample())
            time.sleep(args.interval)
            gpu = GpuCollector(backend=args.gpu).sample()
            gpu['age_seconds'] = 0.0
            gpu['sampled_at'] = datetime.now(timezone.utc).isoformat()
            emit(monitor.update(proc.sample(), gpu), args)
            return 0
        if args.command == 'serve':
            with state_lock(args.state):
                return loop(args)
        return loop(args)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        # Avoid a second BrokenPipeError from interpreter shutdown.
        with open(os.devnull, 'w') as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 0
    except (OSError, ValueError, RuntimeError, CollectorError, KeyError, TypeError) as exc:
        print('serverwatch: ' + str(exc), file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
