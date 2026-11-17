#!/usr/bin/env python3
"""Bundle and deploy ServerWatch using literal, non-executable env settings."""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    'ROOT_SSH', 'NODE_IP', 'ROOT_USER', 'SSH_PORT', 'SSH_KEY',
    'INSTALL_PREFIX', 'STATE_FILE', 'ENABLE_SERVICE', 'ENABLE_LINGER',
    'INTERVAL', 'WINDOW', 'TOP', 'GPU', 'GPU_INTERVAL',
}


class ConfigError(ValueError):
    pass


def account(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,31}', value):
        raise ConfigError('ROOT_USER must be a valid SSH login name')
    return value


def host(value):
    if ':' in value and not re.fullmatch(r'[0-9A-Fa-f:.]+', value):
        raise ConfigError('IPv6 addresses must not contain scope identifiers or shell syntax')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', value):
            raise ConfigError('NODE_IP / ROOT_SSH must contain a hostname, IP address or OpenSSH alias')
    return value


def seconds(name, value):
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigError(f'{name} must be a number of seconds') from exc
    if not math.isfinite(number) or not 0.1 <= number <= 3600:
        raise ConfigError(f'{name} must be between 0.1 and 3600 seconds')
    return number


def integer(name, value, maximum):
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= maximum:
        raise ConfigError(f'{name} must be an integer between 1 and {maximum}')
    return int(value)


def boolean(name, value):
    if value.lower() not in ('true', 'false'):
        raise ConfigError(f'{name} must be true or false')
    return value.lower() == 'true'


def remote_path(name, value):
    if not value:
        return None
    if (not re.fullmatch(r'/[A-Za-z0-9._/-]+', value)
            or any(part == '..' for part in value.split('/'))):
        raise ConfigError(f'{name} must be an absolute path using letters, digits, ., _, -, /; leave blank for the default')
    normalized = str(PurePosixPath(value))
    if normalized == '/':
        raise ConfigError(f'{name} must not be the filesystem root')
    return normalized


