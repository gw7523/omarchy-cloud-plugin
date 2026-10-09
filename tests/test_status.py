#!/usr/bin/env python3

import importlib.util
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import mock_open, patch


STATUS_PATH = Path(__file__).resolve().parents[1] / "bin" / "omarchy-cloud-status"
LOADER = SourceFileLoader("omarchy_cloud_status", str(STATUS_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
status = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(status)


class FailedMountStatusTests(unittest.TestCase):
    def test_reads_failure_from_selected_legacy_unit(self):
        systemctl_output = """\
Id=omarchy-cloud-mount@gdrive.service
ActiveState=inactive
UnitFileState=disabled

Id=rclone-mount@gdrive.service
ActiveState=failed
UnitFileState=enabled
"""
        commands = []

        def fake_run(command, timeout=5):
            commands.append(command)
            if command[0] == "systemctl":
                return 0, systemctl_output
            if command[0] == "journalctl":
                return 0, "ERROR : mount failed"
            self.fail(f"unexpected command: {command}")

        with patch.object(status, "run", side_effect=fake_run):
            unit = status.unit_states(["gdrive"])["gdrive"]
            detail = status.last_failure(unit["unit"])

        self.assertEqual(unit["active"], "failed")
        self.assertEqual(unit["unit"], "rclone-mount@gdrive.service")
        self.assertEqual(detail, "ERROR : mount failed")
        self.assertEqual(
            commands[1][0:5],
            [
                "journalctl",
                "--user",
                "-u",
                "rclone-mount@gdrive.service",
                "-n",
            ],
        )


class ExpiredSignInTests(unittest.TestCase):
    ICLOUD_421 = (
        'ERROR : IO error: HTTP error 421 (421 Misdirected Request) returned body: '
        '"{\\"reason\\":\\"Invalid global session\\",\\"error\\":2}"'
    )

    def test_reads_invocation_id(self):
        out = "Id=omarchy-cloud-mount@icloud.service\nActiveState=active\nUnitFileState=enabled\nInvocationID=abc123\n"
        with patch.object(status, "run", return_value=(0, out)):
            unit = status.unit_states(["icloud"])["icloud"]
        self.assertEqual(unit["invocation"], "abc123")

    def test_icloud_invalid_session_in_current_run_is_expired(self):
        commands = []

        def fake_run(command, timeout=5):
            commands.append(command)
            return 0, self.ICLOUD_421

        with patch.object(status, "run", side_effect=fake_run):
            self.assertTrue(status.sign_in_expired({"invocation": "abc123"}))
        self.assertIn("_SYSTEMD_INVOCATION_ID=abc123", commands[0])

    def test_oauth_invalid_grant_is_expired(self):
        with patch.object(status, "run", return_value=(0, 'ERROR : couldn\'t fetch token: invalid_grant')):
            self.assertTrue(status.sign_in_expired({"invocation": "abc123"}))

    def test_other_errors_are_not_expired(self):
        with patch.object(status, "run", return_value=(0, "ERROR : vfs cache: failed to upload")):
            self.assertFalse(status.sign_in_expired({"invocation": "abc123"}))

    def test_no_invocation_skips_the_journal(self):
        with patch.object(status, "run", side_effect=AssertionError("journal read")):
            self.assertFalse(status.sign_in_expired({"invocation": ""}))
            self.assertFalse(status.sign_in_expired({}))


class MountInfoTests(unittest.TestCase):
    def test_preserves_utf8_and_decodes_only_kernel_octal_escapes(self):
        mountinfo = (
            "36 29 0:42 / /tmp/Café/My\\040Drive rw,nosuid,nodev "
            "- fuse.rclone gdrive: rw\n"
        )

        with patch("builtins.open", mock_open(read_data=mountinfo)):
            paths = status.mounted_paths()

        self.assertEqual(paths, {"/tmp/Café/My Drive"})


if __name__ == "__main__":
    unittest.main()
