import ast
import asyncio
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path

from app import provisioning_runtime


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_PATH = PROJECT_ROOT / "app" / "application.py"
PROVISIONING_RUNTIME_PATH = PROJECT_ROOT / "app" / "provisioning_runtime.py"
WIFI_PATH = PROJECT_ROOT / "web" / "wifi.py"


class FakeWLAN:
    def __init__(self, operations):
        self.operations = operations

    def active(self, value):
        self.operations.append(("wlan-active", value))

    def connect(self, ssid, password):
        self.operations.append(("wlan-connect", ssid, password))

    def disconnect(self):
        self.operations.append(("wlan-disconnect-direct",))


class RuntimeHarness:
    def __init__(
        self,
        ble_failure=None,
        coordinator_failure=None,
        prepare_error=None,
        release_error=None,
        indicator_start_error=None,
        indicator_stop_error=None,
    ):
        self.operations = []
        self.ble_failure = ble_failure
        self.coordinator_failure = coordinator_failure
        self.prepare_error = prepare_error
        self.release_error = release_error
        self.indicator_start_error = indicator_start_error
        self.indicator_stop_error = indicator_stop_error
        self.station = FakeWLAN(self.operations)
        self.channels = []
        self.network_managers = []
        self.ble_instances = []
        self.coordinators = []
        self.indicators = []
        self.reset_calls = 0

    @contextmanager
    def dependencies(self):
        harness = self

        network = types.ModuleType("network")
        network.STA_IF = object()

        def wlan(interface):
            harness.operations.append(("station-construct", interface))
            return harness.station

        network.WLAN = wlan

        bootstrap = types.ModuleType("lib.ble_bootstrap")

        def prepare():
            harness.operations.append(("ble-prepare",))
            if harness.prepare_error is not None:
                raise harness.prepare_error

        def release():
            harness.operations.append(("ble-release",))
            if harness.release_error is not None:
                raise harness.release_error

        bootstrap.prepare_ble_controller = prepare
        bootstrap.release_ble_controller = release

        channel_module = types.ModuleType("lib.async_channel")

        class FakeChannel:
            def __init__(self):
                harness.operations.append(("channel-construct", self))
                harness.channels.append(self)

        channel_module.SingleValueChannel = FakeChannel

        wifi = types.ModuleType("web.wifi")

        class FakeNetworkManager:
            def __init__(self, wlan):
                self.wlan = wlan
                self.disconnect_calls = 0
                harness.operations.append(("network-construct", wlan))
                harness.network_managers.append(self)

            def disconnect(self):
                self.disconnect_calls += 1
                harness.operations.append(("network-disconnect", self.wlan))

        wifi.NetworkManager = FakeNetworkManager

        ble = types.ModuleType("lib.ble_provisioning")

        class FakeBleProvisioner:
            def __init__(self, channel):
                self.channel = channel
                self.failure = harness.ble_failure
                self.started = asyncio.Event()
                self.status_characteristic = None
                harness.operations.append(("ble-construct", channel))
                harness.ble_instances.append(self)

            async def run(self):
                harness.operations.append(("ble-start",))
                self.status_characteristic = object()
                harness.operations.append(("ble-service-ready",))
                self.started.set()
                try:
                    while True:
                        await asyncio.sleep(1)
                finally:
                    harness.operations.append(("ble-stop",))

            def raise_if_failed(self):
                if self.failure is not None:
                    raise self.failure

        ble.BleProvisioner = FakeBleProvisioner

        coordinator_module = types.ModuleType("app.provisioning")

        class FakeCoordinator:
            def __init__(self, network_manager, credential_store, channel):
                self.network_manager = network_manager
                self.credential_store = credential_store
                self.channel = channel
                self.failure = harness.coordinator_failure
                self.provisioned = asyncio.Event()
                self.started = asyncio.Event()
                harness.operations.append(
                    (
                        "coordinator-construct",
                        network_manager,
                        credential_store,
                        channel,
                    )
                )
                harness.coordinators.append(self)

            async def run(self):
                harness.operations.append(("coordinator-start",))
                self.started.set()
                try:
                    while not self.provisioned.is_set() and self.failure is None:
                        await asyncio.sleep(0)
                finally:
                    harness.operations.append(("coordinator-stop",))

            def raise_if_failed(self):
                if self.failure is not None:
                    raise self.failure

        coordinator_module.ProvisioningCoordinator = FakeCoordinator

        config = types.ModuleType("config")
        config.STATUS_LED_PIN = 2

        indicator_module = types.ModuleType("led.provisioning_indicator")

        class FakeIndicator:
            def __init__(self, pin_number):
                self.pin_number = pin_number
                harness.operations.append(("indicator-construct", pin_number))
                harness.indicators.append(self)

            def start(self):
                harness.operations.append(("indicator-start",))
                if harness.indicator_start_error is not None:
                    raise harness.indicator_start_error

            def stop(self):
                harness.operations.append(("indicator-stop",))
                if harness.indicator_stop_error is not None:
                    raise harness.indicator_stop_error

        indicator_module.ProvisioningIndicator = FakeIndicator

        replacements = {
            "network": network,
            "lib.ble_bootstrap": bootstrap,
            "lib.async_channel": channel_module,
            "lib.ble_provisioning": ble,
            "web.wifi": wifi,
            "app.provisioning": coordinator_module,
            "config": config,
            "led.provisioning_indicator": indicator_module,
        }
        old_modules = {name: sys.modules.get(name) for name in replacements}
        old_settle = provisioning_runtime.BLE_SETTLE_S
        old_monitor = provisioning_runtime.MONITOR_INTERVAL_S
        provisioning_runtime.BLE_SETTLE_S = 0
        provisioning_runtime.MONITOR_INTERVAL_S = 0
        for name, module in replacements.items():
            sys.modules[name] = module

        try:
            yield
        finally:
            provisioning_runtime.BLE_SETTLE_S = old_settle
            provisioning_runtime.MONITOR_INTERVAL_S = old_monitor
            for name, previous in old_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

    def reset(self):
        self.reset_calls += 1
        self.operations.append(("reset",))


class ProvisioningRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def _start(self, harness):
        runtime = provisioning_runtime.ProvisioningRuntime(
            object(),
            reset=harness.reset,
        )
        task = asyncio.create_task(runtime.run())
        await asyncio.wait_for(runtime.ready.wait(), timeout=0.25)
        return runtime, task

    async def test_success_uses_one_channel_and_resets_after_ordered_cleanup(self):
        harness = RuntimeHarness()
        with harness.dependencies():
            runtime, task = await self._start(harness)

            self.assertIs(runtime.station, harness.station)
            self.assertIs(runtime.network_manager.wlan, harness.station)
            self.assertIs(runtime.ble_provisioner.channel, harness.channels[0])
            self.assertIs(runtime.coordinator.channel, harness.channels[0])
            self.assertEqual(runtime.indicator.pin_number, 2)
            self.assertEqual(harness.reset_calls, 0)

            runtime.coordinator.provisioned.set()
            await asyncio.wait_for(task, timeout=0.25)

        names = [operation[0] for operation in harness.operations]
        self.assertFalse(any(name.startswith("wlan-") for name in names))
        self.assertLess(names.index("station-construct"), names.index("ble-prepare"))
        self.assertLess(names.index("ble-service-ready"), names.index("indicator-start"))
        self.assertLess(names.index("coordinator-stop"), names.index("indicator-stop"))
        self.assertLess(names.index("indicator-stop"), names.index("ble-stop"))
        self.assertLess(names.index("ble-stop"), names.index("network-disconnect"))
        self.assertLess(names.index("network-disconnect"), names.index("ble-release"))
        self.assertLess(names.index("ble-release"), names.index("reset"))
        self.assertEqual(harness.reset_calls, 1)

    async def test_cancellation_cleans_up_without_reset(self):
        harness = RuntimeHarness()
        with harness.dependencies():
            runtime, task = await self._start(harness)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        names = [operation[0] for operation in harness.operations]
        self.assertIn("coordinator-stop", names)
        self.assertIn("indicator-stop", names)
        self.assertIn("ble-stop", names)
        self.assertIn("network-disconnect", names)
        self.assertIn("ble-release", names)
        self.assertNotIn("reset", names)
        self.assertFalse(runtime.ready.is_set())
        self.assertIsNone(runtime.indicator)

    async def test_component_failure_is_visible_and_prevents_reset(self):
        for owner in ("ble", "coordinator"):
            with self.subTest(owner=owner):
                failure = RuntimeError(owner + " failed")
                harness = RuntimeHarness()
                with harness.dependencies():
                    runtime, task = await self._start(harness)
                    if owner == "ble":
                        runtime.ble_provisioner.failure = failure
                    else:
                        runtime.coordinator.failure = failure
                    with self.assertRaisesRegex(RuntimeError, owner + " failed"):
                        await asyncio.wait_for(task, timeout=0.25)
                self.assertEqual(harness.reset_calls, 0)

    async def test_cleanup_failure_does_not_mask_primary_failure(self):
        harness = RuntimeHarness(
            release_error=OSError("cleanup failure"),
        )
        with harness.dependencies():
            runtime, task = await self._start(harness)
            runtime.ble_provisioner.failure = RuntimeError(
                "primary BLE failure"
            )
            with self.assertRaisesRegex(RuntimeError, "primary BLE failure"):
                await asyncio.wait_for(task, timeout=0.25)

    async def test_clean_cleanup_failure_prevents_reset_and_surfaces(self):
        harness = RuntimeHarness(release_error=OSError("release failed"))
        with harness.dependencies():
            runtime, task = await self._start(harness)
            runtime.coordinator.provisioned.set()
            with self.assertRaisesRegex(OSError, "release failed"):
                await asyncio.wait_for(task, timeout=0.25)
        self.assertEqual(harness.reset_calls, 0)

    async def test_prepare_failure_releases_best_effort_and_builds_no_graph(self):
        harness = RuntimeHarness(prepare_error=OSError("reserve failed"))
        runtime = provisioning_runtime.ProvisioningRuntime(
            object(),
            reset=harness.reset,
        )
        with harness.dependencies():
            with self.assertRaisesRegex(OSError, "reserve failed"):
                await runtime.run()

        names = [operation[0] for operation in harness.operations]
        self.assertEqual(names[:2], ["station-construct", "ble-prepare"])
        self.assertNotIn("network-construct", names)
        self.assertNotIn("ble-construct", names)
        self.assertIn("ble-release", names)
        self.assertNotIn("reset", names)

    async def test_indicator_start_failure_does_not_block_provisioning(self):
        harness = RuntimeHarness(
            indicator_start_error=MemoryError("indicator allocation failed")
        )
        with harness.dependencies():
            runtime, task = await self._start(harness)
            self.assertIsNone(runtime.indicator)
            runtime.coordinator.provisioned.set()
            await asyncio.wait_for(task, timeout=0.25)

        self.assertEqual(harness.reset_calls, 1)
        self.assertIn(("indicator-start",), harness.operations)

    async def test_indicator_stop_failure_never_masks_success_or_reset(self):
        harness = RuntimeHarness(
            indicator_stop_error=OSError("indicator cleanup failed")
        )
        with harness.dependencies():
            runtime, task = await self._start(harness)
            runtime.coordinator.provisioned.set()
            await asyncio.wait_for(task, timeout=0.25)

        self.assertEqual(harness.reset_calls, 1)
        self.assertIn(("indicator-stop",), harness.operations)


