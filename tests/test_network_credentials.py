import ast
import asyncio
import importlib
import io
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from web.credentials import Credentials


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIFI_PATH = PROJECT_ROOT / "web" / "wifi.py"


class CandidateWLAN:
    """Adversarial station fake with independently controlled link and DHCP."""

    def __init__(self, outcomes=(), ip="192.0.2.25", connected=False):
        self.outcomes = list(outcomes)
        self.ip = ip
        self.connected = connected
        self.current_status = 0
        self.calls = []

    def active(self, value):
        self.calls.append(("active", value))

    def connect(self, ssid, password):
        self.calls.append(("connect", ssid, password))
        outcome = self.outcomes.pop(0) if self.outcomes else False
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, int) and not isinstance(outcome, bool):
            self.current_status = outcome
            self.connected = False
            return
        self.connected = bool(outcome)
        self.current_status = 3 if self.connected else 1

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.connected = False
        self.current_status = 0

    def isconnected(self):
        self.calls.append(("isconnected",))
        return self.connected

    def status(self):
        self.calls.append(("status",))
        return self.current_status

    def ifconfig(self):
        self.calls.append(("ifconfig",))
        return (self.ip, "255.255.255.0", "192.0.2.1", "192.0.2.1")

    def lose_connection(self):
        self.connected = False
        self.current_status = 0


class CandidateWifiHarness:
    def __init__(self):
        self.network = types.ModuleType("network")
        self.network.STA_IF = object()
        self.network.STAT_IDLE = 0
        self.network.STAT_CONNECTING = 1
        self.network.STAT_WRONG_PASSWORD = -3
        self.network.STAT_NO_AP_FOUND = -2
        self.network.STAT_CONNECT_FAIL = -1
        self.network.STAT_GOT_IP = 3
        self.network.WLAN = lambda _: CandidateWLAN()

        self.config = types.ModuleType("web.network_config")
        self.config.WIFI_CONNECT_TIMEOUT_S = 0.01
        self.config.WIFI_POLL_INTERVAL_S = 0
        self.config.WIFI_MONITOR_INTERVAL_S = 0
        self.config.WIFI_RECONNECT_BACKOFF_S = (0,)

    def import_module(self):
        old_modules = {
            name: sys.modules.get(name)
            for name in ("network", "web.network_config", "web.wifi")
        }
        sys.modules["network"] = self.network
        sys.modules["web.network_config"] = self.config
        sys.modules.pop("web.wifi", None)

        clock = [0]

        def ticks_ms():
            clock[0] += 5
            return clock[0]

        try:
            with mock.patch.object(time, "ticks_ms", ticks_ms, create=True), mock.patch.object(
                time,
                "ticks_diff",
                lambda current, previous: current - previous,
                create=True,
            ):
                module = importlib.import_module("web.wifi")
        finally:
            for name, previous in old_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

        module.time = types.SimpleNamespace(
            ticks_ms=ticks_ms,
            ticks_diff=lambda current, previous: current - previous,
        )
        return module


class NetworkCredentialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.harness = CandidateWifiHarness()
        self.wifi = self.harness.import_module()

    async def _wait_for_call(self, wlan, name):
        for _ in range(100):
            if any(call[0] == name for call in wlan.calls):
                return
            await asyncio.sleep(0)
        self.fail("WLAN call {!r} never occurred".format(name))

    async def _cancel(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_success_requires_link_and_nonzero_ip_then_adopts_candidate(self):
        wlan = CandidateWLAN(outcomes=(True, True))
        manager = self.wifi.NetworkManager(wlan=wlan)
        credentials = Credentials("new-garden", "new-secret")

        result = await manager.try_credentials(credentials)

        self.assertTrue(result.success)
        self.assertEqual(result.reason, "connected")
        call_names = [call[0] for call in wlan.calls]
        self.assertLess(call_names.index("connect"), call_names.index("ifconfig"))

        # Adoption is tested by behavior rather than by reaching into private
        # fields: after link loss, run() must reconnect with the candidate.
        wlan.lose_connection()
        task = asyncio.create_task(manager.run())
        await asyncio.wait_for(manager.wait_until_connected(), timeout=0.25)
        connect_calls = [call for call in wlan.calls if call[0] == "connect"]
        self.assertEqual(connect_calls[-1], ("connect", "new-garden", "new-secret"))
        await self._cancel(task)

    async def test_link_without_dhcp_is_failure_and_preserves_previous_credentials(self):
        wlan = CandidateWLAN(outcomes=(True, True), ip="0.0.0.0")
        manager = self.wifi.NetworkManager("known-good", "old-secret", wlan=wlan)

        result = await manager.try_credentials(Credentials("candidate", "bad-secret"))

        self.assertFalse(result.success)
        self.assertNotEqual(result.reason, "connected")
        self.assertIn(("disconnect",), wlan.calls)

        wlan.ip = "192.0.2.8"
        task = asyncio.create_task(manager.run())
        await asyncio.wait_for(manager.wait_until_connected(), timeout=0.25)
        connect_calls = [call for call in wlan.calls if call[0] == "connect"]
        self.assertEqual(connect_calls[-1], ("connect", "known-good", "old-secret"))
        await self._cancel(task)

    async def test_known_terminal_wlan_statuses_become_typed_results(self):
        cases = (
            (self.harness.network.STAT_WRONG_PASSWORD, "wrong_password"),
            (self.harness.network.STAT_NO_AP_FOUND, "no_ap"),
            (self.harness.network.STAT_CONNECT_FAIL, "connect_failed"),
        )

        for status, reason in cases:
            with self.subTest(status=status):
                wlan = CandidateWLAN(outcomes=(status,))
                manager = self.wifi.NetworkManager(wlan=wlan)
                result = await manager.try_credentials(Credentials("garden", "secret"))
                self.assertFalse(result.success)
                self.assertEqual(result.reason, reason)
                self.assertIn(("disconnect",), wlan.calls)

    async def test_candidate_timeout_is_bounded_and_cleans_up(self):
        wlan = CandidateWLAN(outcomes=(False,))
        manager = self.wifi.NetworkManager(wlan=wlan)

        result = await asyncio.wait_for(
            manager.try_credentials(Credentials("garden", "secret"), timeout_s=0.01),
            timeout=0.25,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "timeout")
        self.assertIn(("disconnect",), wlan.calls)

    async def test_candidate_attempts_are_serialized_without_second_wlan_connect(self):
        self.wifi.cfg.WIFI_CONNECT_TIMEOUT_S = 1000
        wlan = CandidateWLAN(outcomes=(False,))
        manager = self.wifi.NetworkManager(wlan=wlan)
        first = asyncio.create_task(
            manager.try_credentials(Credentials("first", "first-secret"))
        )
        await self._wait_for_call(wlan, "connect")

        with self.assertRaises(RuntimeError):
            await manager.try_credentials(Credentials("second", "second-secret"))

        self.assertEqual(
            [call for call in wlan.calls if call[0] == "connect"],
            [("connect", "first", "first-secret")],
        )
        self.assertTrue(manager.busy)
        self.assertFalse(manager.running)
        await self._cancel(first)
        self.assertFalse(manager.busy)
        self.assertFalse(manager.running)

    async def test_candidate_validation_is_rejected_while_reconnect_loop_owns_wlan(self):
        wlan = CandidateWLAN(outcomes=(True,))
        manager = self.wifi.NetworkManager("active", "active-secret", wlan=wlan)
        run_task = asyncio.create_task(manager.run())
        await asyncio.wait_for(manager.wait_until_connected(), timeout=0.25)

        self.assertTrue(manager.running)
        self.assertTrue(manager.busy)

        with self.assertRaises(RuntimeError):
            await manager.try_credentials(Credentials("candidate", "candidate-secret"))

        self.assertEqual(
            [call for call in wlan.calls if call[0] == "connect"],
            [("connect", "active", "active-secret")],
        )
        await self._cancel(run_task)
        self.assertFalse(manager.running)
        self.assertFalse(manager.busy)

    async def test_cancelled_candidate_propagates_and_disconnects_without_adoption(self):
        self.wifi.cfg.WIFI_CONNECT_TIMEOUT_S = 1000
        wlan = CandidateWLAN(outcomes=(False, True))
        manager = self.wifi.NetworkManager("known-good", "old-secret", wlan=wlan)
        attempt = asyncio.create_task(
            manager.try_credentials(Credentials("candidate", "candidate-secret"))
        )
        await self._wait_for_call(wlan, "connect")

        await self._cancel(attempt)
        self.assertIn(("disconnect",), wlan.calls)

        task = asyncio.create_task(manager.run())
        await asyncio.wait_for(manager.wait_until_connected(), timeout=0.25)
        self.assertEqual(
            [call for call in wlan.calls if call[0] == "connect"][-1],
            ("connect", "known-good", "old-secret"),
        )
        await self._cancel(task)

    async def test_forget_active_credentials_disconnects_and_prevents_running(self):
        wlan = CandidateWLAN(connected=True)
        manager = self.wifi.NetworkManager("garden", "secret", wlan=wlan)

        manager.forget_active_credentials()

        self.assertIn(("disconnect",), wlan.calls)
        with self.assertRaises(RuntimeError):
            await manager.run()

    async def test_results_are_immutable_and_never_contain_credentials(self):
        secret = "MUST-NOT-LEAK-candidate-password"
        wlan = CandidateWLAN(outcomes=(self.harness.network.STAT_WRONG_PASSWORD,))
        manager = self.wifi.NetworkManager(wlan=wlan)

        output = io.StringIO()
        with redirect_stdout(output):
            result = await manager.try_credentials(Credentials("private-ssid", secret))

        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(secret, str(result))
        for name, value in (("success", True), ("reason", secret)):
            with self.subTest(name=name):
                with self.assertRaises((AttributeError, TypeError)):
                    setattr(result, name, value)


class NetworkCredentialBoundaryTests(unittest.TestCase):
    def test_network_manager_has_no_persistence_or_ble_dependencies(self):
        tree = ast.parse(WIFI_PATH.read_text(), filename=str(WIFI_PATH))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden = (
            "esp32",
            "web.credentials",
            "lib.ble_provisioning",
            "aioble",
            "bluetooth",
            "led",
            "display",
        )
        coupled = sorted(
            name
            for name in imported
            if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
        )
        self.assertEqual(coupled, [])

    def test_only_network_manager_or_boot_reservation_constructs_wlan(self):
        violations = []
        reservation_path = PROJECT_ROOT / "app" / "provisioning_runtime.py"
        for path in PROJECT_ROOT.rglob("*.py"):
            if (
                "tests" in path.parts
                or ".venv" in path.parts
                or path == WIFI_PATH
                or path == reservation_path
            ):
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    alias.name == "network" for alias in node.names
                ):
                    violations.append((str(path.relative_to(PROJECT_ROOT)), node.lineno, "import"))
                elif isinstance(node, ast.ImportFrom) and node.module == "network":
                    violations.append((str(path.relative_to(PROJECT_ROOT)), node.lineno, "import"))
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "WLAN"
                ):
                    violations.append((str(path.relative_to(PROJECT_ROOT)), node.lineno, "WLAN"))

        self.assertEqual(
            violations,
            [],
            "only NetworkManager and its inactive boot reservation may construct WLAN",
        )

        reservation_tree = ast.parse(
            reservation_path.read_text(),
            filename=str(reservation_path),
        )
        mutating_methods = {"active", "connect", "disconnect", "config"}
        direct_mutations = [
            (node.lineno, node.func.attr)
            for node in ast.walk(reservation_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in mutating_methods
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "station"
        ]
        self.assertEqual(
            direct_mutations,
            [],
            "the reservation may allocate an inactive handle but may not mutate Wi-Fi state",
        )


if __name__ == "__main__":
    unittest.main()
