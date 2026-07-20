import ast
import asyncio
import builtins
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from lib import ble_bootstrap


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = PROJECT_ROOT / "main.py"
BOOTSTRAP_PATH = PROJECT_ROOT / "lib" / "ble_bootstrap.py"


class RecordingAioble:
    def __init__(self, error=None, stop_error=None):
        self.error = error
        self.stop_error = stop_error
        self.config_calls = []
        self.stop_calls = 0

    def config(self, **kwargs):
        self.config_calls.append(kwargs)
        if self.error is not None:
            raise self.error

    def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error


class BootHarness:
    TRACKED_IMPORTS = {
        "web.credentials",
        "app.provisioning_runtime",
        "app.application",
        "lib.ble_bootstrap",
        "lib.ble_provisioning",
    }

    def __init__(
        self,
        credentials,
        recovery_error=None,
        load_error=None,
        provisioning_import_error=None,
        provisioning_error=None,
        application_import_error=None,
        create_error=None,
        run_error=None,
    ):
        self.credentials = credentials
        self.recovery_error = recovery_error
        self.load_error = load_error
        self.provisioning_import_error = provisioning_import_error
        self.provisioning_error = provisioning_error
        self.application_import_error = application_import_error
        self.create_error = create_error
        self.run_error = run_error
        self.operations = []
        self.top_level_operations = []
        self.store_instances = []

    async def execute(self):
        harness = self

        async def recovery_sleep(delay):
            harness.operations.append(("recovery-wait", delay))
            if harness.recovery_error is not None:
                raise harness.recovery_error
            await asyncio.sleep(0)

        class FakeCredentialStore:
            def __init__(self):
                harness.operations.append(("store-construct", self))
                harness.store_instances.append(self)

            def load(self):
                harness.operations.append(("store-load", self))
                if harness.load_error is not None:
                    raise harness.load_error
                return harness.credentials

        credentials_module = types.ModuleType("web.credentials")
        credentials_module.CredentialStore = FakeCredentialStore

        async def run_provisioning(store):
            harness.operations.append(("provisioning-run", store))
            if harness.provisioning_error is not None:
                raise harness.provisioning_error

        provisioning_module = types.ModuleType("app.provisioning_runtime")
        provisioning_module.run_provisioning = run_provisioning

        class FakeApplication:
            async def run(self):
                harness.operations.append(("application-run", self))
                if harness.run_error is not None:
                    raise harness.run_error

        def create_application(credentials):
            harness.operations.append(("application-construct", credentials))
            if harness.create_error is not None:
                raise harness.create_error
            return FakeApplication()

        application_module = types.ModuleType("app.application")
        application_module.create_application = create_application

        replacements = {
            "web.credentials": credentials_module,
            "app.provisioning_runtime": provisioning_module,
            "app.application": application_module,
        }
        old_modules = {name: sys.modules.get(name) for name in replacements}
        for name, module in replacements.items():
            sys.modules[name] = module

        original_import = builtins.__import__

        def tracked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name in harness.TRACKED_IMPORTS:
                harness.operations.append(("import", name))
                if (
                    name == "app.provisioning_runtime"
                    and harness.provisioning_import_error is not None
                ):
                    raise harness.provisioning_import_error
                if (
                    name == "app.application"
                    and harness.application_import_error is not None
                ):
                    raise harness.application_import_error
            return original_import(name, globals, locals, fromlist, level)

        try:
            with mock.patch("builtins.__import__", side_effect=tracked_import):
                spec = importlib.util.spec_from_file_location(
                    "minimal_main_under_test",
                    MAIN_PATH,
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                harness.top_level_operations = list(harness.operations)
                await module.main(recovery_sleep)
        finally:
            for name, previous in old_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous


class BleBootstrapTests(unittest.TestCase):
    def test_default_and_boundary_payloads_map_to_att_mtu(self):
        for max_payload_bytes, expected_mtu in ((None, 259), (1, 4), (514, 517)):
            with self.subTest(max_payload_bytes=max_payload_bytes):
                aioble = RecordingAioble()
                if max_payload_bytes is None:
                    ble_bootstrap.prepare_ble_controller(aioble_module=aioble)
                else:
                    ble_bootstrap.prepare_ble_controller(
                        max_payload_bytes,
                        aioble_module=aioble,
                    )
                self.assertEqual(aioble.config_calls, [{"mtu": expected_mtu}])

    def test_invalid_payload_limits_fail_before_controller_mutation(self):
        cases = (
            (True, TypeError),
            (1.5, TypeError),
            ("256", TypeError),
            (0, ValueError),
            (-1, ValueError),
            (515, ValueError),
        )
        for value, error_type in cases:
            with self.subTest(value=value):
                aioble = RecordingAioble()
                with self.assertRaises(error_type):
                    ble_bootstrap.prepare_ble_controller(
                        value,
                        aioble_module=aioble,
                    )
                self.assertEqual(aioble.config_calls, [])

    def test_missing_config_api_fails_closed(self):
        for aioble in (object(), types.SimpleNamespace(config=None)):
            with self.subTest(aioble=aioble):
                with self.assertRaisesRegex(RuntimeError, "config"):
                    ble_bootstrap.prepare_ble_controller(aioble_module=aioble)

    def test_configuration_failure_preserves_root_cause(self):
        aioble = RecordingAioble(
            error=OSError("NimBLE allocation failed"),
            stop_error=RuntimeError("stop also failed"),
        )
        with self.assertRaisesRegex(OSError, "NimBLE allocation failed"):
            ble_bootstrap.prepare_ble_controller(aioble_module=aioble)
        self.assertEqual(aioble.stop_calls, 1)

    def test_release_is_safe_to_repeat_and_missing_stop_is_a_noop(self):
        aioble = RecordingAioble()
        ble_bootstrap.release_ble_controller(aioble)
        ble_bootstrap.release_ble_controller(aioble)
        ble_bootstrap.release_ble_controller(object())
        self.assertEqual(aioble.stop_calls, 2)

    def test_bootstrap_has_no_heavy_top_level_imports(self):
        tree = ast.parse(BOOTSTRAP_PATH.read_text(), filename=str(BOOTSTRAP_PATH))
        imports = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        self.assertEqual(imports, [])


class MinimalBootSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_precedes_credential_and_mode_state(self):
        for credentials in (None, object()):
            with self.subTest(credentials=credentials):
                harness = BootHarness(credentials)
                await harness.execute()
                runtime = harness.operations[len(harness.top_level_operations):]
                self.assertEqual(runtime[0][0], "recovery-wait")
                self.assertGreaterEqual(runtime[0][1], 3)
                self.assertEqual(runtime[1][0], "store-construct")

    async def test_recovery_interruption_starts_nothing(self):
        harness = BootHarness(None, recovery_error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await harness.execute()
        runtime = harness.operations[len(harness.top_level_operations):]
        self.assertEqual([item[0] for item in runtime], ["recovery-wait"])

    async def test_uncredentialed_boot_runs_only_provisioning_branch(self):
        harness = BootHarness(None)
        await harness.execute()

        names = [item[0] for item in harness.operations]
        self.assertIn(("import", "app.provisioning_runtime"), harness.operations)
        self.assertNotIn(("import", "app.application"), harness.operations)
        self.assertNotIn(("import", "lib.ble_provisioning"), harness.operations)
        self.assertEqual(names.count("provisioning-run"), 1)
        self.assertNotIn("application-construct", names)
        self.assertIs(
            next(item[1] for item in harness.operations if item[0] == "provisioning-run"),
            harness.store_instances[0],
        )

    async def test_credentialed_boot_runs_only_application_branch(self):
        credentials = object()
        harness = BootHarness(credentials)
        await harness.execute()

        self.assertIn(("import", "app.application"), harness.operations)
        self.assertNotIn(("import", "app.provisioning_runtime"), harness.operations)
        self.assertNotIn(("import", "lib.ble_bootstrap"), harness.operations)
        self.assertNotIn(("import", "lib.ble_provisioning"), harness.operations)
        self.assertIn(("application-construct", credentials), harness.operations)
        self.assertEqual(
            [item[0] for item in harness.operations].count("application-run"),
            1,
        )

    async def test_credential_load_failure_imports_neither_mode(self):
        harness = BootHarness(None, load_error=OSError("NVS read failed"))
        with self.assertRaisesRegex(OSError, "NVS read failed"):
            await harness.execute()
        self.assertNotIn(("import", "app.application"), harness.operations)
        self.assertNotIn(("import", "app.provisioning_runtime"), harness.operations)

    async def test_branch_import_and_runtime_failures_are_not_swallowed(self):
        cases = (
            BootHarness(
                None,
                provisioning_import_error=ImportError("no provisioning graph"),
            ),
            BootHarness(None, provisioning_error=RuntimeError("provisioning failed")),
            BootHarness(
                object(),
                application_import_error=ImportError("no application graph"),
            ),
            BootHarness(object(), create_error=RuntimeError("compose failed")),
            BootHarness(object(), run_error=RuntimeError("application failed")),
        )
        patterns = (
            "no provisioning graph",
            "provisioning failed",
            "no application graph",
            "compose failed",
            "application failed",
        )
        for harness, pattern in zip(cases, patterns):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex((ImportError, RuntimeError), pattern):
                    await harness.execute()

    def test_main_has_only_mode_neutral_top_level_imports(self):
        tree = ast.parse(MAIN_PATH.read_text(), filename=str(MAIN_PATH))
        imports = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        self.assertEqual(imports, ["asyncio", "web.credentials"])
        self.assertFalse(
            any(
                isinstance(node, ast.ClassDef) and node.name == "Application"
                for node in tree.body
            )
        )


if __name__ == "__main__":
    unittest.main()
