import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("serverwatch_deploy_frontend", PROJECT / "scripts/deploy_serverwatch.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeploymentFrontendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="serverwatch-deploy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = self.root / "node.env"

    def config(self, text="NODE_IP=192.0.2.10\n", local=False):
        self.env.write_text(text, encoding="utf-8")
        return deploy.read_config(self.env, local=local)

    def options(self, **changes):
        values = dict(local=False, check=False, upgrade=False, dry_run=False)
        values.update(changes)
        return argparse.Namespace(**values)

    def fake_bundle(self, directory, settings):
        directory = Path(directory)
        (directory / "config.json").write_text(json.dumps(settings))
        (directory / "deploy_target.py").write_text("# test worker\n")
        (directory / "serverwatch.pyz").write_bytes(b"test zipapp")

    def test_literal_config_crlf_comments_and_no_final_newline(self):
        self.env.write_bytes(b"\xef\xbb\xbf# node\r\n\r\nNODE_IP=192.0.2.10\r\nROOT_USER=operator\r\nINTERVAL=0.5\r\nWINDOW=30\r\nTOP=8\r\nGPU=none\r\nENABLE_SERVICE=false")
        settings, connection = deploy.read_config(self.env)
        self.assertEqual(connection["target"], "operator@192.0.2.10")
        self.assertEqual(settings["interval"], 0.5)
        self.assertEqual(settings["window"], 30)
        self.assertEqual(settings["top"], 8)
        self.assertEqual(settings["gpu"], "none")
        self.assertFalse(settings["enable_service"])
        self.assertIsNone(settings["install_prefix"])
        self.assertIsNone(connection["port"])

    def test_unknown_duplicate_and_executable_env_syntax_rejected(self):
        marker = self.root / "executed"
        for extra in (
            "UNKNOWN=1", "TOP=5\nTOP=9", "export TOP=5", "TOP =5", "INTERVAL=2 # comment",
            'NODE_IP="192.0.2.10"', f"INSTALL_PREFIX=$(touch {marker})", f"ROOT_USER=`touch {marker}`",
        ):
            with self.subTest(extra=extra), patch.object(deploy.subprocess, "run") as run:
                with self.assertRaises(deploy.ConfigError):
                    self.config("NODE_IP=192.0.2.10\n" + extra)
                run.assert_not_called()
                self.assertFalse(marker.exists())

    def test_quotes_and_variables_are_not_expanded_for_key_paths(self):
        literal = self.root / "$KEY_FILE"
        literal.write_text("test key placeholder")
        with patch.dict("os.environ", {"KEY_FILE": "nonexistent-expanded-path"}):
            _, connection = self.config("NODE_IP=192.0.2.10\nSSH_KEY=$KEY_FILE")
        self.assertEqual(connection["key"], str(literal))
        with self.assertRaises(deploy.ConfigError):
            self.config('NODE_IP=192.0.2.10\nSSH_KEY="$KEY_FILE"')

    def test_numeric_and_boolean_constraints_match_cli(self):
        bad = {
            "INTERVAL": ["0", "0.01", "3601", "nan", "inf", ""],
            "GPU_INTERVAL": ["-1", "NaN"], "WINDOW": ["1", "Infinity"],
            "TOP": ["0", "101", "2.5", "١"], "SSH_PORT": ["0", "65536", "-p22"],
            "ENABLE_SERVICE": ["yes", "1"], "ENABLE_LINGER": ["maybe"], "GPU": ["intel"],
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(deploy.ConfigError):
                    self.config(f"NODE_IP=192.0.2.10\n{key}={value}")
        with self.assertRaises(deploy.ConfigError):
            self.config("NODE_IP=192.0.2.10\nINTERVAL=0.1\nWINDOW=3600")
        settings, _ = self.config("NODE_IP=192.0.2.10\nINTERVAL=0.1\nWINDOW=360\nTOP=100\nENABLE_SERVICE=FALSE")
        self.assertEqual(settings["window"] / settings["interval"], 3600)
        self.assertFalse(settings["enable_service"])

    def test_remote_paths_are_absolute_literal_and_canonical(self):
        for value in ("/", "relative", "~/.local", "/opt/../etc", "/opt/with space", "/opt/x;touch_y", "/opt/$HOME"):
            with self.subTest(value=value), self.assertRaises(deploy.ConfigError):
                self.config("NODE_IP=192.0.2.10\nINSTALL_PREFIX=" + value)
        settings, _ = self.config("NODE_IP=192.0.2.10\nINSTALL_PREFIX=/opt/serverwatch/\nSTATE_FILE=/var/cache/serverwatch/status.json")
        self.assertEqual(settings["install_prefix"], "/opt/serverwatch")
        self.assertEqual(settings["state_file"], "/var/cache/serverwatch/status.json")

    def test_ssh_alias_host_validation_and_port_inheritance(self):
        for target in ("-oProxyCommand=evil", "root@host;whoami", "root@host with space", "a@b@c", "root@$(touch_x)"):
            with self.subTest(target=target), self.assertRaises(deploy.ConfigError):
                self.config("ROOT_SSH=" + target)
        with self.assertRaises(deploy.ConfigError):
            self.config("ROOT_SSH=my-vps\nROOT_USER=root")
        _, alias = self.config("ROOT_SSH=my-vps\nROOT_USER=\nNODE_IP=\nSSH_PORT=")
        args = deploy.ssh_args(alias)
        self.assertEqual(args[-2:], ["--", "my-vps"])
        self.assertNotIn("-p", args)
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("ForwardAgent=no", args)
        _, ipv6 = self.config("NODE_IP=2001:db8::42\nROOT_USER=operator")
        self.assertEqual(ipv6["target"], "operator@2001:db8::42")

    def test_private_key_with_spaces_and_metacharacters_is_one_argument(self):
        keys = self.root / "keys"
        keys.mkdir()
        name = "private $(touch SHOULD_NOT_EXIST) & key"
        identity = keys / name
        identity.write_text("placeholder")
        _, connection = self.config("NODE_IP=192.0.2.10\nSSH_PORT=2222\nSSH_KEY=keys/" + name)
        args = deploy.ssh_args(connection)
        self.assertEqual(args[args.index("-i") + 1], str(identity))
        self.assertEqual(args[args.index("-p") + 1], "2222")
        self.assertEqual(args[-1], "root@192.0.2.10")
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_local_mode_ignores_remote_connection_fields(self):
        settings, connection = self.config("NODE_IP=-invalid\nROOT_USER=invalid name\nROOT_SSH=a@b@c\nSSH_PORT=bad\nSSH_KEY=/missing/private-key\nENABLE_SERVICE=false", local=True)
        self.assertFalse(settings["enable_service"])
        self.assertEqual(connection, {"target": None, "port": None, "key": None})

    def test_dry_run_does_not_build_connect_or_write(self):
        self.config("NODE_IP=192.0.2.10\nENABLE_SERVICE=false\nENABLE_LINGER=true")
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        out = io.StringIO()
        with patch.object(deploy, "make_bundle") as build, patch.object(deploy.subprocess, "run") as run, contextlib.redirect_stdout(out):
            code = deploy.main(["--env", str(self.env), "--dry-run"])
        self.assertEqual(code, 0)
        build.assert_not_called()
        run.assert_not_called()
        self.assertIn("root@192.0.2.10", out.getvalue())
        self.assertIn("CLI only", out.getvalue())
        self.assertIn("leave unchanged", out.getvalue())
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_remote_check_transports_only_bundle_and_never_interpolates_config(self):
        settings, connection = self.config("NODE_IP=192.0.2.10\nINSTALL_PREFIX=/opt/unique-config-prefix\nSTATE_FILE=/tmp/unique-config-state/status.json")
        recorded = {}
        def remote(command, **kwargs):
            recorded["command"] = command
            with tarfile.open(fileobj=kwargs["stdin"], mode="r:") as archive:
                recorded["names"] = archive.getnames()
                recorded["config"] = json.load(archive.extractfile("config.json"))
                self.assertTrue(all(member.isfile() for member in archive.getmembers()))
            return subprocess.CompletedProcess(command, 0)
        with patch.object(deploy, "make_bundle", side_effect=self.fake_bundle), patch.object(deploy.subprocess, "run", side_effect=remote):
            deploy.execute(settings, connection, self.options(check=True, upgrade=True))
        self.assertEqual(recorded["names"], ["config.json", "deploy_target.py", "serverwatch.pyz"])
        self.assertEqual(recorded["config"], settings)
        command = recorded["command"]
        self.assertEqual(command[0], "ssh")
        self.assertEqual(command[-2], "root@192.0.2.10")
        self.assertTrue(command[-1].endswith(" --check --upgrade"))
        self.assertNotIn("unique-config-prefix", command[-1])
        self.assertNotIn("unique-config-state", command[-1])
        self.assertIn("trap", command[-1])

    def test_local_check_invokes_worker_directly_and_cleans_bundle(self):
        settings, connection = self.config("ENABLE_SERVICE=false", local=True)
        observed = {}
        def target(command, **kwargs):
            observed["command"] = command
            bundle = Path(command[command.index("--bundle") + 1])
            observed["bundle"] = bundle
            self.assertTrue((bundle / "serverwatch.pyz").exists())
            return subprocess.CompletedProcess(command, 0)
        with patch.object(deploy, "make_bundle", side_effect=self.fake_bundle), patch.object(deploy.subprocess, "run", side_effect=target):
            deploy.execute(settings, connection, self.options(local=True, check=True))
        self.assertEqual(observed["command"][0], sys.executable)
        self.assertEqual(observed["command"][-1], "--check")
        self.assertNotIn("ssh", observed["command"])
        self.assertFalse(observed["bundle"].exists())

    def test_build_failure_prevents_remote_contact(self):
        settings, connection = self.config()
        failure = subprocess.CalledProcessError(5, ["build.py"])
        with patch.object(deploy.subprocess, "run", side_effect=failure) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                deploy.execute(settings, connection, self.options())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], sys.executable)
        self.assertTrue(run.call_args.args[0][1].endswith("scripts/build.py"))

    def test_missing_config_remote_failure_and_conflicting_modes_are_errors(self):
        stderr = io.StringIO()
        with patch.object(deploy.subprocess, "run") as run, contextlib.redirect_stderr(stderr):
            self.assertEqual(deploy.main(["--env", str(self.root / "missing.env"), "--dry-run"]), 1)
            run.assert_not_called()
        self.assertIn("configuration not found", stderr.getvalue())
        self.config()
        with patch.object(deploy, "make_bundle", side_effect=self.fake_bundle), \
             patch.object(deploy.subprocess, "run", side_effect=subprocess.CalledProcessError(255, ["ssh"])), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(deploy.main(["--env", str(self.env), "--check"]), 1)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            deploy.main(["--dry-run", "--check"])
        self.assertEqual(error.exception.code, 2)

    def test_shell_entry_works_from_checkout_with_spaces_and_ampersand(self):
        checkout = self.root / "Project with spaces & punctuation"
        scripts = checkout / "scripts"
        scripts.mkdir(parents=True)
        for name in ("deploy-serverwatch.sh", "deploy_serverwatch.py"):
            shutil.copyfile(PROJECT / "scripts" / name, scripts / name)
        (scripts / "new_node.env").write_text("NODE_IP=192.0.2.10\nENABLE_SERVICE=false\n")
        for option in ("--help", "--dry-run"):
            with self.subTest(option=option):
                result = subprocess.run(["bash", str(scripts / "deploy-serverwatch.sh"), option], cwd=self.root,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(result.stdout, "")
                if option == "--dry-run":
                    self.assertIn("root@192.0.2.10", result.stdout)
        self.assertFalse((checkout / "dist").exists())
        self.assertEqual(set(p.name for p in scripts.iterdir()), {"deploy-serverwatch.sh", "deploy_serverwatch.py", "new_node.env"})


if __name__ == "__main__":
    unittest.main()
