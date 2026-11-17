#!/usr/bin/env python3
"""Target-side installer used by deploy-serverwatch.sh; Python standard library only."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

UNIT = 'serverwatch.service'
FIELDS = {'install_prefix', 'state_file', 'enable_service', 'enable_linger', 'interval', 'window', 'top', 'gpu', 'gpu_interval'}


class DeploymentError(RuntimeError):
    pass


def validate_config(config):
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise DeploymentError('invalid deployment configuration fields')
    for name in ('enable_service', 'enable_linger'):
        if type(config[name]) is not bool:
            raise DeploymentError(name + ' must be a boolean')
    for name in ('interval', 'window', 'gpu_interval'):
        value = config[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not .1 <= value <= 3600:
            raise DeploymentError(name + ' must be between 0.1 and 3600')
    if config['window'] < config['interval'] or config['window'] / config['interval'] > 3600:
        raise DeploymentError('invalid sampling window/interval ratio')
    if type(config['top']) is not int or not 1 <= config['top'] <= 100:
        raise DeploymentError('top must be an integer between 1 and 100')
    if config['gpu'] not in ('auto', 'nvidia', 'amd', 'none'):
        raise DeploymentError('invalid GPU backend')
    for name in ('install_prefix', 'state_file'):
        value = config[name]
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9._/-]+', value)
                                  or '..' in Path(value).parts or value == '/'):
            raise DeploymentError(name + ' must be an absolute path without shell syntax, spaces or ..')
    return dict(config)


def archive_hash(path):
    """Ignore ZIP timestamps and ordering; compare the installed program contents."""
    digest = hashlib.sha256()
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names) or '__main__.py' not in names:
                raise DeploymentError('invalid ServerWatch archive')
            # ZIP metadata may change between builds, but the executable
            # shebang must not drift unnoticed for direct CLI invocation.
            prefix_length = min(info.header_offset for info in archive.infolist())
            with Path(path).open('rb') as stream:
                prefix = stream.read(prefix_length)
            digest.update(b'prefix\0' + str(len(prefix)).encode() + b'\0' + prefix)
            for name in sorted(names):
                data = archive.read(name)
                digest.update(name.encode() + b'\0' + str(len(data)).encode() + b'\0' + data)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise DeploymentError('cannot read ServerWatch archive: ' + str(path)) from exc
    return digest.hexdigest()


def systemd_arg(value):
    value = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$')
    return '"' + value + '"'


class Installer:
    def __init__(self, config, bundle, *, home=None, runtime=None, runner=None, system_paths=None,
                 sleep=time.sleep, monotonic=time.monotonic, wall_time=time.time, systemd_root=None):
        self.config = validate_config(config)
        self.bundle = Path(bundle)
        self.home = Path(home) if home is not None else Path.home()
        self.uid = os.getuid()
        self.username = pwd.getpwuid(self.uid).pw_name
        self.runtime = Path(runtime) if runtime is not None else Path('/run/user') / str(self.uid)
        self.systemd_root = Path(systemd_root) if systemd_root is not None else Path('/run/systemd/system')
        self.prefix = Path(config['install_prefix']) if config['install_prefix'] else self.home / '.local'
        self.binary = self.prefix / 'bin/serverwatch'
        self.unit = self.home / '.config/systemd/user' / UNIT
        self.manifest = self.prefix / 'share/serverwatch/deployment.json'
        self.lock = self.manifest.with_name('deploy.lock')
        self.state = Path(config['state_file']) if config['state_file'] else self.runtime / 'serverwatch/status.json'
        self.candidate = self.bundle / 'serverwatch.pyz'
        self.runner = runner or subprocess.run
        self.sleep, self.monotonic, self.wall_time = sleep, monotonic, wall_time
        self.system_paths = system_paths if system_paths is not None else [
            Path('/etc/systemd/user') / UNIT, Path('/usr/lib/systemd/user') / UNIT,
            Path('/usr/local/lib/systemd/user') / UNIT, Path('/etc/systemd/system') / UNIT,
            Path('/run/systemd/user') / UNIT, Path('/run/systemd/system') / UNIT,
        ]
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', XDG_RUNTIME_DIR=str(self.runtime),
                        DBUS_SESSION_BUS_ADDRESS='unix:path=' + str(self.runtime / 'bus'))
        self.max_age = min(3600, max(15, config['interval'] * 2 + 5))
        self._admin = [] if self.uid == 0 else ['sudo', '-n']

    def run(self, args, *, check=True, timeout=20):
        try:
            result = self.runner([str(arg) for arg in args], env=self.env, text=True, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentError('command could not complete: ' + shlex.join(map(str, args))) from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout or '').strip()
            raise DeploymentError(shlex.join(map(str, args)) + (': ' + detail if detail else ': command failed'))
        return result

    def userctl(self, *args, check=True, timeout=20):
        return self.run(['systemctl', '--user', *args], check=check, timeout=timeout)

    def _safe_path(self, path):
        for item in [path, *path.parents]:
            if item.is_symlink():
                raise DeploymentError('refusing a symbolic link in installation path: ' + str(item))
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid:
                raise DeploymentError('expected a regular file owned by the current account: ' + str(path))
        for parent in path.parents:
            if not parent.exists():
                continue
            info = parent.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, self.uid):
                raise DeploymentError('untrusted installation parent directory: ' + str(parent))
            shared_temp = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_mode & 0o022 and not shared_temp:
                raise DeploymentError('group/world-writable installation parent directory: ' + str(parent))

    def _writable_parent(self, path):
        parent = path.parent
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK | os.X_OK):
            raise DeploymentError('installation parent is not writable by the current account: ' + str(parent))

    def _unit_text(self):
        c = self.config
        args = [sys.executable, self.binary, 'serve', '--state', self.state,
                '--interval', format(c['interval'], '.15g'), '--window', format(c['window'], '.15g'),
                '--top', str(c['top']), '--gpu', c['gpu'], '--gpu-interval', format(c['gpu_interval'], '.15g')]
        return ('# Managed by ServerWatch deploy-serverwatch.sh\n[Unit]\n'
                'Description=ServerWatch lightweight server resource monitor\n\n[Service]\nType=simple\n'
                'ExecStart=' + ' '.join(systemd_arg(arg) for arg in args) + '\n'
                'Restart=on-failure\nRestartSec=3\nRuntimeDirectory=serverwatch\nRuntimeDirectoryMode=0700\n'
                'UMask=0077\nNice=10\nNoNewPrivileges=yes\n\n[Install]\nWantedBy=default.target\n').encode()

    def _manifest_bytes(self):
        config = dict(self.config, install_prefix=str(self.prefix), state_file=str(self.state))
        record = {'schema_version': 1, 'config': config, 'app_hash': archive_hash(self.candidate),
                  'unit_hash': hashlib.sha256(self._unit_text()).hexdigest() if config['enable_service'] else None,
                  'python': sys.executable}
        return (json.dumps(record, sort_keys=True, indent=2) + '\n').encode()

    def _manager_ready(self):
        return self.userctl('show-environment', check=False).returncode == 0

    def _linger(self):
        result = self.run(['loginctl', 'show-user', self.username, '--property=Linger', '--value'], check=False)
        value = result.stdout.strip()
        if result.returncode or value not in ('yes', 'no'):
            # An account with no active login manager may have no loginctl object.
            return (Path('/var/lib/systemd/linger') / self.username).exists()
        return value == 'yes'

    def _service_state(self):
        enabled = self.userctl('is-enabled', UNIT, check=False).stdout.strip()
        if enabled in ('masked', 'masked-runtime'):
            raise DeploymentError('serverwatch.service is masked; unmask it explicitly before deploying')
        return (self.userctl('is-active', '--quiet', UNIT, check=False).returncode == 0, enabled)

    def _check_effective_unit(self):
        result = self.userctl('show', UNIT, '--property=FragmentPath', '--property=DropInPaths', check=False)
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if fields.get('DropInPaths'):
            raise DeploymentError('existing ServerWatch service overrides must be reviewed and removed before deploying')
        fragment = fields.get('FragmentPath')
        if fragment and Path(fragment) != self.unit:
            raise DeploymentError('another ServerWatch unit is already loaded: ' + fragment)

    def preflight(self, report=True):
        if not sys.platform.startswith('linux') or sys.version_info < (3, 10):
            raise DeploymentError('deployment target requires Linux and Python 3.10+')
        if not self.candidate.is_file():
            raise DeploymentError('bundle is missing serverwatch.pyz')
        protected = {self.binary, self.unit, self.manifest, self.lock}
        if self.state in protected or Path(str(self.state) + '.lock') in protected:
            raise DeploymentError('STATE_FILE collides with an installation file')
        if any(path in self.state.parents or self.state in path.parents for path in protected):
            raise DeploymentError('STATE_FILE overlaps an installation file path')
        for path in protected | {self.state, Path(str(self.state) + '.lock')}:
            self._safe_path(path)
        for path in (self.binary, self.manifest):
            self._writable_parent(path)
        if self.config['enable_service']:
            self._writable_parent(self.unit)
            if self.config['state_file'] is not None:
                self._writable_parent(self.state)
        if self.state.exists():
            try:
                if self.state.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError('oversized state file')
                cached = json.loads(self.state.read_text())
                if not isinstance(cached, dict) or cached.get('schema_version') != 1 or not all(
                        isinstance(cached.get(key), dict) for key in ('cpu', 'memory', 'gpu', 'recent')):
                    raise ValueError('not a ServerWatch snapshot')
            except (ValueError, OSError) as exc:
                raise DeploymentError('refusing to overwrite an unrecognized STATE_FILE: ' + str(self.state)) from exc
        self.desired_manifest = self._manifest_bytes()
        self.run([sys.executable, self.candidate, '--version'])
        self.before_active, self.before_enabled, self.before_linger = False, 'disabled', False
        if self.config['enable_service']:
            if not self.systemd_root.is_dir():
                raise DeploymentError('a running systemd installation is required for ENABLE_SERVICE=true')
            for command in ('systemctl', 'loginctl'):
                if shutil.which(command) is None:
                    raise DeploymentError('missing prerequisite: ' + command)
            for path in self.system_paths + [self.unit]:
                if path != self.unit and (path.exists() or path.is_symlink()):
                    raise DeploymentError('another ServerWatch service exists: ' + str(path))
                if path.with_name(path.name + '.d').exists():
                    raise DeploymentError('ServerWatch service overrides exist: ' + str(path) + '.d')
            ready = self._manager_ready()
            self.before_linger = self._linger()
            if self.config['enable_linger'] and not self.before_linger and self.uid != 0:
                self.run(['sudo', '-n', 'true'])
            if not ready:
                if not self.config['enable_linger']:
                    raise DeploymentError('user systemd manager is unavailable; enable linger or establish a user session')
                if self.uid != 0:
                    self.run(['sudo', '-n', 'true'])
            else:
                self._check_effective_unit()
                self.before_active, self.before_enabled = self._service_state()
        else:
            previous = {}
            if self.manifest.exists():
                try:
                    previous = json.loads(self.manifest.read_text())
                except (ValueError, OSError) as exc:
                    raise DeploymentError('cannot read existing deployment manifest') from exc
            if self.unit.exists() or previous.get('config', {}).get('enable_service'):
                raise DeploymentError('service-to-CLI conversion is manual: stop/disable and remove the unit and deployment manifest first')
        if report:
            print('Target: ' + self.username + ' (uid ' + str(self.uid) + ')')
            print('Program: ' + str(self.binary))
            print('State: ' + str(self.state))
            print('User service plan: ' + ('enable and start ' + UNIT if self.config['enable_service'] else 'CLI only'))
            print('Linger plan: ' + ('enable for this account' if self.config['enable_service'] and self.config['enable_linger'] else 'leave unchanged'))

    def _same_install(self):
        if not self.binary.exists() or not self.manifest.exists():
            return False
        try:
            same = (archive_hash(self.binary) == archive_hash(self.candidate)
                    and self.manifest.read_bytes() == self.desired_manifest
                    and stat.S_IMODE(self.binary.stat().st_mode) == 0o755)
            if self.config['enable_service']:
                same = same and self.unit.exists() and self.unit.read_bytes() == self._unit_text()
            return same
        except (OSError, DeploymentError):
            return False

    def _cache_ready(self, after=None):
        try:
            if self.state.stat().st_size > 8 * 1024 * 1024:
                return False
            snapshot = json.loads(self.state.read_text())
            stamp = datetime.fromisoformat(snapshot['time'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                return False
            recorded = stamp.timestamp()
            age = self.wall_time() - recorded
            return (snapshot['schema_version'] == 1 and isinstance(snapshot['gpu'], dict)
                    and isinstance(snapshot['recent'], dict) and snapshot['cpu']['logical_cores'] > 0
                    and snapshot['memory']['total_bytes'] > 0 and -5 <= age <= self.max_age
                    and (after is None or recorded >= after))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def health(self, after=None, timeout=10):
        deadline = self.monotonic() + timeout
        stable_since = None
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise DeploymentError('service did not produce a fresh valid snapshot; inspect journalctl --user -u ' + UNIT)
            ready = self.userctl('is-active', '--quiet', UNIT, check=False, timeout=min(2, remaining)).returncode == 0 and self._cache_ready(after)
            now = self.monotonic()
            if ready:
                stable_since = now if stable_since is None else stable_since
                if now - stable_since >= .5:
                    return
            else:
                stable_since = None
            if now >= deadline:
                raise DeploymentError('service did not produce a fresh valid snapshot; inspect journalctl --user -u ' + UNIT)
            self.sleep(.2)

    def _publish(self, path, content, mode):
        self._safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._safe_path(path)
        fd, pending = tempfile.mkstemp(prefix='.serverwatch-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(pending, mode)
            os.replace(pending, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(pending)

    def _rollback(self, backups, service_touched, linger_changed):
        errors = []
        def attempt(action):
            try:
                action()
            except Exception as exc:
                errors.append(str(exc))
        if service_touched:
            attempt(lambda: self.userctl('stop', UNIT))
            # Disable while the attempted unit still exists. This also removes
            # persistent links before restoring prior enabled-runtime state.
            attempt(lambda: self.userctl('disable', UNIT))
        for path, previous in reversed(list(backups.items())):
            if previous is None:
                attempt(lambda path=path: path.unlink(missing_ok=True))
            else:
                content, mode = previous
                attempt(lambda path=path, content=content, mode=mode: self._publish(path, content, mode))
        if service_touched:
            attempt(lambda: self.userctl('daemon-reload'))
            if self.before_enabled in ('enabled', 'enabled-runtime'):
                args = ['enable'] + (['--runtime'] if self.before_enabled == 'enabled-runtime' else [])
                attempt(lambda: self.userctl(*args, UNIT))
            if self.before_active:
                attempt(lambda: self.userctl('start', UNIT))
        if linger_changed:
            attempt(lambda: self.run([*self._admin, 'loginctl', 'disable-linger', self.username]))
        if errors:
            raise DeploymentError('rollback incomplete: ' + '; '.join(errors))
        print('Previous installation and service state restored.', file=sys.stderr)

    def deploy(self, *, check=False, upgrade=False):
        self.preflight()
        if check:
            print('Preflight passed; no installation, service or linger settings changed.')
            return
        if not upgrade and any(path.exists() for path in (self.binary, self.unit, self.manifest)) and not self._same_install():
            raise DeploymentError('existing installation differs; use --upgrade to replace the program or configuration')
        self.lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._safe_path(self.lock)
        fd = os.open(self.lock, os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeploymentError('another ServerWatch deployment is running') from exc
            # Recheck state under the lock, in case another deployment just finished.
            self.preflight(report=False)
            same = self._same_install()
            if same and not upgrade:
                if self.config['enable_service']:
                    if not self.before_active or self.before_enabled != 'enabled' or (self.config['enable_linger'] and not self.before_linger):
                        raise DeploymentError('installation exists but service settings differ; use --upgrade to repair')
                    self.health()
                print('Already current; no files replaced and no service restarted.')
                self.print_usage()
                return
            existing = any(path.exists() for path in (self.binary, self.unit, self.manifest))
            if existing and not upgrade:
                raise DeploymentError('existing installation differs; use --upgrade to replace the program or configuration')
            targets = {self.binary: (self.candidate.read_bytes(), 0o755)}
            if self.config['enable_service']:
                targets[self.unit] = (self._unit_text(), 0o644)
            targets[self.manifest] = (self.desired_manifest, 0o600)
            backups = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None for path in targets}
            service_touched = linger_changed = False
            try:
                if self.config['enable_service'] and self.before_active:
                    service_touched = True
                    self.userctl('stop', UNIT)
                for path, (content, mode) in targets.items():
                    if path != self.manifest:
                        self._publish(path, content, mode)
                if self.config['enable_service']:
                    service_touched = True
                    if self.config['enable_linger'] and not self.before_linger:
                        linger_changed = True
                        self.run([*self._admin, 'loginctl', 'enable-linger', self.username])
                    if not self._manager_ready():
                        self.run([*self._admin, 'systemctl', 'start', 'user@' + str(self.uid) + '.service'])
                    self.userctl('daemon-reload')
                    self._check_effective_unit()
                    self.userctl('enable', UNIT)
                    after = self.wall_time()
                    self.userctl('start', UNIT)
                    self.health(after)
                else:
                    self.run([sys.executable, self.binary, '--version'])
                self._publish(self.manifest, *targets[self.manifest])
            except BaseException as exc:
                try:
                    self._rollback(backups, service_touched, linger_changed)
                except DeploymentError as rollback:
                    raise DeploymentError(str(exc) + '; ' + str(rollback)) from exc
                raise
            print('ServerWatch deployment completed.')
            self.print_usage()
        finally:
            os.close(fd)

    def print_usage(self):
        command = [str(self.binary), 'watch']
        if self.config['enable_service']:
            command += ['--state', str(self.state), '--max-age', format(self.max_age, '.15g')]
        print('PATH (on target, current shell): export PATH=' + shlex.quote(str(self.binary.parent)) + ':"$PATH"')
        print('View (on target): ' + shlex.join(command))
        if self.config['enable_service']:
            print('Logs: journalctl --user -u ' + UNIT + ' -n 30 --no-pager')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--upgrade', action='store_true')
    args = parser.parse_args(argv)
    termination_signals = [signal.SIGTERM, signal.SIGHUP]
    previous_handlers = {signum: signal.getsignal(signum) for signum in termination_signals}
    def interrupted(signum, frame):
        # Let the transaction restore files/service state on an ordinary SSH
        # disconnect or cancellation; repeated termination must not abort it.
        for number in termination_signals:
            signal.signal(number, signal.SIG_IGN)
        raise KeyboardInterrupt('interrupted by signal ' + str(signum))
    for signum in termination_signals:
        signal.signal(signum, interrupted)
    try:
        config = json.loads((args.bundle / 'config.json').read_text())
        Installer(config, args.bundle).deploy(check=args.check, upgrade=args.upgrade)
        return 0
    except (DeploymentError, OSError, ValueError, KeyboardInterrupt) as exc:
        print('deploy-serverwatch: ' + (str(exc) or 'interrupted'), file=sys.stderr)
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    raise SystemExit(main())
