import ast
import asyncio
import importlib
import io
import json
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from app.provisioning import ProvisioningCoordinator
from lib.async_channel import SingleValueChannel
from web.credentials import Credentials


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BLE_PATH = PROJECT_ROOT / "lib" / "ble_provisioning.py"
CREDENTIAL_STORE_PATH = PROJECT_ROOT / "web" / "credentials.py"
README_PATH = PROJECT_ROOT / "README.md"


class FakeGattError(Exception):
    pass


class FakeUUID:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return isinstance(other, FakeUUID) and self.value == other.value

    def __hash__(self):
        return hash(self.value)

    def __repr__(self):
        return "UUID({!r})".format(self.value)


class FakeService:
    def __init__(self, uuid):
        self.uuid = uuid


class FakeCharacteristic:
    def __init__(self, service, uuid, operation_log, **flags):
        self.service = service
        self.uuid = uuid
        self.flags = flags
        self.operation_log = operation_log
        self.value = b""
        self.write_history = []
        self.notify_history = []
        self.indicate_history = []
        self.indicate_started = asyncio.Event()
        self.indicate_release = asyncio.Event()
        self.indicate_auto_ack = True
        self.indicate_error = None
        self.notify_error = None
        self.written_calls = 0
        self._writes = asyncio.Queue()

    async def written(self):
        self.written_calls += 1
        self.operation_log.append(("await-write", self.written_calls))
        return await self._writes.get()

    def inject_write(self, connection, payload):
        self.operation_log.append(("command-write", bytes(payload)))
        self._writes.put_nowait((connection, payload))

    def write(self, payload, *args, **kwargs):
        self.value = bytes(payload)
        self.write_history.append(self.value)
        self.operation_log.append(("status-write", self.value))

    def read(self):
        return self.value

    def notify(self, connection, payload=None):
        value = self.value if payload is None else bytes(payload)
        self.notify_history.append((connection, value))
        self.operation_log.append(("status-notify", value))
        if self.notify_error is not None:
            raise self.notify_error

    async def indicate(self, connection, payload=None, timeout_ms=1000):
        value = self.value if payload is None else bytes(payload)
        self.indicate_history.append((connection, value, timeout_ms))
        self.operation_log.append(("status-indicate", value))
        self.indicate_started.set()
        if not self.indicate_auto_ack:
            with connection.timeout(timeout_ms):
                await self.indicate_release.wait()
        if self.indicate_error is not None:
            raise self.indicate_error
        self.operation_log.append(("status-indicate-ack", value))


class FakeBLEController:
    def __init__(self, operation_log):
        self.operation_log = operation_log
        self.config_calls = []
        self._active = False

    def active(self, value=None):
        if value is None:
            return self._active
        self._active = bool(value)
        self.operation_log.append(("ble-active", self._active))

    def config(self, **kwargs):
        self.config_calls.append(kwargs)
        self.operation_log.append(("ble-config", kwargs))


class FakeConnection:
    def __init__(self):
        self._connected = True
        self._disconnected = asyncio.Event()
        self._timeout_task = None
        self._timeout_expired = False
        self.timeout_values = []
        self.active_timeout_ms = None
        self.disconnect_calls = 0

    def is_connected(self):
        return self._connected

    async def disconnected(self, timeout_ms=None):
        await self._disconnected.wait()

    async def disconnect(self, timeout_ms=None):
        self.disconnect_calls += 1
        self.drop()

    def drop(self):
        self._connected = False
        self._disconnected.set()
        if self._timeout_task is not None:
            self._timeout_task.cancel()

    def timeout(self, timeout_ms):
        return FakeConnectionTimeout(self, timeout_ms)

    def expire_timeout(self):
        if self._timeout_task is None or self.active_timeout_ms is None:
            raise AssertionError("no finite connection timeout is active")
        self._timeout_expired = True
        self._timeout_task.cancel()


class FakeDeviceDisconnectedError(Exception):
    pass


class FakeConnectionTimeout:
    """Mirror aioble's synchronous timeout/disconnect context contract."""

    def __init__(self, connection, timeout_ms=None):
        self.connection = connection
        self.timeout_ms = timeout_ms

    def __enter__(self):
        self.connection.timeout_values.append(self.timeout_ms)
        self.connection.active_timeout_ms = self.timeout_ms
        self.connection._timeout_expired = False
        self.connection._timeout_task = asyncio.current_task()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.connection._timeout_task = None
        self.connection.active_timeout_ms = None
        if exc_type is asyncio.CancelledError and self.connection._timeout_expired:
            self.connection._timeout_expired = False
            raise asyncio.TimeoutError()
        if exc_type is asyncio.CancelledError and not self.connection.is_connected():
            raise FakeDeviceDisconnectedError()
        return False


