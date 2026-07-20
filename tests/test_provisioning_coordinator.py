import ast
import asyncio
import importlib
import inspect
import unittest
from pathlib import Path
from unittest import mock

from web.credentials import Credentials


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COORDINATOR_PATH = PROJECT_ROOT / "app" / "provisioning.py"
CREDENTIAL_STORE_PATH = PROJECT_ROOT / "web" / "credentials.py"


class FakeConnectionResult:
    def __init__(self, success, reason):
        self.success = success
        self.reason = reason


class FakeSubmittedCredentials:
    """BLE-shaped value deliberately distinct from the persistence value."""

    __slots__ = ("ssid", "password")

    def __init__(self, ssid, password):
        object.__setattr__(self, "ssid", ssid)
        object.__setattr__(self, "password", password)


class Attempt:
    def __init__(self, result=None, error=None, released=False):
        self.result = result
        self.error = error
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        if released:
            self.release.set()


class FakeNetworkManager:
    def __init__(self, attempts, order, forget_error=None, running=False):
        self.attempts = list(attempts)
        self.order = order
        self.forget_error = forget_error
        self.running = running
        self.busy = running
        self.try_calls = []
        self.forget_calls = 0

    async def try_credentials(self, credentials):
        if not self.attempts:
            raise AssertionError("unexpected credential validation attempt")
        attempt = self.attempts.pop(0)
        self.try_calls.append(credentials)
        self.order.append(("network-start", credentials.ssid))
        attempt.started.set()
        try:
            await attempt.release.wait()
        except asyncio.CancelledError:
            self.order.append(("network-cancelled", credentials.ssid))
            attempt.cancelled.set()
            raise
        if attempt.error is not None:
            raise attempt.error
        self.order.append(("network-result", attempt.result.reason))
        return attempt.result

    def forget_active_credentials(self):
        self.forget_calls += 1
        self.order.append(("network-forget",))
        if self.forget_error is not None:
            raise self.forget_error


class FakeCredentialStore:
    def __init__(self, order, save_errors=(), clear_error=None):
        self.order = order
        self.save_errors = list(save_errors)
        self.clear_error = clear_error
        self.saved = []
        self.clear_calls = 0

    def save(self, credentials):
        self.order.append(("store-save", credentials.ssid))
        self.saved.append(credentials)
        error = self.save_errors.pop(0) if self.save_errors else None
        if error is not None:
            raise error

    def clear(self):
        self.clear_calls += 1
        self.order.append(("store-clear",))
        if self.clear_error is not None:
            raise self.clear_error


class FakeRequest:
    def __init__(self, ssid="garden", password="secret", order=None, delivered=True):
        self.credentials = FakeSubmittedCredentials(ssid, password)
        self.ssid = ssid
        self.password = password
        self.order = order if order is not None else []
        self.cancelled = False
        self._cancelled_event = asyncio.Event()
        self.result = None
        self.result_called = asyncio.Event()
        self.response_waiting = asyncio.Event()
        self.response_release = asyncio.Event()
        self.response_delivered = delivered

    async def wait_cancelled(self):
        await self._cancelled_event.wait()
        return True

    def cancel(self, reason="client_disconnected"):
        if self.cancelled or self.result is not None:
            return False
        self.cancelled = True
        self.order.append(("request-cancel", reason))
        self._cancelled_event.set()
        return True

    def succeed(self):
        if self.cancelled or self.result is not None:
            return False
        self.result = ("success", None)
        self.order.append(("request-success",))
        self.result_called.set()
        return True

    def fail(self, reason):
        if self.cancelled or self.result is not None:
            return False
        self.result = ("error", reason)
        self.order.append(("request-fail", reason))
        self.result_called.set()
        return True

    async def wait_response_sent(self):
        self.order.append(("response-wait",))
        self.response_waiting.set()
        await self.response_release.wait()
        self.order.append(("response-sent", self.response_delivered))
        return self.response_delivered

    # Accept the coordinator's earlier provisional spelling while requiring
    # identical acknowledgment semantics.
    async def wait_response_handled(self):
        return await self.wait_response_sent()


class RecordingChannel:
    def __init__(self, order):
        self.order = order
        self._queue = asyncio.Queue()
        self.get_calls = 0

    async def put(self, value):
        await self._queue.put(value)

    async def get(self):
        self.get_calls += 1
        self.order.append(("channel-get", self.get_calls))
        return await self._queue.get()


