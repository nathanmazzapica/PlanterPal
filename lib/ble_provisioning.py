import asyncio
import json

import aioble
import bluetooth

from lib.ble_bootstrap import (
    ATT_WRITE_OVERHEAD_BYTES,
    DEFAULT_MAX_PAYLOAD_BYTES,
    MAX_ATT_MTU,
    prepare_ble_controller,
)


# Keep these identifiers stable: provisioning clients discover the service and
# command characteristic by UUID rather than by their position in the service.
SERVICE_UUID = "2bd127f3-ea4c-48f2-8234-32bf0660aecb"
COMMAND_CHAR_UUID = "f4320080-4ba2-4307-918a-b49e9a1dbff5"
STATUS_CHAR_UUID = "7d26a2f2-f4df-4dc3-8c49-078ca1c9b1ec"

DEVICE_NAME = "PlanterPal"
ADV_INTERVAL = 250_000
MAX_PAYLOAD_BYTES = DEFAULT_MAX_PAYLOAD_BYTES
DEFAULT_ATT_PAYLOAD_BYTES = 20
RESULT_INDICATION_TIMEOUT_MS = 1_000
CONNECTION_IDLE_TIMEOUT_MS = 120_000


class SubmittedCredentials:
    """Immutable credential input owned by the BLE transport boundary."""

    MAX_SSID_BYTES = 32
    MAX_PASSWORD_BYTES = 64

    __slots__ = ("_ssid", "_password")

    def __init__(self, ssid, password):
        if not isinstance(ssid, str) or not ssid:
            raise ValueError("ssid must be a non-empty string")
        if not isinstance(password, str):
            raise TypeError("password must be a string")
        if len(ssid.encode("utf-8")) > self.MAX_SSID_BYTES:
            raise ValueError("ssid exceeds 32 UTF-8 bytes")
        if len(password.encode("utf-8")) > self.MAX_PASSWORD_BYTES:
            raise ValueError("password exceeds 64 UTF-8 bytes")

        self._ssid = ssid
        self._password = password

    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise AttributeError("SubmittedCredentials is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise AttributeError("SubmittedCredentials is immutable")

    @property
    def ssid(self):
        return self._ssid

    @property
    def password(self):
        return self._password

    def __repr__(self):
        return "SubmittedCredentials(ssid={!r}, password=<redacted>)".format(
            self.ssid
        )

    __str__ = __repr__


class ProvisioningResult:
    """Immutable result supplied by the provisioning coordinator."""

    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"

    __slots__ = ("_status", "_reason")

    def __init__(self, status, reason=None):
        if status not in (self.SUCCESS, self.ERROR, self.CANCELLED):
            raise ValueError("unknown provisioning result status")
        if status == self.SUCCESS and reason is not None:
            raise ValueError("a successful result must not have a reason")
        if status != self.SUCCESS and not isinstance(reason, str):
            raise TypeError("an unsuccessful result requires a reason")

        self._status = status
        self._reason = reason

    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise AttributeError("ProvisioningResult is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise AttributeError("ProvisioningResult is immutable")

    @property
    def status(self):
        return self._status

    @property
    def reason(self):
        return self._reason

    @property
    def success(self):
        return self.status == self.SUCCESS

    def __repr__(self):
        return "ProvisioningResult(status={!r}, reason={!r})".format(
            self.status,
            self.reason,
        )

    __str__ = __repr__


class ProvisioningRequest:
    """One immutable credential candidate with a one-shot result contract.

    The coordinator completes the request with ``succeed`` or ``fail``. A BLE
    disconnect completes it with ``cancel`` and also signals
    ``wait_cancelled`` so an in-progress network attempt can be stopped. After
    completing it, the coordinator can await ``wait_response_sent`` before it
    shuts down provisioning and enters running mode.
    """

    SAFE_ERROR_REASONS = (
        "wrong_password",
        "no_ap",
        "timeout",
        "connect_failed",
        "storage_failed",
        "internal_error",
    )
    SAFE_CANCEL_REASONS = (
        "client_disconnected",
        "provisioning_stopped",
    )

    __slots__ = (
        "_credentials",
        "_result",
        "_completed",
        "_cancelled_event",
        "_response_finished",
        "_response_sent",
    )

    def __init__(self, credentials):
        if not isinstance(credentials, SubmittedCredentials):
            raise TypeError("credentials must be a SubmittedCredentials value")

        self._credentials = credentials
        self._result = None
        self._completed = asyncio.Event()
        self._cancelled_event = asyncio.Event()
        self._response_finished = asyncio.Event()
        self._response_sent = False

    @property
    def credentials(self):
        return self._credentials

    @property
    def ssid(self):
        return self.credentials.ssid

    @property
    def password(self):
        return self.credentials.password

    @property
    def completed(self):
        return self._result is not None

    @property
    def cancelled(self):
        return (
            self._result is not None
            and self._result.status == ProvisioningResult.CANCELLED
        )

    def succeed(self):
        return self._complete(ProvisioningResult(ProvisioningResult.SUCCESS))

    def fail(self, reason):
        if reason not in self.SAFE_ERROR_REASONS:
            reason = "internal_error"
        return self._complete(
            ProvisioningResult(ProvisioningResult.ERROR, reason)
        )

    def cancel(self, reason="client_disconnected"):
        if reason not in self.SAFE_CANCEL_REASONS:
            reason = "provisioning_stopped"
        completed = self._complete(
            ProvisioningResult(ProvisioningResult.CANCELLED, reason)
        )
        if completed:
            self._cancelled_event.set()
        return completed

    async def wait_result(self):
        await self._completed.wait()
        return self._result

    async def wait_cancelled(self):
        await self._cancelled_event.wait()

    async def wait_response_sent(self):
        """Wait until BLE either emits the result or can no longer do so."""

        await self._response_finished.wait()
        return self._response_sent

    def _complete(self, result):
        if self._result is not None:
            raise RuntimeError("ProvisioningRequest is already completed")
        self._result = result
        self._completed.set()
        return True

    def _finish_response(self, sent):
        if self._response_finished.is_set():
            return
        self._response_sent = bool(sent)
        self._response_finished.set()

    def __repr__(self):
        return "ProvisioningRequest(ssid={!r}, password=<redacted>)".format(
            self.ssid
        )

    __str__ = __repr__


class _InvalidCommand(Exception):
    def __init__(self, reason):
        self.reason = reason


class _NoConnectionTimeout:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class BleProvisioner:
    """Own the provisioning GATT service and serialize credential requests.

    The request sink must be either an async callable or an object with an
    async ``put(request)`` method. It only receives ``ProvisioningRequest``
    values; this transport never operates Wi-Fi or persists credentials.
    """

    def __init__(
        self,
        request_sink,
        aioble_module=None,
        bluetooth_module=None,
        device_name=DEVICE_NAME,
        max_payload_bytes=MAX_PAYLOAD_BYTES,
    ):
        put = getattr(request_sink, "put", None)
        if not callable(request_sink) and not callable(put):
            raise TypeError("request_sink must be an async callable or channel")
        if not isinstance(device_name, str) or not device_name:
            raise ValueError("device_name must be a non-empty string")
        if not isinstance(max_payload_bytes, int) or isinstance(
            max_payload_bytes, bool
        ):
            raise TypeError("max_payload_bytes must be an integer")
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        if max_payload_bytes + ATT_WRITE_OVERHEAD_BYTES > MAX_ATT_MTU:
            raise ValueError("max_payload_bytes exceeds the ATT MTU limit")

        self._request_sink = request_sink
        self._aioble = aioble if aioble_module is None else aioble_module
        self._bluetooth = (
            bluetooth if bluetooth_module is None else bluetooth_module
        )
        self._device_name = device_name
        self._max_payload_bytes = max_payload_bytes

        self._service_uuid = None
        self._service = None
        self._command_characteristic = None
        self._status_characteristic = None
        self._connection = None
        self._current_request = None
        self._failure = None
        self._running = False

    @property
    def command_characteristic(self):
        return self._command_characteristic

    @property
    def status_characteristic(self):
        return self._status_characteristic

    @property
    def current_request(self):
        return self._current_request

    @property
    def failure(self):
        return self._failure

    def raise_if_failed(self):
        if self._failure is not None:
            raise self._failure

    @property
    def running(self):
        return self._running

    async def run(self):
        if self._running:
            raise RuntimeError("BleProvisioner is already running")

        self._failure = None
        self._running = True

        try:
            try:
                self._configure_mtu()
                self._register_service()

                while True:
                    connection = await self._advertise_once()
                    if connection is None:
                        raise RuntimeError(
                            "BLE advertising stopped unexpectedly"
                        )

                    self._connection = connection
                    try:
                        await self._serve_connection(connection)
                    finally:
                        await self._disconnect(connection)
                        self._connection = None
            finally:
                request = self._current_request
                if request is not None:
                    if not request.completed:
                        request.cancel("provisioning_stopped")
                    request._finish_response(False)
                    self._current_request = None

                try:
                    await self._disconnect(self._connection)
                finally:
                    self._connection = None
                    try:
                        self._stop_controller()
                    finally:
                        # A controller-stop error is still a task failure, but
                        # stale service handles must never survive teardown.
                        self._clear_service()
                        self._running = False
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failure = error
            raise

    def _configure_mtu(self):
        """Allow one complete maximum-sized command after central negotiation."""

        prepare_ble_controller(self._max_payload_bytes, self._aioble)

    def _register_service(self):
        service_uuid = self._bluetooth.UUID(SERVICE_UUID)
        command_uuid = self._bluetooth.UUID(COMMAND_CHAR_UUID)
        status_uuid = self._bluetooth.UUID(STATUS_CHAR_UUID)

        service = self._aioble.Service(service_uuid)
        command_characteristic = self._aioble.Characteristic(
            service,
            command_uuid,
            write=True,
            capture=True,
            # aioble uses the initial local value to allocate enough GATT
            # storage for a complete credential command.
            initial=bytearray(self._max_payload_bytes),
        )
        status_characteristic = self._aioble.Characteristic(
            service,
            status_uuid,
            read=True,
            notify=True,
            indicate=True,
            initial=self._encode_status("ready"),
        )

        self._aioble.register_services(service)
        self._service_uuid = service_uuid
        self._service = service
        self._command_characteristic = command_characteristic
        self._status_characteristic = status_characteristic

    async def _advertise_once(self):
        """Advertise in a child task so aioble cannot swallow our cancel."""

        completed = asyncio.Event()
        outcome = {}

        async def advertise():
            try:
                outcome["connection"] = await self._aioble.advertise(
                    ADV_INTERVAL,
                    name=self._device_name,
                    services=[self._service_uuid],
                )
            except BaseException as error:
                outcome["error"] = error
            finally:
                completed.set()

        advertise_task = asyncio.create_task(advertise())
        try:
            await completed.wait()
        except asyncio.CancelledError:
            advertise_task.cancel()
            # Await cleanup even on aioble versions whose advertise()
            # coroutine consumes its own CancelledError and returns None.
            try:
                await advertise_task
            except asyncio.CancelledError:
                pass
            connection = outcome.get("connection")
            if connection is not None:
                # aioble versions may consume advertising cancellation and
                # still return a just-established central connection. It has
                # not yet been assigned to self._connection, so settle it here.
                await self._disconnect(connection)
            raise

        error = outcome.get("error")
        if error is not None:
            raise error
        return outcome.get("connection")

    async def _serve_connection(self, connection):
        request = None

        try:
            self._publish_status(connection, "ready")

            while connection.is_connected():
                with self._connection_timeout(
                    connection,
                    CONNECTION_IDLE_TIMEOUT_MS,
                ):
                    writer, raw_data = await self._command_characteristic.written()

                if writer is not connection or not connection.is_connected():
                    continue

                try:
                    credentials = self._parse_command(raw_data)
                except _InvalidCommand as error:
                    await self._publish_result(
                        connection,
                        "invalid",
                        error.reason,
                    )
                    continue

                request = ProvisioningRequest(credentials)
                self._current_request = request
                self._publish_status(connection, "testing")

                if not connection.is_connected():
                    request.cancel("client_disconnected")
                    return

                with self._connection_timeout(connection):
                    await self._submit(request)
                    result = await request.wait_result()

                if result.success:
                    response_sent = await self._publish_result(
                        connection,
                        "success",
                    )
                elif result.status == ProvisioningResult.ERROR:
                    response_sent = await self._publish_result(
                        connection,
                        "error",
                        result.reason,
                    )
                else:
                    response_sent = await self._publish_result(
                        connection,
                        "error",
                        "cancelled",
                    )

                request._finish_response(response_sent)
                if self._current_request is request:
                    self._current_request = None
                request = None
        except Exception as error:
            if self._is_disconnect_error(error) or self._is_timeout_error(error):
                if request is not None:
                    if not request.completed:
                        request.cancel("client_disconnected")
                    request._finish_response(False)
                return
            raise
        finally:
            if request is not None:
                reason = (
                    "client_disconnected"
                    if not connection.is_connected()
                    else "provisioning_stopped"
                )
                if not request.completed:
                    request.cancel(reason)
                request._finish_response(False)
            if self._current_request is request:
                self._current_request = None

    async def _submit(self, request):
        put = getattr(self._request_sink, "put", None)
        if callable(put):
            await put(request)
            return
        await self._request_sink(request)

    def _parse_command(self, raw_data):
        if not isinstance(raw_data, (bytes, bytearray, memoryview)):
            raise _InvalidCommand("invalid_payload")
        if len(raw_data) > self._max_payload_bytes:
            raise _InvalidCommand("payload_too_large")

        try:
            text = bytes(raw_data).decode("utf-8")
        except UnicodeError:
            raise _InvalidCommand("invalid_utf8")

        try:
            command = json.loads(text)
        except (TypeError, ValueError):
            raise _InvalidCommand("invalid_json")

        if not isinstance(command, dict):
            raise _InvalidCommand("invalid_command")
        if (
            len(command) != 3
            or "type" not in command
            or "ssid" not in command
            or "password" not in command
        ):
            raise _InvalidCommand("invalid_fields")
        if command["type"] != "wifi_credentials":
            raise _InvalidCommand("invalid_command")

        ssid = command["ssid"]
        password = command["password"]
        if not isinstance(ssid, str) or not ssid:
            raise _InvalidCommand("invalid_ssid")
        if not isinstance(password, str):
            raise _InvalidCommand("invalid_password")

        if len(ssid.encode("utf-8")) > 32:
            raise _InvalidCommand("invalid_ssid")
        if len(password.encode("utf-8")) > 64:
            raise _InvalidCommand("invalid_password")

        try:
            return SubmittedCredentials(ssid, password)
        except (TypeError, ValueError):
            # The explicit checks above provide stable field-specific errors.
            # This remains a final guard if SubmittedCredentials gains one.
            raise _InvalidCommand("invalid_fields")

    def _publish_status(self, connection, status, reason=None):
        payload = self._encode_status(status, reason)
        self._status_characteristic.write(payload)

        if not connection.is_connected():
            return False

        try:
            self._status_characteristic.notify(
                connection,
                self._att_payload(payload, status),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._is_delivery_error(error, connection):
                return False
            raise
        return True

    async def _publish_result(self, connection, status, reason=None):
        """Publish a terminal result and wait for its ATT acknowledgment."""

        payload = self._encode_status(status, reason)
        self._status_characteristic.write(payload)

        if not connection.is_connected():
            return False

        try:
            await self._status_characteristic.indicate(
                connection,
                self._att_payload(payload, status),
                timeout_ms=RESULT_INDICATION_TIMEOUT_MS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._is_delivery_error(error, connection):
                return False
            raise
        return True

    @classmethod
    def _att_payload(cls, payload, status):
        """Keep outbound status updates within the default ATT payload.

        The full status, including a safe failure reason, remains as the local
        readable characteristic value. A client that receives a generic error
        or invalid token reads the characteristic before sending another
        command.
        """

        if len(payload) <= DEFAULT_ATT_PAYLOAD_BYTES:
            return payload
        return cls._encode_status(status)

    @staticmethod
    def _encode_status(status, reason=None):
        # Values are internal protocol constants containing only ASCII letters
        # and underscores. Construct this tiny payload directly to keep it
        # compact and avoid unsupported json keyword arguments on MicroPython.
        if reason is None:
            text = '{"status":"' + status + '"}'
        else:
            text = (
                '{"status":"'
                + status
                + '","reason":"'
                + reason
                + '"}'
            )
        return text.encode("utf-8")

    @staticmethod
    def _connection_timeout(connection, timeout_ms=None):
        timeout = getattr(connection, "timeout", None)
        if callable(timeout):
            return timeout(timeout_ms)
        return _NoConnectionTimeout()

    def _is_disconnect_error(self, error):
        error_type = getattr(self._aioble, "DeviceDisconnectedError", None)
        return error_type is not None and isinstance(error, error_type)

    @staticmethod
    def _is_timeout_error(error):
        timeout_error = getattr(asyncio, "TimeoutError", None)
        return timeout_error is not None and isinstance(error, timeout_error)

    def _is_delivery_error(self, error, connection):
        if not connection.is_connected() or self._is_disconnect_error(error):
            return True

        gatt_error = getattr(self._aioble, "GattError", None)
        if gatt_error is not None and isinstance(error, gatt_error):
            return True

        if self._is_timeout_error(error):
            return True

        return isinstance(error, OSError)

    async def _disconnect(self, connection):
        if connection is None or not connection.is_connected():
            return
        try:
            await connection.disconnect()
        except Exception as error:
            if self._is_disconnect_error(error) or not connection.is_connected():
                return
            raise

    def _stop_controller(self):
        stop = getattr(self._aioble, "stop", None)
        if callable(stop):
            stop()

    def _clear_service(self):
        self._service_uuid = None
        self._service = None
        self._command_characteristic = None
        self._status_characteristic = None


async def run_provisioning(request_sink):
    """Compatibility entry point for callers that do not own the class."""

    await BleProvisioner(request_sink).run()