def read_config(path, local=False):
    path = Path(path).expanduser().absolute()
    raw = {}
    try:
        content = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError as exc:
        raise ConfigError(f'configuration not found: {path}; copy scripts/new_node.env.example first') from exc
    for line_number, line in enumerate(content.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, separator, value = line.partition('=')
        if not separator or name not in FIELDS:
            raise ConfigError(f'{path}:{line_number}: unknown field or invalid KEY=value line: {name}')
        if name in raw:
            raise ConfigError(f'{path}:{line_number}: duplicate field {name}')
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ConfigError(f'{path}:{line_number}: control characters are not allowed')
        raw[name] = value
    settings = {
        'install_prefix': remote_path('INSTALL_PREFIX', raw.get('INSTALL_PREFIX', '')),
        'state_file': remote_path('STATE_FILE', raw.get('STATE_FILE', '')),
        'enable_service': boolean('ENABLE_SERVICE', raw.get('ENABLE_SERVICE', 'true')),
        'enable_linger': boolean('ENABLE_LINGER', raw.get('ENABLE_LINGER', 'true')),
        'interval': seconds('INTERVAL', raw.get('INTERVAL', '2')),
        'window': seconds('WINDOW', raw.get('WINDOW', '60')),
        'top': integer('TOP', raw.get('TOP', '5'), 100),
        'gpu': raw.get('GPU', 'auto'),
        'gpu_interval': seconds('GPU_INTERVAL', raw.get('GPU_INTERVAL', '5')),
    }
    if settings['gpu'] not in ('auto', 'nvidia', 'amd', 'none'):
        raise ConfigError('GPU must be auto, nvidia, amd or none')
    if settings['window'] < settings['interval']:
        raise ConfigError('WINDOW must be at least INTERVAL')
    if settings['window'] / settings['interval'] > 3600:
        raise ConfigError('WINDOW / INTERVAL must not exceed 3600 samples')
    connection = {'target': None, 'port': None, 'key': None}
    if not local:
        alias = raw.get('ROOT_SSH', '')
        if alias:
            if raw.get('NODE_IP') or raw.get('ROOT_USER'):
                raise ConfigError('use ROOT_SSH OR NODE_IP + ROOT_USER; clear the other fields')
            pieces = alias.split('@')
            if len(pieces) > 2:
                raise ConfigError('ROOT_SSH must be an OpenSSH alias or user@hostname')
            if len(pieces) == 2:
                account(pieces[0])
            host(pieces[-1])
            connection['target'] = alias
        else:
            connection['target'] = account(raw.get('ROOT_USER') or 'root') + '@' + host(raw.get('NODE_IP', ''))
        # An omitted/empty port respects an OpenSSH alias's Port setting.
        port = raw.get('SSH_PORT', '')
        connection['port'] = integer('SSH_PORT', port, 65535) if port else None
        identity = raw.get('SSH_KEY', '')
        if identity:
            key = Path(identity).expanduser()
            if not key.is_absolute():
                key = path.parent / key
            if not key.is_file():
                raise ConfigError(f'SSH_KEY is not a local private-key file: {key}')
            connection['key'] = str(key.absolute())
    return settings, connection


def ssh_args(connection):
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no']
    if connection['port'] is not None:
        command += ['-p', str(connection['port'])]
    if connection['key']:
        command += ['-i', connection['key']]
    return command + ['--', connection['target']]


def show_plan(settings, connection, args):
    prefix = settings['install_prefix'] or '<target HOME>/.local'
    state = settings['state_file'] or '/run/user/<target UID>/serverwatch/status.json'
    print('Target: ' + ('current Linux login account (--local)' if args.local else connection['target']))
    print(f'Executable: {prefix}/bin/serverwatch')
    print('User service: ' + ('enable and start serverwatch.service' if settings['enable_service'] else 'CLI only'))
    linger = settings['enable_service'] and settings['enable_linger']
    print('Linger: ' + ('enable for the target login account' if linger else 'leave unchanged'))
    print('State: ' + state)
    print(f"Sampling: CPU {settings['interval']:g}s / GPU {settings['gpu_interval']:g}s / window {settings['window']:g}s / top {settings['top']} / GPU {settings['gpu']}")
    print('Upgrade: ' + ('allow replacement of changed files/configuration' if args.upgrade else 'require matching files/configuration when already installed'))
    print('Dry run only: no SSH connection, installation or service changes.', flush=True)


def make_bundle(directory, settings):
    directory = Path(directory)
    subprocess.run([sys.executable, str(ROOT / 'scripts/build.py'), '--output', str(directory / 'serverwatch.pyz')],
                   check=True, stdout=subprocess.PIPE, text=True)
    (directory / 'config.json').write_text(json.dumps(settings, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
    (directory / 'deploy_target.py').write_bytes((ROOT / 'scripts/deploy_target.py').read_bytes())


def execute(settings, connection, args):
    with tempfile.TemporaryDirectory(prefix='serverwatch-deploy-') as directory:
        bundle = Path(directory)
        make_bundle(bundle, settings)
        flags = (['--check'] if args.check else []) + (['--upgrade'] if args.upgrade else [])
        if args.local:
            subprocess.run([sys.executable, str(bundle / 'deploy_target.py'), '--bundle', str(bundle), *flags], check=True)
            return
        archive = bundle / 'bundle.tar'
        with tarfile.open(archive, 'w', format=tarfile.USTAR_FORMAT) as stream:
            for name in ('config.json', 'deploy_target.py', 'serverwatch.pyz'):
                stream.add(bundle / name, arcname=name, recursive=False)
        # All dynamic configuration stays inside JSON. The remote shell only
        # receives this fixed command and flags selected by argparse.
        command = '''set -eu
umask 077
work=$(mktemp -d /tmp/serverwatch-deploy.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT HUP INT TERM
tar -xf - -C "$work"
python3 "$work/deploy_target.py" --bundle "$work"'''
        command += ''.join(' ' + flag for flag in flags)
        with archive.open('rb') as stream:
            subprocess.run([*ssh_args(connection), command], stdin=stream, check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='bash scripts/deploy-serverwatch.sh', description=__doc__)
    parser.add_argument('--env', type=Path, default=ROOT / 'scripts/new_node.env', help='literal KEY=value configuration')
    parser.add_argument('--local', action='store_true', help='install for the invoking account on this Linux host')
    parser.add_argument('--upgrade', action='store_true', help='replace changed program/service configuration')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='print the plan locally, without SSH or installation')
    mode.add_argument('--check', action='store_true', help='check target prerequisites without installation or service changes')
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 10):
            raise ConfigError('Python 3.10 or newer is required')
        settings, connection = read_config(args.env, local=args.local)
        if args.dry_run:
            show_plan(settings, connection, args)
        else:
            execute(settings, connection, args)
        return 0
    except KeyboardInterrupt:
        print('deploy-serverwatch: interrupted', file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('deploy-serverwatch: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
