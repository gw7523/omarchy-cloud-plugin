#!/usr/bin/env python3

import importlib.util
import os
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
MOUNT = REPO / "bin" / "omarchy-cloud-mount"

STATUS_PATH = REPO / "bin" / "omarchy-cloud-status"
LOADER = SourceFileLoader("omarchy_cloud_status_env", str(STATUS_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
status = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(status)


class RecordParsingTests(unittest.TestCase):
    def test_reads_environment_fields_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dropbox-sfl.conf"
            path.write_text(
                "# comment\n"
                "label=Dropbox\\ \\(SFL\\)\n"
                "extra_flags=''\n"
                "remote=dropbox\n"
                "env_kind=distrobox\n"
                "env_name=sfl\n"
                "mount_dir=/home/me/Work/sfl/.home/Dropbox\n",
                encoding="utf-8",
            )
            record = status.parse_record(path)

        self.assertEqual(record["label"], "Dropbox (SFL)")
        self.assertEqual(record["extra_flags"], "")
        self.assertEqual(record["remote"], "dropbox")
        self.assertEqual(record["env_kind"], "distrobox")
        self.assertEqual(record["env_name"], "sfl")
        self.assertEqual(record["mount_dir"], "/home/me/Work/sfl/.home/Dropbox")

    def test_legacy_record_means_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            remotes = Path(tmp) / "omarchy-cloud" / "remotes"
            remotes.mkdir(parents=True)
            (remotes / "gdrive.conf").write_text(
                "label='Google Drive'\nextra_flags='--drive-skip-gdocs'\n",
                encoding="utf-8",
            )
            with patch.object(status, "STATE_DIR", Path(tmp) / "omarchy-cloud"):
                services = status.managed_services()

        self.assertEqual(list(services), ["gdrive"])
        self.assertEqual(services["gdrive"]["envKind"], "host")
        self.assertEqual(services["gdrive"]["remote"], "gdrive")
        self.assertEqual(services["gdrive"]["mountDir"], "")

    def test_unsafe_container_name_is_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            remotes = Path(tmp) / "omarchy-cloud" / "remotes"
            remotes.mkdir(parents=True)
            (remotes / "bad.conf").write_text(
                "env_kind=docker\nenv_name='x; rm -rf /'\n", encoding="utf-8"
            )
            with patch.object(status, "STATE_DIR", Path(tmp) / "omarchy-cloud"):
                services = status.managed_services()

        self.assertEqual(services["bad"]["envKind"], "host")
        self.assertTrue(services["bad"]["brokenEnv"])


class EnvExecTests(unittest.TestCase):
    def test_builds_argv_without_a_shell(self):
        self.assertEqual(
            status.env_exec_command("host", "", "", ["rclone", "about", "x:"]),
            ["rclone", "about", "x:"],
        )
        self.assertEqual(
            status.env_exec_command("distrobox", "sfl", "", ["rclone", "about", "x:"]),
            ["distrobox", "enter", "--name", "sfl", "--", "rclone", "about", "x:"],
        )
        self.assertEqual(
            status.env_exec_command("docker", "dev", "me", ["true"]),
            ["docker", "exec", "-i", "-u", "me", "dev", "true"],
        )
        self.assertEqual(
            status.env_exec_command("podman", "dev", "", ["true"]),
            ["podman", "exec", "-i", "dev", "true"],
        )

    def test_probe_parses_home_remotes_and_mounts(self):
        probe_output = (
            "@@HOME\n/home/me/box\n"
            "@@RCLONE\nyes\n"
            "@@CONFIG\n[dropbox]\ntype = dropbox\ntoken = XXX\n\n"
            "[nextcloud]\ntype = webdav\nurl = https://x/remote.php/dav/files/me/\n"
            "### Double check the config for sensitive info before posting publicly\n"
            "@@MOUNTS\n"
            "771 874 0:91 / /home/me/box/Dropbox rw,nosuid - fuse.rclone dropbox: rw\n"
            "1 2 0:3 / / rw - ext4 /dev/sda1 rw\n"
        )

        def fake_run(command, timeout=5):
            self.assertEqual(command[:5], ["distrobox", "enter", "--name", "sfl", "--"])
            return 0, probe_output

        with patch.object(status, "env_running", return_value=True), \
             patch.object(status, "run", side_effect=fake_run):
            probe = status.probe_environment("distrobox", "sfl", "")

        self.assertTrue(probe["reachable"])
        self.assertTrue(probe["rclone"])
        self.assertEqual(probe["home"], "/home/me/box")
        self.assertEqual(probe["remotes"]["dropbox"], {"type": "dropbox", "hasAuth": True})
        self.assertEqual(probe["remotes"]["nextcloud"], {"type": "webdav", "hasAuth": False})
        self.assertEqual(probe["mounts"], {"/home/me/box/Dropbox": "dropbox:"})

    def test_stopped_container_is_unreachable_without_exec(self):
        with patch.object(status, "env_running", return_value=False), \
             patch.object(status, "run") as run:
            probe = status.probe_environment("docker", "dev", "")
        self.assertFalse(probe["reachable"])
        run.assert_not_called()


class MountPathTests(unittest.TestCase):
    def test_tilde_root_expands_to_the_container_home(self):
        service = {"name": "proton", "envKind": "distrobox", "mountDir": ""}
        self.assertEqual(
            status.mount_path_for(service, "~/Cloud", "/home/me/box"),
            "/home/me/box/Cloud/proton",
        )

    def test_explicit_mount_dir_wins(self):
        service = {"name": "dropbox-sfl", "envKind": "distrobox", "mountDir": "/srv/Dropbox"}
        self.assertEqual(status.mount_path_for(service, "~/Cloud", "/home/me/box"), "/srv/Dropbox")

    def test_host_root_expands_on_the_host(self):
        service = {"name": "gdrive", "envKind": "host", "mountDir": ""}
        self.assertEqual(
            status.mount_path_for(service, "~/Cloud", ""),
            str(Path.home() / "Cloud" / "gdrive"),
        )


class ContainerRunTests(unittest.TestCase):
    """`run` for a container service starts rclone through the container tool
    and never touches the host's rclone."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.home = self.root / "home"
        self.config = self.root / "config"
        self.fake_bin = self.root / "fake-bin"
        for path in (self.home, self.config, self.fake_bin):
            path.mkdir()
        self.log = self.root / "events.log"
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.config),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "PATH": f"{self.fake_bin}{os.pathsep}{self.env['PATH']}",
                "TEST_LOG": str(self.log),
                "NOTIFY_SOCKET": "",
            }
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def executable(self, name, source):
        path = self.fake_bin / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)

    def record(self, name, body):
        path = self.config / "omarchy-cloud" / "remotes" / f"{name}.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_run_mounts_inside_distrobox(self):
        # The fake container runs commands on this machine, so the folder has
        # to be somewhere this test may create.
        box_dir = self.root / "box" / "Dropbox"
        self.record(
            "dropbox-sfl",
            "label=Dropbox\nextra_flags=''\nremote=dropbox\n"
            f"env_kind=distrobox\nenv_name=sfl\nmount_dir={box_dir}\n",
        )
        # A fake distrobox that logs what it is asked to run inside the box
        # and executes it locally. Its `list` says the box is running.
        self.executable(
            "distrobox",
            """#!/bin/bash
if [[ "$1" == list ]]; then
  printf 'ID | NAME | STATUS | IMAGE\\nabc | sfl | Up 2 days | img\\n'
  exit 0
fi
shift 3  # enter --name sfl
[[ "$1" == "--" ]] && shift
printf 'box: %s\\n' "$*" >>"$TEST_LOG"
exec "$@"
""",
        )
        self.executable(
            "rclone",
            "#!/bin/bash\nprintf 'rclone: %s\\n' \"$*\" >>\"$TEST_LOG\"\nexit 0\n",
        )
        self.executable("fusermount3", "#!/bin/bash\nexit 0\n")

        result = subprocess.run(
            [str(MOUNT), "run", "dropbox-sfl"],
            check=False, capture_output=True, text=True, env=self.env, timeout=20,
        )

        events = self.log.read_text(encoding="utf-8")
        # The mount never appeared (fake rclone exits at once), so run reports
        # the failure rather than pretending the folder is live.
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"box: mkdir -p {box_dir}", events)
        self.assertIn(f"rclone: mount dropbox: {box_dir}", events)
        self.assertIn("--vfs-cache-mode full", events)
        # The cache lives under the container's home, which the fake reports
        # as this test's HOME.
        self.assertIn(f"--cache-dir {self.home}/.cache/omarchy-cloud/rclone", events)
        # The host rclone.conf is the wrong config for a container; none is passed.
        self.assertNotIn("--config", events)

    def test_run_refuses_a_container_that_cannot_start(self):
        self.record("x", "remote=x\nenv_kind=docker\nenv_name=missing\n")
        self.executable(
            "docker",
            "#!/bin/bash\nprintf 'docker: %s\\n' \"$*\" >>\"$TEST_LOG\"\nexit 1\n",
        )
        result = subprocess.run(
            [str(MOUNT), "run", "x"],
            check=False, capture_output=True, text=True, env=self.env, timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)

    def test_environments_lists_host_first(self):
        self.executable("distrobox", "#!/bin/bash\nexit 1\n")
        result = subprocess.run(
            [str(MOUNT), "environments"],
            check=False, capture_output=True, text=True, env=self.env, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "host\thost\ttrue")


if __name__ == "__main__":
    unittest.main()