async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def import_coordinator_module():
    return importlib.import_module("app.provisioning")


class ProvisioningCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = import_coordinator_module()
        self.order = []
        self.channel = RecordingChannel(self.order)

    def coordinator(self, attempts, store=None, network=None):
        network = network or FakeNetworkManager(attempts, self.order)
        store = store or FakeCredentialStore(self.order)
        coordinator = self.module.ProvisioningCoordinator(
            network,
            store,
            self.channel,
        )
        return coordinator, network, store

    async def _wait_until(self, predicate, message):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail(message)

    async def test_success_order_is_validate_commit_response_ack_then_provisioned(self):
        attempt = Attempt(FakeConnectionResult(True, "connected"))
        coordinator, network, store = self.coordinator([attempt])
        request = FakeRequest(order=self.order)
        task = asyncio.create_task(coordinator.run())
        await self.channel.put(request)
        await asyncio.wait_for(attempt.started.wait(), timeout=0.25)

        self.assertEqual(store.saved, [])
        self.assertIsNone(request.result)
        self.assertFalse(coordinator.provisioned.is_set())

        attempt.release.set()
        await asyncio.wait_for(request.response_waiting.wait(), timeout=0.25)

        self.assertEqual(store.saved, [Credentials(request.ssid, request.password)])
        self.assertIsInstance(store.saved[0], Credentials)
        self.assertEqual(request.result, ("success", None))
        self.assertFalse(
            coordinator.provisioned.is_set(),
            "running mode cannot begin before BLE handles its success response",
        )
        self.assertFalse(task.done())

        request.response_release.set()
        await asyncio.wait_for(task, timeout=0.25)

        self.assertTrue(coordinator.provisioned.is_set())
        significant = [
            item[0]
            for item in self.order
            if item[0]
            in {
                "network-start",
                "network-result",
                "store-save",
                "request-success",
                "response-wait",
                "response-sent",
            }
        ]
        self.assertEqual(
            significant,
            [
                "network-start",
                "network-result",
                "store-save",
                "request-success",
                "response-wait",
                "response-sent",
            ],
        )
        coordinator.raise_if_failed()

    async def test_second_task_allocation_failure_settles_candidate_without_orphan(self):
        attempt = Attempt(FakeConnectionResult(True, "connected"))
        coordinator, network, store = self.coordinator([attempt])
        request = FakeRequest(order=self.order)
        coordinator_task = asyncio.create_task(coordinator.run())
        await asyncio.sleep(0)

        original_create_task = asyncio.create_task
        created_tasks = []
        rejected_coroutines = []

        def fail_second_task(coroutine):
            if not created_tasks:
                task = original_create_task(coroutine)
                created_tasks.append(task)
                return task
            rejected_coroutines.append(coroutine)
            raise MemoryError("task allocation failed")

        with mock.patch.object(
            self.module.asyncio,
            "create_task",
            side_effect=fail_second_task,
        ):
            await self.channel.put(request)
            for _ in range(20):
                await asyncio.sleep(0)
                if coordinator.failure is not None:
                    break

        await asyncio.wait_for(coordinator_task, timeout=0.25)

        self.assertIsInstance(coordinator.failure, MemoryError)
        self.assertRegex(str(coordinator.failure), "task allocation failed")
        self.assertEqual(len(created_tasks), 1)
        self.assertTrue(created_tasks[0].done())
        self.assertTrue(created_tasks[0].cancelled())
        self.assertEqual(len(rejected_coroutines), 1)
        self.assertIsNone(rejected_coroutines[0].cr_frame)
        self.assertFalse(network.busy)
        self.assertEqual(store.saved, [])
        self.assertIsNone(request.result)

    async def test_failed_wifi_never_saves_and_waits_for_error_response_before_retry(self):
        failed = Attempt(FakeConnectionResult(False, "wrong_password"), released=True)
        succeeded = Attempt(FakeConnectionResult(True, "connected"), released=True)
        coordinator, network, store = self.coordinator([failed, succeeded])
        first = FakeRequest("first", "bad-secret", self.order)
        second = FakeRequest("second", "good-secret", self.order)
        task = asyncio.create_task(coordinator.run())
        await self.channel.put(first)
        await asyncio.wait_for(first.response_waiting.wait(), timeout=0.25)

        self.assertEqual(first.result, ("error", "wrong_password"))
        self.assertEqual(store.saved, [])
        await self.channel.put(second)
        await asyncio.sleep(0)
        self.assertEqual(
            len(network.try_calls),
            1,
            "next candidate cannot begin before prior BLE response is handled",
        )

        first.response_release.set()
        await asyncio.wait_for(succeeded.started.wait(), timeout=0.25)
        await asyncio.wait_for(second.response_waiting.wait(), timeout=0.25)
        second.response_release.set()
        await asyncio.wait_for(task, timeout=0.25)

        self.assertEqual(store.saved, [Credentials(second.ssid, second.password)])
        self.assertTrue(coordinator.provisioned.is_set())

    async def test_storage_failure_rolls_back_network_and_stays_provisioning(self):
        failed_commit = OSError("flash commit failed")
        attempts = [
            Attempt(FakeConnectionResult(True, "connected"), released=True),
            Attempt(FakeConnectionResult(True, "connected"), released=True),
        ]
        store = FakeCredentialStore(self.order, save_errors=(failed_commit, None))
        coordinator, network, _ = self.coordinator(attempts, store=store)
        first = FakeRequest("first", "first-secret", self.order)
        second = FakeRequest("second", "second-secret", self.order)
        task = asyncio.create_task(coordinator.run())
        await self.channel.put(first)
        await asyncio.wait_for(first.response_waiting.wait(), timeout=0.25)

        self.assertEqual(first.result, ("error", "storage_failed"))
        self.assertEqual(network.forget_calls, 1)
        self.assertFalse(coordinator.provisioned.is_set())
        self.assertNotIn(("request-success",), self.order)

        first.response_release.set()
        await self.channel.put(second)
        await asyncio.wait_for(second.response_waiting.wait(), timeout=0.25)
        second.response_release.set()
        await asyncio.wait_for(task, timeout=0.25)

        self.assertEqual(network.forget_calls, 1)
        self.assertEqual(
            store.saved,
            [
                Credentials(first.ssid, first.password),
                Credentials(second.ssid, second.password),
            ],
        )
        self.assertTrue(coordinator.provisioned.is_set())

    async def test_disconnect_cancels_candidate_and_never_persists_it(self):
        cancelled_attempt = Attempt(FakeConnectionResult(True, "connected"))
        success_attempt = Attempt(FakeConnectionResult(True, "connected"), released=True)
        coordinator, network, store = self.coordinator(
            [cancelled_attempt, success_attempt]
        )
        first = FakeRequest("gone", "gone-secret", self.order)
        second = FakeRequest("present", "present-secret", self.order)
        task = asyncio.create_task(coordinator.run())
        await self.channel.put(first)
        await asyncio.wait_for(cancelled_attempt.started.wait(), timeout=0.25)

        first.cancel()
        await asyncio.wait_for(cancelled_attempt.cancelled.wait(), timeout=0.25)

        self.assertEqual(store.saved, [])
        self.assertIsNone(first.result)
        self.assertFalse(coordinator.provisioned.is_set())

        await self.channel.put(second)
        await asyncio.wait_for(success_attempt.started.wait(), timeout=0.25)
        await asyncio.wait_for(second.response_waiting.wait(), timeout=0.25)
        second.response_release.set()
        await asyncio.wait_for(task, timeout=0.25)

        self.assertEqual(store.saved, [Credentials(second.ssid, second.password)])

    async def test_cancelled_request_wins_even_if_network_result_becomes_ready(self):
        attempt = Attempt(FakeConnectionResult(True, "connected"))
        coordinator, network, store = self.coordinator([attempt])
        request = FakeRequest(order=self.order)
        task = asyncio.create_task(coordinator.run())
        await self.channel.put(request)
        await asyncio.wait_for(attempt.started.wait(), timeout=0.25)

        request.cancel()
        attempt.release.set()
        await asyncio.wait_for(attempt.cancelled.wait(), timeout=0.25)
        await asyncio.sleep(0)

        self.assertEqual(store.saved, [])
        self.assertFalse(coordinator.provisioned.is_set())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_successful_commit_remains_durable_if_response_delivery_is_lost(self):
        attempt = Attempt(FakeConnectionResult(True, "connected"), released=True)
        coordinator, _, store = self.coordinator([attempt])
        request = FakeRequest(order=self.order, delivered=False)
        request.response_release.set()
        task = asyncio.create_task(coordinator.run())
        await self.channel.put(request)

        await asyncio.wait_for(task, timeout=0.25)

        self.assertEqual(store.saved, [Credentials(request.ssid, request.password)])
        self.assertEqual(request.result, ("success", None))
        self.assertTrue(coordinator.provisioned.is_set())

    async def test_unexpected_failure_is_visible_and_never_reports_success(self):
        failure = RuntimeError("unexpected storage corruption")
        attempt = Attempt(FakeConnectionResult(True, "connected"), released=True)
        store = FakeCredentialStore(self.order, save_errors=(failure,))
        coordinator, _, _ = self.coordinator([attempt], store=store)
        request = FakeRequest(order=self.order)
        task = asyncio.create_task(coordinator.run())
        await self.channel.put(request)

        await asyncio.wait_for(task, timeout=0.25)

        self.assertIs(coordinator.failure, failure)
        with self.assertRaisesRegex(RuntimeError, "unexpected storage corruption"):
            coordinator.raise_if_failed()
        self.assertFalse(coordinator.provisioned.is_set())
        self.assertNotEqual(request.result, ("success", None))

    async def test_clear_credentials_is_ordered_idle_only_and_failure_safe(self):
        coordinator, network, store = self.coordinator([])

        await maybe_await(coordinator.clear_credentials())

        self.assertEqual(
            [item[0] for item in self.order],
            ["store-clear", "network-forget"],
        )

        running_coordinator, _, _ = self.coordinator([])
        running = asyncio.create_task(running_coordinator.run())
        await self._wait_until(
            lambda: self.channel.get_calls >= 1,
            "coordinator did not start",
        )
        with self.assertRaises(RuntimeError):
            await maybe_await(running_coordinator.clear_credentials())
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running

        failure = OSError("clear commit failed")
        failed_store = FakeCredentialStore(self.order, clear_error=failure)
        failed_network = FakeNetworkManager([], self.order)
        failed = self.module.ProvisioningCoordinator(
            failed_network,
            failed_store,
            RecordingChannel(self.order),
        )
        with self.assertRaises(OSError) as raised:
            await maybe_await(failed.clear_credentials())
        self.assertIs(raised.exception, failure)
        self.assertEqual(failed_network.forget_calls, 0)

        busy_store = FakeCredentialStore(self.order)
        busy_network = FakeNetworkManager(
            [],
            self.order,
            forget_error=RuntimeError("NetworkManager is running"),
            running=True,
        )
        busy = self.module.ProvisioningCoordinator(
            busy_network,
            busy_store,
            RecordingChannel(self.order),
        )
        with self.assertRaises(RuntimeError):
            await maybe_await(busy.clear_credentials())
        self.assertEqual(
            busy_store.clear_calls,
            0,
            "recovery must reject a busy network owner before erasing flash",
        )
        self.assertEqual(busy_network.forget_calls, 0)


class ProvisioningCoordinatorBoundaryTests(unittest.TestCase):
    def test_coordinator_has_no_hardware_ble_led_display_or_reporter_coupling(self):
        tree = ast.parse(COORDINATOR_PATH.read_text(), filename=str(COORDINATOR_PATH))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden = (
            "network",
            "esp32",
            "aioble",
            "bluetooth",
            "lib.ble_provisioning",
            "led",
            "display",
            "web.reporter",
            "web.client",
        )
        coupled = sorted(
            name
            for name in imported
            if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
        )
        self.assertEqual(coupled, [])

    def test_only_coordinator_may_request_runtime_credential_persistence(self):
        violations = []
        for path in PROJECT_ROOT.rglob("*.py"):
            if (
                "tests" in path.parts
                or ".venv" in path.parts
                or path == CREDENTIAL_STORE_PATH
                or path == COORDINATOR_PATH
            ):
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "save"
                ):
                    violations.append((str(path.relative_to(PROJECT_ROOT)), node.lineno))

        self.assertEqual(
            violations,
            [],
            "runtime credential persistence must be requested only by ProvisioningCoordinator",
        )


if __name__ == "__main__":
    unittest.main()