class FakeAioble(types.ModuleType):
    def __init__(self):
        super().__init__("aioble")
        self.operation_log = []
        self.characteristics = []
        self.registered_services = []
        self.advertise_calls = []
        self.stop_calls = 0
        self.stop_error = None
        self.controller = None
        self.DeviceDisconnectedError = FakeDeviceDisconnectedError
        self.GattError = FakeGattError
        self.registered = asyncio.Event()
        self.advertising = asyncio.Event()
        self._connections = asyncio.Queue()

    def Service(self, uuid):
        return FakeService(uuid)

    def Characteristic(self, service, uuid, **flags):
        characteristic = FakeCharacteristic(
            service,
            uuid,
            self.operation_log,
            **flags,
        )
        self.characteristics.append(characteristic)
        return characteristic

    def register_services(self, *services):
        self.registered_services.append(services)
        self.operation_log.append(("register",))
        self.registered.set()

    def config(self, **kwargs):
        if self.controller is None:
            raise AssertionError("fake aioble controller is not configured")
        if not self.controller.active():
            self.controller.active(True)
        return self.controller.config(**kwargs)

    async def advertise(self, interval, **kwargs):
        self.advertise_calls.append((interval, kwargs))
        self.operation_log.append(("advertise", kwargs))
        self.advertising.set()
        return await self._connections.get()

    def connect(self, connection):
        self._connections.put_nowait(connection)

    def stop(self):
        self.stop_calls += 1
        self.operation_log.append(("stop",))
        if self.stop_error is not None:
            raise self.stop_error

    @property
    def command_characteristic(self):
        return next(item for item in self.characteristics if item.flags.get("capture"))

    @property
    def status_characteristic(self):
        return next(item for item in self.characteristics if item.flags.get("notify"))


class BleModuleHarness:
    def __init__(self):
        self.aioble = FakeAioble()
        self.bluetooth = types.ModuleType("bluetooth")
        self.bluetooth.UUID = FakeUUID
        self.bluetooth_controller = FakeBLEController(self.aioble.operation_log)
        self.aioble.controller = self.bluetooth_controller
        self.bluetooth.BLE = lambda: self.bluetooth_controller
        self.micropython = types.ModuleType("micropython")
        self.micropython.const = lambda value: value

    def import_module(self):
        names = ("aioble", "bluetooth", "micropython", "lib.ble_provisioning")
        old_modules = {name: sys.modules.get(name) for name in names}
        sys.modules["aioble"] = self.aioble
        sys.modules["bluetooth"] = self.bluetooth
        sys.modules["micropython"] = self.micropython
        sys.modules.pop("lib.ble_provisioning", None)
        try:
            return importlib.import_module("lib.ble_provisioning")
        finally:
            for name, previous in old_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous


class RecordingSink:
    def __init__(self, operation_log=None, error=None):
        self.operation_log = operation_log if operation_log is not None else []
        self.error = error
        self.requests = []
        self.received = asyncio.Event()

    async def put(self, request):
        self.operation_log.append(("dispatch", request.ssid))
        if self.error is not None:
            raise self.error
        self.requests.append(request)
        self.received.set()


class IntegrationNetworkManager:
    def __init__(self, operation_log):
        self.operation_log = operation_log
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.credentials = []
        self.forget_calls = 0

    async def try_credentials(self, credentials):
        self.credentials.append(credentials)
        self.operation_log.append(("network-candidate", credentials.ssid))
        self.started.set()
        await self.release.wait()
        self.operation_log.append(("network-connected", credentials.ssid))
        return types.SimpleNamespace(success=True, reason="connected")

    def forget_active_credentials(self):
        self.forget_calls += 1
        self.operation_log.append(("network-forget",))


class IntegrationCredentialStore:
    def __init__(self, operation_log):
        self.operation_log = operation_log
        self.saved = []
        self.saved_event = asyncio.Event()

    def save(self, credentials):
        self.saved.append(credentials)
        self.operation_log.append(("store-save", credentials.ssid))
        self.saved_event.set()


