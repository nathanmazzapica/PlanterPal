import contextlib
import io
import unittest
from unittest import mock

from tools import deploy


class DeploymentManifestTests(unittest.TestCase):
    def test_manifest_covers_every_production_source(self):
        declared = set(deploy.validate_manifest())

        self.assertEqual(declared, deploy.discover_device_sources())
        self.assertNotIn("boot.py", declared)
        self.assertNotIn("web/wifi_config.py", declared)
        self.assertNotIn("web/wifi_config.example.py", declared)

    def test_manifest_has_complete_local_import_closure(self):
        missing = deploy.missing_local_dependencies(
            deploy.SUPPORT_FILES + (deploy.ENTRY_POINT,)
        )

        self.assertEqual(missing, [])

    def test_omitted_import_dependency_is_detected(self):
        incomplete = tuple(
            path
            for path in deploy.SUPPORT_FILES
            if path != "lib/async_channel.py"
        )

        missing = deploy.missing_local_dependencies(
            incomplete + (deploy.ENTRY_POINT,)
        )

        self.assertIn(
            (
                "app/application.py",
                "lib.async_channel",
                "lib/async_channel.py",
            ),
            missing,
        )
        with self.assertRaisesRegex(
            deploy.DeploymentError,
            "omitted production files: lib/async_channel.py",
        ):
            deploy.validate_manifest(support_files=incomplete)

    def test_entry_point_follows_support_verification_and_both_probes(self):
        plan = deploy.build_plan(clean=True, reset=True)
        names = [step.name for step in plan]
        entry_index = names.index("copy_entry_point")

        self.assertLess(names.index("create_directories"), names.index("copy_support"))
        self.assertLess(names.index("copy_support"), names.index("verify_support"))
        self.assertLess(names.index("verify_support"), entry_index)
        probe_indexes = [index for index, name in enumerate(names) if name == "run_probe"]
        reset_indexes = [
            index for index, name in enumerate(names) if name == "hard_reset_for_probe"
        ]
        self.assertEqual(len(reset_indexes), len(deploy.HARDWARE_PROBES))
        self.assertEqual(
            reset_indexes,
            [index - 1 for index in probe_indexes],
        )
        self.assertTrue(
            all(index < entry_index for index, name in enumerate(names) if name == "run_probe")
        )
        self.assertEqual(
            [step.detail for step in plan if step.name == "run_probe"],
            list(deploy.HARDWARE_PROBES),
        )
        self.assertEqual(names[-2:], ["verify_entry_point", "reset"])

    def test_clean_plan_removes_entry_before_any_file_copy(self):
        plan = deploy.build_plan(clean=True, reset=False)
        names = [step.name for step in plan]

        self.assertLess(names.index("remove_entry_point"), names.index("copy_support"))
        self.assertLess(names.index("verify_clean"), names.index("copy_support"))
        self.assertNotIn("boot.py", deploy._remove_code(deploy.SUPPORT_FILES))

    def test_dry_run_validates_and_never_requires_mpremote(self):
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            deploy.deploy(
                "/dev/example",
                clean=True,
                assume_yes=False,
                dry_run=True,
            )

        rendered = output.getvalue()
        self.assertIn("Manifest valid", rendered)
        self.assertIn("copy_support", rendered)
        self.assertIn("copy_entry_point", rendered)
        self.assertLess(rendered.index("copy_support"), rendered.index("copy_entry_point"))

    def test_live_clean_requires_explicit_confirmation(self):
        with mock.patch.object(deploy.shutil, "which", return_value="mpremote"):
            with self.assertRaisesRegex(deploy.DeploymentError, "--clean requires --yes"):
                deploy.deploy("/dev/example", clean=True, assume_yes=False)

    def test_device_session_uses_recovery_window_and_timeout(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return type("Completed", (), {"returncode": 0, "stdout": ""})()

        session = deploy.DeviceSession("/dev/example", runner=runner, printer=lambda _: None)
        session.run("fs", "ls")

        argv, kwargs = calls[0]
        self.assertEqual(
            argv[:6],
            ["mpremote", "connect", "/dev/example", "sleep", "1", "fs"],
        )
        self.assertEqual(kwargs["timeout"], deploy.COMMAND_TIMEOUT_S)

    def test_aioble_preflight_does_not_import_or_activate_ble(self):
        calls = []

        class Session:
            def execute(self, code, capture=False):
                calls.append(code)
                return "PP_AIOBLE_FILES_OK"

            def run(self, *commands):
                raise AssertionError("aioble should already be present")

        deploy._ensure_aioble(Session())

        self.assertNotIn("import aioble", calls[0])
        self.assertIn("lib/aioble/__init__.mpy", calls[0])


if __name__ == "__main__":
    unittest.main()
