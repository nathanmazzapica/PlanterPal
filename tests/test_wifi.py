import ast
import asyncio
import importlib
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIFI_MODULE_PATH = PROJECT_ROOT / "web" / "wifi.py"


class FakeWLAN:
    """Small stateful stand-in for MicroPython's station WLAN."""

    def __init__(self, connect_outcomes=(), connected=False, isconnected_error=None):
        self._connect_outcomes = list(connect_outcomes)
        self._connected = connected
        self._isconnected_error = isconnected_error
        self._attempt_in_progress = False

        self.active_calls = []
        self.connect_calls = []
        self.disconnect_calls = 0
        self.ifconfig_calls = 0

    def active(self, value):
        self.active_calls.append(value)

    def connect(self, ssid, password):
        if self._attempt_in_progress:
            raise AssertionError("a second Wi-Fi attempt was started before cleanup")

        self._attempt_in_progress = True
        self.connect_calls.append((ssid, password))
        outcome = self._connect_outcomes.pop(0) if self._connect_outcomes else False

        if isinstance(outcome, BaseException):
            self._attempt_in_progress = False
            raise outcome

        if outcome:
            self._connected = True
            self._attempt_in_progress = False

    def disconnect(self):
        self.disconnect_calls += 1
        self._attempt_in_progress = False
        self._connected = False

    def isconnected(self):
        if self._isconnected_error is not None:
            raise self._isconnected_error
        return self._connected

    def ifconfig(self):
        self.ifconfig_calls += 1
        return ("192.0.2.2", "255.255.255.0", "192.0.2.1", "192.0.2.1")

    def lose_connection(self):
        self._attempt_in_progress = False
        self._connected = False


class WifiModuleHarness:
    """Imports web.wifi with host-safe MicroPython dependency fakes."""

    def __init__(self):
        self.created_wlans = []
        self.network = types.ModuleType("network")
        self.network.STA_IF = object()
        self.network.WLAN = self._create_wlan

        self.config = types.ModuleType("web.network_config")
        self.config.WIFI_CONNECT_TIMEOUT_S = 0.01
        self.config.WIFI_POLL_INTERVAL_S = 0
        self.config.WIFI_MONITOR_INTERVAL_S = 0
        self.config.WIFI_RECONNECT_BACKOFF_S = (0,)

        # Keep the import hermetic if compatibility code still mentions the
        # old configuration module during the migration.
        self.wifi_config = types.ModuleType("web.wifi_config")
        self.wifi_config.cfg = {"ssid": "global-ssid", "pw": "global-password"}

    def _create_wlan(self, interface):
        wlan = FakeWLAN()
        self.created_wlans.append((interface, wlan))
        return wlan

    def import_module(self):
        old_modules = {
            name: sys.modules.get(name)
            for name in (
                "network",
                "web.network_config",
                "web.wifi_config",
                "web.wifi",
            )
        }

        sys.modules["network"] = self.network
        sys.modules["web.network_config"] = self.config
        sys.modules["web.wifi_config"] = self.wifi_config
        sys.modules.pop("web.wifi", None)

        clock = [0]

        def ticks_ms():
            clock[0] += 5
            return clock[0]

        ticks_diff = lambda current, previous: current - previous
        try:
            with mock.patch.object(time, "ticks_ms", ticks_ms, create=True), mock.patch.object(
                time, "ticks_diff", ticks_diff, create=True
            ):
                module = importlib.import_module("web.wifi")
        finally:
            for name, previous in old_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

        # web.wifi imports the module rather than the functions. Give that
        # module a MicroPython-compatible clock without mutating host time for
        # the remainder of the test process.
        module.time = types.SimpleNamespace(ticks_ms=ticks_ms, ticks_diff=ticks_diff)
        module.print = lambda *args, **kwargs: None

        return module


class NetworkManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.harness = WifiModuleHarness()
        self.wifi = self.harness.import_module()

    async def _cancel(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_constructs_and_exclusively_owns_station_wlan(self):
        manager = self.wifi.NetworkManager("garden", "secret")

        self.assertEqual(len(self.harness.created_wlans), 1)
        interface, wlan = self.harness.created_wlans[0]
        self.assertIs(interface, self.harness.network.STA_IF)

        wlan._connect_outcomes.append(True)
        task = asyncio.create_task(manager.run())
        await asyncio.wait_for(manager.wait_until_connected(), timeout=0.25)

        self.assertEqual(wlan.active_calls, [True])
        self.assertEqual(wlan.connect_calls, [("garden", "secret")])
        await self._cancel(task)

    async def test_connected_event_tracks_link_success_and_loss(self):
        wlan = FakeWLAN(connect_outcomes=(True, False))
        manager = self.wifi.NetworkManager("garden", "secret", wlan=wlan)
        task = asyncio.create_task(manager.run())

        await asyncio.wait_for(manager.wait_until_connected(), timeout=0.25)
        self.assertTrue(manager.connected.is_set())

        wlan.lose_connection()
        for _ in range(100):
            if not manager.connected.is_set():
                break
            await asyncio.sleep(0)

        self.assertFalse(manager.connected.is_set())
        await self._cancel(task)

    async def test_timeout_cleans_up_before_retrying(self):
        wlan = FakeWLAN(connect_outcomes=(False, True))
        manager = self.wifi.NetworkManager("garden", "secret", wlan=wlan)
        task = asyncio.create_task(manager.run())

        await asyncio.wait_for(manager.wait_until_connected(), timeout=0.5)

        self.assertEqual(wlan.connect_calls, [("garden", "secret"), ("garden", "secret")])
        self.assertGreaterEqual(wlan.disconnect_calls, 1)
        await self._cancel(task)

    async def test_oserror_is_retried(self):
        wlan = FakeWLAN(connect_outcomes=(OSError("radio busy"), True))
        manager = self.wifi.NetworkManager("garden", "secret", wlan=wlan)
        task = asyncio.create_task(manager.run())

        await asyncio.wait_for(manager.wait_until_connected(), timeout=0.25)

        self.assertEqual(wlan.connect_calls, [("garden", "secret"), ("garden", "secret")])
        self.assertTrue(manager.connected.is_set())
        await self._cancel(task)

    async def test_cancellation_propagates_and_clears_connected(self):
        wlan = FakeWLAN(connect_outcomes=(True,))
        manager = self.wifi.NetworkManager("garden", "secret", wlan=wlan)
        task = asyncio.create_task(manager.run())
        await asyncio.wait_for(manager.wait_until_connected(), timeout=0.25)

        await self._cancel(task)

        self.assertFalse(manager.connected.is_set())

    async def test_unexpected_failure_is_visible_to_run_waiter_and_raise(self):
        failure = RuntimeError("broken WLAN driver")
        wlan = FakeWLAN(isconnected_error=failure)
        manager = self.wifi.NetworkManager("garden", "secret", wlan=wlan)
        waiter = asyncio.create_task(manager.wait_until_connected())
        task = asyncio.create_task(manager.run())

        await asyncio.wait_for(task, timeout=0.25)
        with self.assertRaisesRegex(RuntimeError, "broken WLAN driver"):
            await asyncio.wait_for(waiter, timeout=0.25)
        with self.assertRaisesRegex(RuntimeError, "broken WLAN driver"):
            manager.raise_if_failed()

        self.assertFalse(manager.connected.is_set())

    async def test_second_run_cannot_start_another_connection_attempt(self):
        self.wifi.cfg.WIFI_CONNECT_TIMEOUT_S = 1000
        wlan = FakeWLAN(connect_outcomes=(False,))
        manager = self.wifi.NetworkManager("garden", "secret", wlan=wlan)
        first = asyncio.create_task(manager.run())

        for _ in range(100):
            if wlan.connect_calls:
                break
            await asyncio.sleep(0)
        self.assertEqual(len(wlan.connect_calls), 1)

        second = asyncio.create_task(manager.run())
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(second, timeout=0.25)

        self.assertEqual(len(wlan.connect_calls), 1)
        await self._cancel(first)


class NetworkManagerStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(WIFI_MODULE_PATH.read_text(), filename=str(WIFI_MODULE_PATH))

    def test_wifi_module_has_no_led_ble_display_or_backend_coupling(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden = (
            "led",
            "lib.ble_provisioning",
            "display",
            "web.client",
            "web.wifi_config",
        )
        coupled = sorted(
            name for name in imported if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
        )
        self.assertEqual(coupled, [])

    def test_wlan_access_is_scoped_to_network_manager(self):
        wlan_methods = {"active", "connect", "disconnect", "ifconfig", "isconnected"}
        violations = []

        class MutationVisitor(ast.NodeVisitor):
            def __init__(self):
                self.class_name = None

            def visit_ClassDef(self, node):
                previous = self.class_name
                self.class_name = node.name
                self.generic_visit(node)
                self.class_name = previous

            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr in wlan_methods:
                    if self.class_name != "NetworkManager":
                        violations.append((node.lineno, node.func.attr))
                self.generic_visit(node)

        MutationVisitor().visit(self.tree)
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