def wifi_message(ssid="garden", password="secret"):
    return json.dumps(
        {
            "type": "wifi_credentials",
            "ssid": ssid,
            "password": password,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def decode_status(payload):
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict) or not isinstance(decoded.get("status"), str):
        raise AssertionError("BLE status must be a JSON object with a string status")
    return decoded


class BleProvisioningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.harness = BleModuleHarness()
        self.module = self.harness.import_module()

    async def _start(self, sink=None, max_payload_bytes=256):
        sink = sink if sink is not None else RecordingSink(self.harness.aioble.operation_log)
        provisioner = self.module.BleProvisioner(
            sink,
            aioble_module=self.harness.aioble,
            bluetooth_module=self.harness.bluetooth,
            max_payload_bytes=max_payload_bytes,
        )
        task = asyncio.create_task(provisioner.run())
        await asyncio.wait_for(self.harness.aioble.registered.wait(), timeout=0.25)
        await asyncio.wait_for(self.harness.aioble.advertising.wait(), timeout=0.25)
        connection = FakeConnection()
        self.harness.aioble.connect(connection)
        return provisioner, sink, connection, task

    async def _cancel(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def _wait_until(self, predicate, message):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail(message)

    def _statuses(self):
        return [
            decode_status(payload)
            for payload in self.harness.aioble.status_characteristic.write_history
        ]

    async def _wait_for_status(self, status, count=1):
        await self._wait_until(
            lambda: sum(item["status"] == status for item in self._statuses()) >= count,
            "BLE status {!r} was never emitted".format(status),
        )

    def _notified_statuses(self):
        return [
            decode_status(payload)
            for _, payload in self.harness.aioble.status_characteristic.notify_history
        ]

    def _indicated_statuses(self):
        return [
            decode_status(payload)
            for _, payload, _ in self.harness.aioble.status_characteristic.indicate_history
        ]

    async def test_mtu_and_gatt_contract_are_configured_before_advertising(self):
        _, _, _, task = await self._start()
        status = self.harness.aioble.status_characteristic
        command = self.harness.aioble.command_characteristic
        operations = [item[0] for item in self.harness.aioble.operation_log]

        self.assertTrue(self.harness.bluetooth_controller.active())
        self.assertEqual(
            self.harness.bluetooth_controller.config_calls,
            [{"mtu": self.module.MAX_PAYLOAD_BYTES + self.module.ATT_WRITE_OVERHEAD_BYTES}],
        )
        self.assertLess(operations.index("ble-active"), operations.index("ble-config"))
        self.assertLess(operations.index("ble-config"), operations.index("register"))
        self.assertLess(operations.index("register"), operations.index("advertise"))
        self.assertTrue(command.flags.get("write"))
        self.assertTrue(command.flags.get("capture"))
        self.assertEqual(
            len(command.flags.get("initial")),
            self.module.MAX_PAYLOAD_BYTES,
        )
        self.assertTrue(status.flags.get("read"))
        self.assertTrue(status.flags.get("notify"))
        self.assertTrue(status.flags.get("indicate"))
        await self._cancel(task)

    async def test_valid_request_is_immutable_redacted_and_waits_for_result(self):
        password = "MUST-NOT-LEAK-BLE-password"
        _, sink, connection, task = await self._start()
        command = self.harness.aioble.command_characteristic

        output = io.StringIO()
        with redirect_stdout(output):
            command.inject_write(connection, wifi_message("private-garden", password))
            await asyncio.wait_for(sink.received.wait(), timeout=0.25)
            await self._wait_for_status("testing")

            request = sink.requests[0]
            self.assertEqual(request.ssid, "private-garden")
            self.assertEqual(request.password, password)
            self.assertNotIn(password, repr(request))
            self.assertNotIn(password, str(request))
            self.assertNotIn(password, repr(request.credentials))
            self.assertNotIn(password, str(request.credentials))
            for name, replacement in (("ssid", "other"), ("password", "other")):
                with self.subTest(name=name):
                    with self.assertRaises((AttributeError, TypeError)):
                        setattr(request, name, replacement)
                    with self.assertRaises((AttributeError, TypeError)):
                        setattr(request.credentials, name, replacement)

            # BLE must not claim success until its owner has completed network
            # validation and persistence and explicitly resolves the request.
            self.assertNotIn("success", [item["status"] for item in self._statuses()])
            request.succeed()
            self.assertTrue(
                await asyncio.wait_for(request.wait_response_sent(), timeout=0.25)
            )
            await self._wait_for_status("success")

        self.assertNotIn(password, output.getvalue())
        self.assertTrue(
            all(
                password.encode("utf-8") not in payload
                for payload in self.harness.aioble.status_characteristic.write_history
            )
        )
        await self._cancel(task)

    async def test_success_waits_for_att_indication_ack_before_response_finishes(self):
        _, sink, connection, task = await self._start()
        status = self.harness.aioble.status_characteristic
        status.indicate_auto_ack = False
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message(),
        )
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        request = sink.requests[0]

        request.succeed()
        await asyncio.wait_for(status.indicate_started.wait(), timeout=0.25)
        response = asyncio.create_task(request.wait_response_sent())
        await asyncio.sleep(0)

        self.assertFalse(response.done())
        self.assertEqual(decode_status(status.read()), {"status": "success"})
        self.assertNotIn(
            "success",
            [item["status"] for item in self._notified_statuses()],
            "terminal success must use an acknowledged indication, not notify",
        )
        indicated = status.indicate_history[-1][1]
        self.assertLessEqual(len(indicated), 20)
        self.assertEqual(decode_status(indicated), {"status": "success"})

        status.indicate_release.set()
        self.assertTrue(await asyncio.wait_for(response, timeout=0.25))
        await self._cancel(task)

    async def test_known_progress_notification_failure_is_best_effort(self):
        provisioner, sink, connection, task = await self._start()
        status = self.harness.aioble.status_characteristic
        await self._wait_until(
            lambda: any(
                item["status"] == "ready" for item in self._notified_statuses()
            ),
            "initial ready notification was not attempted",
        )
        status.notify_error = FakeGattError("testing notify not subscribed")

        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message(),
        )
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)

        self.assertEqual(decode_status(status.read()), {"status": "testing"})
        self.assertFalse(task.done())
        self.assertIsNone(provisioner.failure)

        status.notify_error = None
        sink.requests[0].fail("wrong_password")
        self.assertTrue(
            await asyncio.wait_for(
                sink.requests[0].wait_response_sent(),
                timeout=0.25,
            )
        )
        self.assertFalse(task.done())
        self.assertIsNone(provisioner.failure)
        await self._cancel(task)

    async def test_error_keeps_full_reason_readable_but_indicates_compact_token(self):
        _, sink, connection, task = await self._start()
        status = self.harness.aioble.status_characteristic
        command = self.harness.aioble.command_characteristic
        status.indicate_auto_ack = False
        command.inject_write(connection, wifi_message("first", "bad-secret"))
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        first = sink.requests[0]

        first.fail("wrong_password")
        await asyncio.wait_for(status.indicate_started.wait(), timeout=0.25)
        command.inject_write(connection, wifi_message("second", "good-secret"))
        await asyncio.sleep(0)

        self.assertEqual(decode_status(status.read()), {
            "status": "error",
            "reason": "wrong_password",
        })
        indicated = status.indicate_history[-1][1]
        self.assertLessEqual(
            len(indicated),
            20,
            "terminal indication must fit the default ATT payload",
        )
        self.assertEqual(decode_status(indicated), {"status": "error"})
        self.assertNotIn(
            "error",
            [item["status"] for item in self._notified_statuses()],
        )
        self.assertEqual(command.written_calls, 1)
        self.assertEqual([request.ssid for request in sink.requests], ["first"])

        status.indicate_release.set()
        self.assertTrue(
            await asyncio.wait_for(first.wait_response_sent(), timeout=0.25)
        )
        await self._wait_until(
            lambda: len(sink.requests) == 2,
            "next credential command was not accepted after error ACK",
        )
        sink.requests[1].fail("no_ap")
        self.assertTrue(
            await asyncio.wait_for(
                sink.requests[1].wait_response_sent(),
                timeout=0.25,
            )
        )
        await self._cancel(task)

    async def test_invalid_command_indication_blocks_next_write_until_ack(self):
        _, sink, connection, task = await self._start()
        status = self.harness.aioble.status_characteristic
        command = self.harness.aioble.command_characteristic
        status.indicate_auto_ack = False

        command.inject_write(connection, b"{")
        await asyncio.wait_for(status.indicate_started.wait(), timeout=0.25)
        command.inject_write(connection, wifi_message("valid", "valid-secret"))
        await asyncio.sleep(0)

        self.assertEqual(
            decode_status(status.read()),
            {"status": "invalid", "reason": "invalid_json"},
        )
        indicated = status.indicate_history[-1][1]
        self.assertLessEqual(len(indicated), 20)
        self.assertEqual(decode_status(indicated), {"status": "invalid"})
        self.assertEqual(command.written_calls, 1)
        self.assertEqual(sink.requests, [])

        status.indicate_release.set()
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        sink.requests[0].fail("connect_failed")
        self.assertTrue(
            await asyncio.wait_for(
                sink.requests[0].wait_response_sent(),
                timeout=0.25,
            )
        )
        await self._cancel(task)

    async def test_utf8_byte_boundaries_and_open_network_are_accepted(self):
        _, sink, connection, task = await self._start()
        command = self.harness.aioble.command_characteristic
        command.inject_write(
            connection,
            wifi_message("é" * 16, "🔒" * 16),
        )
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        self.assertEqual(len(sink.requests[0].ssid.encode("utf-8")), 32)
        self.assertEqual(len(sink.requests[0].password.encode("utf-8")), 64)
        sink.requests[0].fail("wrong_password")
        await self._wait_for_status("error")

        sink.received.clear()
        command.inject_write(connection, wifi_message("open-network", ""))
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        self.assertEqual(sink.requests[1].password, "")
        sink.requests[1].fail("connect_failed")
        await self._wait_for_status("error", count=2)
        await self._cancel(task)

    async def test_malformed_messages_return_errors_and_same_connection_recovers(self):
        _, sink, connection, task = await self._start(max_payload_bytes=128)
        command = self.harness.aioble.command_characteristic
        malformed = (
            b"\xff\xfe",
            b"{",
            b"[]",
            b'{"type":"unsupported","ssid":"garden","password":"secret"}',
            b'{"type":"wifi_credentials","password":"secret"}',
            b'{"type":"wifi_credentials","ssid":7,"password":"secret"}',
            b"x" * 129,
            wifi_message("garden", "p" * 65),
        )

        for index, payload in enumerate(malformed, start=1):
            command.inject_write(connection, payload)
            await self._wait_until(
                lambda: len(
                    [
                        item
                        for item in self._statuses()
                        if item["status"] in ("error", "invalid")
                    ]
                )
                >= index,
                "malformed request {} did not receive an error".format(index),
            )

        self.assertEqual(sink.requests, [])
        self.assertFalse(task.done(), "validation errors must not kill provisioning")

        command.inject_write(connection, wifi_message("valid", "valid-secret"))
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        sink.requests[0].fail("wrong_password")
        await self._wait_for_status("error")
        self.assertFalse(task.done(), "a rejected candidate must keep provisioning available")
        await self._cancel(task)

    async def test_only_one_request_is_read_and_dispatched_until_first_completes(self):
        _, sink, connection, task = await self._start()
        command = self.harness.aioble.command_characteristic
        command.inject_write(connection, wifi_message("first", "first-secret"))
        command.inject_write(connection, wifi_message("second", "second-secret"))

        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual([request.ssid for request in sink.requests], ["first"])
        self.assertEqual(
            command.written_calls,
            1,
            "BLE must not consume a second write while the first result is pending",
        )

        sink.received.clear()
        sink.requests[0].succeed()
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        self.assertEqual([request.ssid for request in sink.requests], ["first", "second"])
        sink.requests[1].fail("no_ap")
        await self._wait_for_status("error")
        await self._cancel(task)

    async def test_request_resolution_is_one_shot(self):
        _, sink, connection, task = await self._start()
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message(),
        )
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        request = sink.requests[0]

        request.succeed()
        with self.assertRaises(RuntimeError):
            request.succeed()
        with self.assertRaises(RuntimeError):
            request.fail("wrong_password")

        await self._wait_for_status("success")
        await self._cancel(task)

    async def test_disconnect_cancels_pending_request_and_resumes_advertising(self):
        _, sink, connection, task = await self._start()
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message(),
        )
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        request = sink.requests[0]

        connection.drop()

        cancellation = getattr(request, "wait_cancelled", None)
        if callable(cancellation):
            await asyncio.wait_for(cancellation(), timeout=0.25)
        else:
            cancelled = getattr(request, "cancelled", None)
            self.assertIsNotNone(
                cancelled,
                "request must expose disconnect cancellation to its coordinator",
            )
            if callable(getattr(cancelled, "wait", None)):
                await asyncio.wait_for(cancelled.wait(), timeout=0.25)
            else:
                self.assertTrue(cancelled)

        await self._wait_until(
            lambda: len(self.harness.aioble.advertise_calls) >= 2,
            "provisioner did not resume advertising after disconnect",
        )
        self.assertFalse(task.done())
        await self._cancel(task)

    async def test_disconnect_after_success_resolution_completes_response_ack_false(self):
        _, sink, connection, task = await self._start()
        status = self.harness.aioble.status_characteristic
        status.indicate_auto_ack = False
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message(),
        )
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        request = sink.requests[0]

        request.succeed()
        await asyncio.wait_for(status.indicate_started.wait(), timeout=0.25)
        connection.drop()

        delivered = await asyncio.wait_for(
            request.wait_response_sent(),
            timeout=0.25,
        )
        self.assertFalse(delivered)
        await self._wait_until(
            lambda: len(self.harness.aioble.advertise_calls) >= 2,
            "provisioner did not recover after losing the final response",
        )
        self.assertFalse(task.done())
        await self._cancel(task)

    async def test_known_gatt_delivery_failure_is_false_and_service_recovers(self):
        provisioner, sink, connection, task = await self._start()
        status = self.harness.aioble.status_characteristic
        status.indicate_error = FakeGattError("central rejected indication")
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message("first", "first-secret"),
        )
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        first = sink.requests[0]

        first.fail("wrong_password")
        self.assertFalse(
            await asyncio.wait_for(first.wait_response_sent(), timeout=0.25)
        )
        self.assertFalse(task.done())
        self.assertIsNone(provisioner.failure)

        status.indicate_error = None
        sink.received.clear()
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message("second", "second-secret"),
        )
        await self._wait_until(
            lambda: len(sink.requests) == 2,
            "service did not recover after a known indication failure",
        )
        sink.requests[1].fail("no_ap")
        self.assertTrue(
            await asyncio.wait_for(
                sink.requests[1].wait_response_sent(),
                timeout=0.25,
            )
        )
        await self._cancel(task)

    async def test_indication_timeout_is_false_and_does_not_kill_provisioning(self):
        provisioner, sink, connection, task = await self._start()
        status = self.harness.aioble.status_characteristic
        status.indicate_auto_ack = False
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message("first", "first-secret"),
        )
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        first = sink.requests[0]

        first.fail("timeout")
        await asyncio.wait_for(status.indicate_started.wait(), timeout=0.25)
        self.assertEqual(
            connection.active_timeout_ms,
            self.module.RESULT_INDICATION_TIMEOUT_MS,
        )
        connection.expire_timeout()

        self.assertFalse(
            await asyncio.wait_for(first.wait_response_sent(), timeout=0.25)
        )
        self.assertFalse(task.done())
        self.assertIsNone(provisioner.failure)

        status.indicate_auto_ack = True
        sink.received.clear()
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message("second", "second-secret"),
        )
        await self._wait_until(
            lambda: len(sink.requests) == 2,
            "service did not accept another command after indication timeout",
        )
        sink.requests[1].fail("connect_failed")
        self.assertTrue(
            await asyncio.wait_for(
                sink.requests[1].wait_response_sent(),
                timeout=0.25,
            )
        )
        await self._cancel(task)

    async def test_cancellation_during_indication_finishes_response_false(self):
        _, sink, connection, task = await self._start()
        status = self.harness.aioble.status_characteristic
        status.indicate_auto_ack = False
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message(),
        )
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)
        request = sink.requests[0]
        request.succeed()
        await asyncio.wait_for(status.indicate_started.wait(), timeout=0.25)

        await self._cancel(task)

        self.assertFalse(
            await asyncio.wait_for(request.wait_response_sent(), timeout=0.25)
        )
        self.assertGreaterEqual(connection.disconnect_calls, 1)

    async def test_idle_connection_times_out_disconnects_and_readvertises(self):
        provisioner, _, connection, task = await self._start()
        await self._wait_until(
            lambda: connection.active_timeout_ms is not None,
            "connected client never entered a finite command idle timeout",
        )

        self.assertEqual(
            connection.active_timeout_ms,
            self.module.CONNECTION_IDLE_TIMEOUT_MS,
        )
        self.assertEqual(self.module.CONNECTION_IDLE_TIMEOUT_MS, 120_000)
        connection.expire_timeout()

        await self._wait_until(
            lambda: len(self.harness.aioble.advertise_calls) >= 2,
            "idle client did not release BLE and resume advertising",
        )
        self.assertGreaterEqual(connection.disconnect_calls, 1)
        self.assertFalse(task.done())
        self.assertIsNone(provisioner.failure)
        await self._cancel(task)

    async def test_unexpected_sink_failure_surfaces_after_cleanup(self):
        failure = RuntimeError("request pipeline broke")
        sink = RecordingSink(self.harness.aioble.operation_log, error=failure)
        _, _, connection, task = await self._start(sink=sink)
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message(),
        )

        with self.assertRaisesRegex(RuntimeError, "request pipeline broke"):
            await asyncio.wait_for(task, timeout=0.25)

        self.assertGreaterEqual(self.harness.aioble.stop_calls, 1)
        self.assertGreaterEqual(connection.disconnect_calls, 1)

    async def test_cancellation_propagates_and_releases_ble_resources(self):
        _, sink, connection, task = await self._start()
        self.harness.aioble.command_characteristic.inject_write(
            connection,
            wifi_message(),
        )
        await asyncio.wait_for(sink.received.wait(), timeout=0.25)

        await self._cancel(task)

        self.assertGreaterEqual(self.harness.aioble.stop_calls, 1)
        self.assertGreaterEqual(connection.disconnect_calls, 1)

    async def test_cancellation_while_advertising_propagates_and_stops_controller(self):
        sink = RecordingSink(self.harness.aioble.operation_log)
        provisioner = self.module.BleProvisioner(
            sink,
            aioble_module=self.harness.aioble,
            bluetooth_module=self.harness.bluetooth,
        )
        task = asyncio.create_task(provisioner.run())
        await asyncio.wait_for(self.harness.aioble.advertising.wait(), timeout=0.25)

        await self._cancel(task)

        self.assertGreaterEqual(self.harness.aioble.stop_calls, 1)
        self.assertFalse(provisioner.running)

    async def test_cancellation_disconnects_connection_returned_by_advertise_race(self):
        connection = FakeConnection()

        async def advertise_then_connect(interval, **kwargs):
            self.harness.aioble.advertising.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return connection

        self.harness.aioble.advertise = advertise_then_connect
        provisioner = self.module.BleProvisioner(
            RecordingSink(),
            aioble_module=self.harness.aioble,
            bluetooth_module=self.harness.bluetooth,
        )
        task = asyncio.create_task(provisioner.run())
        await asyncio.wait_for(self.harness.aioble.advertising.wait(), timeout=0.25)

        await self._cancel(task)

        self.assertEqual(connection.disconnect_calls, 1)
        self.assertFalse(connection.is_connected())
        self.assertFalse(provisioner.running)

    async def test_stop_failure_still_clears_all_provisioner_state(self):
        failure = RuntimeError("controller stop failed")
        self.harness.aioble.stop_error = failure
        provisioner = self.module.BleProvisioner(
            RecordingSink(),
            aioble_module=self.harness.aioble,
            bluetooth_module=self.harness.bluetooth,
        )
        task = asyncio.create_task(provisioner.run())
        await asyncio.wait_for(self.harness.aioble.advertising.wait(), timeout=0.25)

        task.cancel()
        with self.assertRaisesRegex(RuntimeError, "controller stop failed"):
            await task

        self.assertIs(provisioner.failure, failure)
        self.assertFalse(provisioner.running)
        self.assertIsNone(provisioner._service_uuid)
        self.assertIsNone(provisioner._service)
        self.assertIsNone(provisioner.command_characteristic)
        self.assertIsNone(provisioner.status_characteristic)
        self.assertIsNone(provisioner.current_request)
        self.assertIsNone(provisioner._connection)


class BleProvisioningIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_transport_coordinator_and_channel_obey_commit_ack_order(self):
        harness = BleModuleHarness()
        ble_module = harness.import_module()
        channel = SingleValueChannel()
        network = IntegrationNetworkManager(harness.aioble.operation_log)
        store = IntegrationCredentialStore(harness.aioble.operation_log)
        provisioner = ble_module.BleProvisioner(
            channel,
            aioble_module=harness.aioble,
            bluetooth_module=harness.bluetooth,
        )
        coordinator = ProvisioningCoordinator(network, store, channel)
        ble_task = asyncio.create_task(provisioner.run())
        coordinator_task = asyncio.create_task(coordinator.run())

        try:
            await asyncio.wait_for(harness.aioble.registered.wait(), timeout=0.25)
            await asyncio.wait_for(harness.aioble.advertising.wait(), timeout=0.25)
            connection = FakeConnection()
            harness.aioble.connect(connection)
            status = harness.aioble.status_characteristic
            status.indicate_auto_ack = False

            harness.aioble.command_characteristic.inject_write(
                connection,
                wifi_message("integrated", "integrated-secret"),
            )
            await asyncio.wait_for(network.started.wait(), timeout=0.25)

            self.assertEqual(store.saved, [])
            self.assertFalse(coordinator.provisioned.is_set())
            self.assertFalse(coordinator_task.done())

            network.release.set()
            await asyncio.wait_for(store.saved_event.wait(), timeout=0.25)
            await asyncio.wait_for(status.indicate_started.wait(), timeout=0.25)

            self.assertEqual(
                store.saved,
                [Credentials("integrated", "integrated-secret")],
            )
            self.assertFalse(
                coordinator.provisioned.is_set(),
                "running mode cannot begin before the central ACKs success",
            )
            self.assertFalse(coordinator_task.done())
            self.assertNotIn(
                "status-indicate-ack",
                [item[0] for item in harness.aioble.operation_log],
            )

            status.indicate_release.set()
            await asyncio.wait_for(coordinator_task, timeout=0.25)

            self.assertTrue(coordinator.provisioned.is_set())
            self.assertIsNone(coordinator.failure)
            self.assertEqual(network.forget_calls, 0)
            operations = [item[0] for item in harness.aioble.operation_log]
            self.assertLess(operations.index("command-write"), operations.index("network-candidate"))
            self.assertLess(operations.index("network-candidate"), operations.index("store-save"))
            self.assertLess(operations.index("store-save"), operations.index("status-indicate"))
            self.assertLess(operations.index("status-indicate"), operations.index("status-indicate-ack"))
        finally:
            network.release.set()
            if not coordinator_task.done():
                coordinator_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await coordinator_task
            if not ble_task.done():
                ble_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await ble_task


