"""Private atomic snapshot files; no database or network listener."""
import contextlib
import datetime
import json
import math
import os
from pathlib import Path
import tempfile


def default_state():
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    base = Path(runtime) if runtime else Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache')))
    return base / 'serverwatch' / 'status.json'


def write_state(path, snapshot):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.status-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(snapshot, stream, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
            stream.write('\n')
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


@contextlib.contextmanager
def state_lock(path):
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(path) + '.lock', flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('another ServerWatch service is already writing this state file') from exc
        yield
    finally:
        os.close(fd)


def read_state(path, max_age=15.0):
    with Path(path).open(encoding='utf-8') as stream:
        content = stream.read(8 * 1024 * 1024 + 1)
    if len(content) > 8 * 1024 * 1024:
        raise ValueError('state file is too large')
    try:
        def finite_number(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError('state contains a nonfinite number')
            return number
        snapshot = json.loads(content, parse_float=finite_number, parse_constant=finite_number)
        if not isinstance(snapshot, dict) or snapshot.get('schema_version') != 1:
            raise ValueError('unsupported state schema')
        for key in ('cpu', 'memory', 'gpu', 'recent'):
            if not isinstance(snapshot[key], dict):
                raise ValueError('invalid state section: ' + key)
        for key in ('top_cpu', 'top_memory', 'top_gpu', 'warnings'):
            if not isinstance(snapshot[key], list):
                raise ValueError('invalid state section: ' + key)
        required = {
            'cpu': ('logical_cores', 'used_percent', 'iowait_percent', 'load_average'),
            'memory': ('total_bytes', 'available_bytes', 'used_bytes', 'swap_total_bytes', 'swap_used_bytes'),
            'gpu': ('devices', 'processes', 'warnings'),
            'recent': ('window_seconds', 'observed_seconds', 'processes'),
        }
        for section, fields in required.items():
            if any(field not in snapshot[section] for field in fields):
                raise ValueError('incomplete state section: ' + section)
        arrays = [snapshot[key] for key in ('top_cpu', 'top_memory', 'top_gpu')]
        arrays += [snapshot['gpu']['devices'], snapshot['gpu']['processes'], snapshot['recent']['processes'], snapshot['cpu'].get('cores', [])]
        for rows in arrays:
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError('invalid state records')
        stamp = datetime.datetime.fromisoformat(snapshot['time'])
        if stamp.tzinfo is None:
            raise ValueError('state timestamp must include timezone')
        age = (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds()
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid ServerWatch state file') from exc
    snapshot['state_age_seconds'] = round(age, 2)
    snapshot['stale'] = age > max_age or age < -5
    return snapshot
