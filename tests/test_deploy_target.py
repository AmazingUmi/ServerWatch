from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

MODULE = Path(__file__).resolve().parents[1] / 'scripts/deploy_target.py'
spec = importlib.util.spec_from_file_location('serverwatch_deploy_target', MODULE)
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)


def configuration(service=False):
    return {'install_prefix': None, 'state_file': None, 'enable_service': service, 'enable_linger': True,
            'interval': 2.0, 'window': 60.0, 'top': 5, 'gpu': 'auto', 'gpu_interval': 5.0}


class Clock:
    def __init__(self):
        self.value = 1_800_000_000.0
    def now(self):
        return self.value
    def sleep(self, value):
        self.value += value


class FakeSystem:
    def __init__(self, clock):
        self.clock, self.installer = clock, None
        self.calls = []
        self.active, self.enabled, self.linger, self.manager = False, 'disabled', False, True
        self.fail_start = False
        self.stale = False
        self.fail_reload_count = 0
        self.dropins = ''
    def __call__(self, args, **kwargs):
        self.calls.append(args)
        result = lambda code=0, out='', err='': subprocess.CompletedProcess(args, code, out, err)
        if args[:2] == ['sudo', '-n']:
            args = args[2:]
        if args[:2] == ['loginctl', 'show-user']:
            return result(out='yes\n' if self.linger else 'no\n')
        if args[:2] == ['loginctl', 'enable-linger']:
            self.linger = True
        elif args[:2] == ['loginctl', 'disable-linger']:
            self.linger = False
        elif args[:2] == ['systemctl', 'start']:
            self.manager = True
        elif args[:2] == ['systemctl', '--user']:
            action = args[2]
            if action == 'show-environment':
                return result(0 if self.manager else 1)
            if action == 'is-active':
                return result(0 if self.active else 3)
            if action == 'is-enabled':
                return result(0 if self.enabled == 'enabled' else 1, self.enabled + '\n')
            if action == 'show':
                fragment = str(self.installer.unit) if self.installer.unit.exists() else ''
                return result(out='FragmentPath=' + fragment + '\nDropInPaths=' + self.dropins + '\n')
            if action == 'stop':
                self.active = False
            if action == 'enable':
                self.enabled = 'enabled-runtime' if '--runtime' in args else 'enabled'
            if action == 'disable':
                if not self.installer.unit.exists():
                    return result(1, err='unit missing during disable')
                self.enabled = 'disabled'
            if action == 'daemon-reload' and self.fail_reload_count:
                self.fail_reload_count -= 1
                return result(1, err='reload failed')
            if action == 'start':
                if self.fail_start:
                    self.fail_start = False
                    return result(1, err='start failed')
                self.active = True
                self.installer.state.parent.mkdir(parents=True, exist_ok=True)
                stamp = self.clock.now() - (1000 if self.stale else 0)
                self.installer.state.write_text(json.dumps({'schema_version': 1,
                    'time': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                    'cpu': {'logical_cores': 4}, 'memory': {'total_bytes': 1024}, 'gpu': {}, 'recent': {}}))
        return result(out='ServerWatch 0.1.0\n')


class TargetDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home, self.bundle = self.root / 'home', self.root / 'bundle'
        self.home.mkdir()
        self.bundle.mkdir()
        self.systemd = self.root / 'systemd'
        self.systemd.mkdir()
        self.clock = Clock()
        self.system = FakeSystem(self.clock)
        self.archive()
        self.platform = patch.object(target.sys, 'platform', 'linux')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.which = patch.object(target.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name)
        self.which.start()
        self.addCleanup(self.which.stop)
    def archive(self, text='original', stamp=(2026, 1, 1, 0, 0, 0)):
        with zipfile.ZipFile(self.bundle / 'serverwatch.pyz', 'w') as archive:
            info = zipfile.ZipInfo('__main__.py', stamp)
            archive.writestr(info, 'print(' + repr(text) + ')')
    def installer(self, service=False, **changes):
        config = configuration(service)
        config.update(changes)
        installer = target.Installer(config, self.bundle, home=self.home, runtime=self.root / 'runtime',
            runner=self.system, system_paths=[], systemd_root=self.systemd, sleep=self.clock.sleep,
            monotonic=self.clock.now, wall_time=self.clock.now)
        self.system.installer = installer
        return installer
    def deploy(self, installer, **kwargs):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            installer.deploy(**kwargs)
    def test_cli_only_install_and_same_content_redeploy_without_systemd(self):
        installer = self.installer()
        self.deploy(installer)
        inode = installer.binary.stat().st_ino
        self.assertEqual(stat.S_IMODE(installer.binary.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(installer.manifest.stat().st_mode), 0o600)
        self.archive(stamp=(2026, 1, 2, 0, 0, 0))
        self.deploy(installer)
        self.assertEqual(installer.binary.stat().st_ino, inode)
        self.assertFalse(installer.unit.exists())
        self.assertFalse(any('systemctl' in args or 'loginctl' in args for args in self.system.calls))
    def test_check_does_not_create_installation_or_mutate_service(self):
        installer = self.installer(service=True)
        self.deploy(installer, check=True)
        self.assertFalse(installer.prefix.exists())
        self.assertFalse(installer.unit.exists())
        self.assertFalse(self.system.linger)
        self.assertFalse(self.system.active)
        mutations = ('start', 'stop', 'enable', 'disable', 'enable-linger', 'disable-linger', 'daemon-reload')
        self.assertFalse(any(any(word in args for word in mutations) for args in self.system.calls))
    def test_unmanaged_existing_file_requires_upgrade_before_mutation(self):
        installer = self.installer()
        installer.binary.parent.mkdir(parents=True)
        installer.binary.write_bytes(b'preserve unrelated content')
        with self.assertRaisesRegex(target.DeploymentError, '--upgrade'):
            self.deploy(installer)
        self.assertEqual(installer.binary.read_bytes(), b'preserve unrelated content')
        self.assertFalse(installer.lock.parent.exists())
    def test_program_and_configuration_changes_require_upgrade(self):
        installer = self.installer()
        self.deploy(installer)
        before = installer.binary.read_bytes()
        self.archive('changed')
        with self.assertRaisesRegex(target.DeploymentError, '--upgrade'):
            self.deploy(installer)
        self.assertEqual(installer.binary.read_bytes(), before)
        self.deploy(installer, upgrade=True)
        self.assertNotEqual(installer.binary.read_bytes(), before)
        changed = self.installer(interval=3.0)
        with self.assertRaisesRegex(target.DeploymentError, '--upgrade'):
            self.deploy(changed)
    def test_service_install_readiness_linger_and_idempotent_no_restart(self):
        installer = self.installer(service=True)
        self.deploy(installer)
        self.assertTrue(self.system.active)
        self.assertEqual(self.system.enabled, 'enabled')
        self.assertTrue(self.system.linger)
        self.assertIn('--state', installer.unit.read_text())
        self.assertIn('RuntimeDirectory=serverwatch', installer.unit.read_text())
        self.assertTrue(installer._cache_ready())
        self.system.calls.clear()
        self.deploy(installer)
        self.assertFalse(any('start' in args or 'stop' in args or 'daemon-reload' in args for args in self.system.calls))
    def test_failed_upgrade_restores_previous_files_modes_and_service(self):
        installer = self.installer(service=True)
        self.deploy(installer)
        before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                  for path in (installer.binary, installer.unit, installer.manifest)}
        self.system.enabled = 'enabled-runtime'
        self.system.calls.clear()
        self.archive('broken new version')
        self.system.fail_start = True
        with self.assertRaisesRegex(target.DeploymentError, 'start failed'):
            self.deploy(installer, upgrade=True)
        self.assertEqual({path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in before}, before)
        self.assertTrue(self.system.active)
        self.assertEqual(self.system.enabled, 'enabled-runtime')
        disable = next(i for i, args in enumerate(self.system.calls) if args[:3] == ['systemctl', '--user', 'disable'])
        restore = next(i for i, args in enumerate(self.system.calls) if '--runtime' in args)
        self.assertLess(disable, restore)
        self.assertTrue(self.system.linger)
    def test_fresh_deploy_failure_removes_files_and_restores_linger(self):
        installer = self.installer(service=True)
        self.system.fail_start = True
        with self.assertRaises(target.DeploymentError):
            self.deploy(installer)
        self.assertFalse(installer.binary.exists())
        self.assertFalse(installer.unit.exists())
        self.assertFalse(installer.manifest.exists())
        self.assertFalse(self.system.active)
        self.assertEqual(self.system.enabled, 'disabled')
        self.assertFalse(self.system.linger)
    def test_active_unit_with_stale_cache_is_not_healthy(self):
        installer = self.installer(service=True)
        self.system.stale = True
        with self.assertRaisesRegex(target.DeploymentError, 'fresh valid snapshot'):
            self.deploy(installer)
        self.assertFalse(installer.binary.exists())
        self.assertFalse(self.system.linger)
    def test_rollback_errors_are_reported_not_hidden(self):
        installer = self.installer(service=True)
        self.system.fail_reload_count = 2
        with self.assertRaisesRegex(target.DeploymentError, 'rollback incomplete'):
            self.deploy(installer)
        self.assertFalse(installer.binary.exists())
    def test_service_to_cli_change_requires_manual_action(self):
        installer = self.installer(service=True)
        self.deploy(installer)
        before = installer.binary.read_bytes()
        with self.assertRaisesRegex(target.DeploymentError, 'service-to-CLI'):
            self.deploy(self.installer(), upgrade=True)
        self.assertEqual(installer.binary.read_bytes(), before)
        self.assertTrue(self.system.active)
    def test_symlink_dropin_and_state_collision_rejected_before_writes(self):
        installer = self.installer()
        installer.binary.parent.mkdir(parents=True)
        victim = self.root / 'victim'
        victim.write_text('keep')
        installer.binary.symlink_to(victim)
        with self.assertRaisesRegex(target.DeploymentError, 'symbolic link'):
            self.deploy(installer)
        self.assertEqual(victim.read_text(), 'keep')
        installer.binary.unlink()
        bad = self.installer(state_file=str(installer.binary))
        with self.assertRaisesRegex(target.DeploymentError, 'collides'):
            self.deploy(bad)
        bad = self.installer(state_file=str(victim))
        with self.assertRaisesRegex(target.DeploymentError, 'unrecognized STATE_FILE'):
            self.deploy(bad)
        self.assertEqual(victim.read_text(), 'keep')
        service = self.installer(service=True)
        self.system.dropins = '/etc/systemd/user/serverwatch.service.d/override.conf'
        with self.assertRaisesRegex(target.DeploymentError, 'overrides'):
            self.deploy(service)
    def test_invalid_config_values_do_not_run_commands(self):
        for changes in ({'interval': float('nan')}, {'enable_service': 'true'}, {'window': .1}, {'top': True},
                        {'state_file': '/tmp/$(touch-bad)'}, {'install_prefix': '/tmp/../bin'}, {'gpu': 'invalid'}):
            with self.subTest(changes=changes):
                with self.assertRaises(target.DeploymentError):
                    self.installer(**changes)
        self.assertEqual(self.system.calls, [])
    def test_archive_hash_rejects_duplicates(self):
        path = self.bundle / 'serverwatch.pyz'
        archive_bytes = path.read_bytes()
        path.write_bytes(b'#!/usr/bin/env python3\n' + archive_bytes)
        original = target.archive_hash(path)
        path.write_bytes(b'#!/bin/false\n' + archive_bytes)
        self.assertNotEqual(target.archive_hash(path), original)
        with zipfile.ZipFile(self.bundle / 'serverwatch.pyz', 'w') as archive:
            archive.writestr('other.py', 'pass')
        with self.assertRaises(target.DeploymentError):
            target.archive_hash(self.bundle / 'serverwatch.pyz')
    def test_systemd_argument_escaping(self):
        self.assertEqual(target.systemd_arg('/a path/%x/$var/"q"'), '"/a path/%%x/$$var/\\"q\\""')

    def test_world_writable_parent_and_non_directory_parent_are_rejected(self):
        unsafe = self.root / 'unsafe'
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        with self.assertRaisesRegex(target.DeploymentError, 'world-writable'):
            self.deploy(self.installer(install_prefix=str(unsafe)))
        unsafe.chmod(0o700)
        blocker = unsafe / 'file'
        blocker.write_text('preserve')
        with self.assertRaisesRegex(target.DeploymentError, 'parent directory'):
            self.deploy(self.installer(install_prefix=str(blocker)))

    def test_interrupted_transaction_rolls_back_and_signal_handlers_restore(self):
        installer = self.installer()
        self.deploy(installer)
        before = installer.binary.read_bytes()
        self.archive('changed')
        original = installer._publish
        count = 0
        def interrupted_publish(path, content, mode):
            nonlocal count
            count += 1
            if count == 2:
                raise KeyboardInterrupt('test cancel')
            return original(path, content, mode)
        with patch.object(installer, '_publish', side_effect=interrupted_publish):
            with self.assertRaises(KeyboardInterrupt):
                self.deploy(installer, upgrade=True)
        self.assertEqual(installer.binary.read_bytes(), before)
        (self.bundle / 'config.json').write_text(json.dumps(configuration()))
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous = signal.getsignal(signum)
            with patch.object(target.Installer, 'deploy', side_effect=lambda **kwargs: os.kill(os.getpid(), signum)), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(target.main(['--bundle', str(self.bundle)]), 1)
            self.assertEqual(signal.getsignal(signum), previous)


if __name__ == '__main__':
    unittest.main()