class BleProvisioningBoundaryTests(unittest.TestCase):
    def test_ble_has_no_network_persistence_led_display_or_device_mode_coupling(self):
        tree = ast.parse(BLE_PATH.read_text(), filename=str(BLE_PATH))
        imported = set()
        referenced_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Name):
                referenced_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced_names.add(node.attr)

        forbidden_imports = (
            "network",
            "esp32",
            "web.credentials",
            "web.wifi",
            "led",
            "display",
            "main",
        )
        coupled = sorted(
            name
            for name in imported
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in forbidden_imports
            )
        )
        self.assertEqual(coupled, [])
        self.assertTrue(
            {"NVS", "CredentialStore", "WLAN"}.isdisjoint(referenced_names),
            "BLE provisioning may submit credentials but cannot persist or connect them",
        )

    def test_ble_has_no_print_calls_that_can_leak_payloads_or_passwords(self):
        tree = ast.parse(BLE_PATH.read_text(), filename=str(BLE_PATH))
        print_calls = [
            (node.lineno, ast.unparse(node))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        self.assertEqual(print_calls, [])

    def test_gatt_registration_is_lifecycle_owned_not_an_import_side_effect(self):
        tree = ast.parse(BLE_PATH.read_text(), filename=str(BLE_PATH))
        side_effects = []
        for node in tree.body:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            function = node.value.func
            if isinstance(function, ast.Attribute) and function.attr in {
                "register_services",
                "advertise",
                "stop",
            }:
                side_effects.append((node.lineno, function.attr))
        self.assertEqual(side_effects, [])

    def test_credential_store_is_only_production_nvs_record_owner(self):
        persistence_methods = {"get_blob", "set_blob", "erase_key", "commit"}
        violations = []
        for path in PROJECT_ROOT.rglob("*.py"):
            if (
                "tests" in path.parts
                or ".venv" in path.parts
                or path == CREDENTIAL_STORE_PATH
            ):
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in persistence_methods
                ):
                    violations.append(
                        (str(path.relative_to(PROJECT_ROOT)), node.lineno, node.func.attr)
                    )

        self.assertEqual(
            violations,
            [],
            "only CredentialStore may read or mutate the NVS credential record",
        )

    def test_protocol_docs_require_single_complete_utf8_write_and_acknowledgments(self):
        readme = README_PATH.read_text().lower()

        self.assertIn("central still initiates the exchange", readme)
        self.assertIn("encoded command length plus 3 bytes", readme)
        self.assertIn("write-with-response", readme)
        self.assertIn("one complete utf-8 json value", readme)
        self.assertIn("json payload must be at most 256 bytes", readme)
        self.assertIn("utf-8 directly", readme)
        self.assertIn("indications", readme)
        self.assertIn("att\nacknowledgment", readme)

    def test_maximum_utf8_credentials_fit_when_client_does_not_ascii_escape(self):
        compact = wifi_message("é" * 16, "🔒" * 16)
        ascii_escaped = json.dumps(
            {
                "type": "wifi_credentials",
                "ssid": "é" * 16,
                "password": "🔒" * 16,
            },
            ensure_ascii=True,
        ).encode("utf-8")

        self.assertLessEqual(len(compact), 256)
        self.assertGreater(len(ascii_escaped), 256)


if __name__ == "__main__":
    unittest.main()