class ModeIsolationArchitectureTests(unittest.TestCase):
    @staticmethod
    def _top_level_imports(path):
        tree = ast.parse(path.read_text(), filename=str(path))
        imports = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        return imports

    def test_provisioning_runtime_has_only_mode_neutral_top_level_imports(self):
        self.assertEqual(
            self._top_level_imports(PROVISIONING_RUNTIME_PATH),
            ["asyncio", "gc"],
        )

    def test_running_application_cannot_import_ble_or_provisioning(self):
        imports = self._top_level_imports(APPLICATION_PATH)
        forbidden = {
            "aioble",
            "bluetooth",
            "lib.ble_bootstrap",
            "lib.ble_provisioning",
            "app.provisioning",
            "app.provisioning_runtime",
        }
        self.assertFalse(forbidden.intersection(imports))

        source = APPLICATION_PATH.read_text()
        self.assertNotIn("BleProvisioner", source)
        self.assertNotIn("ProvisioningCoordinator", source)

    def test_network_manager_import_does_not_construct_hardware_config(self):
        imports = self._top_level_imports(WIFI_PATH)
        self.assertNotIn("config", imports)
        self.assertIn("web.network_config", imports)

    def test_running_composition_rejects_missing_credentials(self):
        tree = ast.parse(
            APPLICATION_PATH.read_text(),
            filename=str(APPLICATION_PATH),
        )
        compose = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "create_application"
        )
        self.assertTrue(
            any(
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and any(
                    isinstance(comparator, ast.Constant)
                    and comparator.value is None
                    for comparator in node.test.comparators
                )
                for node in ast.walk(compose)
            )
        )


if __name__ == "__main__":
    unittest.main()
