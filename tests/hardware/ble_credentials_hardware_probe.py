"""Destructive hardware probe for credential NVS and local aioble APIs.

Run on an ESP32 with aioble installed, for example:
    mpremote connect <port> run tests/hardware/ble_credentials_hardware_probe.py

Only the disposable ``pp_probe`` NVS namespace is used. Credential values are
never printed.
"""

import asyncio
import esp32
import gc
import sys

from lib.ble_bootstrap import prepare_ble_controller
from lib.async_channel import SingleValueChannel
from web.credentials import Credentials, CredentialStore


NVS_NAMESPACE = "pp_probe"
DIRECT_KEY = "direct"
CREDENTIAL_KEY = CredentialStore.RECORD_KEY


def _erase_if_present(nvs, key):
    try:
        nvs.erase_key(key)
    except OSError:
        pass


def _cleanup_nvs():
    nvs = esp32.NVS(NVS_NAMESPACE)
    _erase_if_present(nvs, DIRECT_KEY)
    _erase_if_present(nvs, CREDENTIAL_KEY)
    nvs.commit()
    reopened = esp32.NVS(NVS_NAMESPACE)
    _assert_missing_blob(reopened, DIRECT_KEY)
    _assert_missing_blob(reopened, CREDENTIAL_KEY)
    print("PASS probe NVS cleanup")


def _assert_missing_blob(nvs, key):
    try:
        nvs.get_blob(key, bytearray(CredentialStore.MAX_RECORD_BYTES))
    except OSError:
        return
    raise AssertionError("expected erased NVS blob")


def probe_direct_nvs():
    payload = b"planterpal-nvs-probe"
    nvs = esp32.NVS(NVS_NAMESPACE)
    nvs.set_blob(DIRECT_KEY, payload)
    nvs.commit()

    reopened = esp32.NVS(NVS_NAMESPACE)
    result = bytearray(len(payload))
    length = reopened.get_blob(DIRECT_KEY, result)
    assert length == len(payload)
    assert bytes(result[:length]) == payload

    reopened.erase_key(DIRECT_KEY)
    reopened.commit()
    _assert_missing_blob(esp32.NVS(NVS_NAMESPACE), DIRECT_KEY)
    print("PASS direct NVS blob commit/reopen/read/erase")


def _round_trip_credentials(credentials):
    CredentialStore(esp32.NVS(NVS_NAMESPACE)).save(credentials)
    loaded = CredentialStore(esp32.NVS(NVS_NAMESPACE)).load()
    assert loaded == credentials


def probe_credential_store():
    ascii_credentials = Credentials("A" * 32, "p" * 64)
    _round_trip_credentials(ascii_credentials)
    print("PASS CredentialStore ASCII boundary save/reopen/load")

    utf8_credentials = Credentials("\u00e9" * 16, "\u5bc6" * 21 + "x")
    assert len(utf8_credentials.ssid.encode("utf-8")) == 32
    assert len(utf8_credentials.password.encode("utf-8")) == 64
    _round_trip_credentials(utf8_credentials)
    print("PASS CredentialStore UTF-8 boundary save/reopen/load")

    CredentialStore(esp32.NVS(NVS_NAMESPACE)).clear()
    assert CredentialStore(esp32.NVS(NVS_NAMESPACE)).load() is None
    print("PASS CredentialStore clear/reopen/load None")


async def _cancel_and_expect(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError("task did not raise CancelledError")


async def _stop_advertising(task):
    task.cancel()

    try:
        connection = await task
    except asyncio.CancelledError:
        print("PASS aioble advertising cancellation propagated")
        return

    if connection is None:
        # The installed aioble peripheral implementation converts external
        # advertising cancellation into a normal ``None`` result after it
        # stops the controller's advertising operation.
        print("PASS aioble advertising cancellation completed normally")
        return

    # A central may connect before this probe gets a chance to cancel the
    # advertiser. In that case advertise() has already completed normally;
    # clean up the returned connection instead of treating it as a failed
    # cancellation.
    assert callable(getattr(connection, "is_connected", None))
    assert callable(getattr(connection, "disconnect", None))

    if connection.is_connected():
        await connection.disconnect(timeout_ms=1000)

    assert not connection.is_connected()
    print("PASS aioble advertising connection cleaned up")


class AuditedAioble:
    """Delegate to installed aioble while recording controller shutdown."""

    def __init__(self, module):
        self._module = module
        self.stop_calls = 0

    def __getattr__(self, name):
        return getattr(self._module, name)

    def stop(self):
        self.stop_calls += 1
        return self._module.stop()


async def probe_aioble_contract(aioble, bluetooth, stop_after=True):

    service_uuid = bluetooth.UUID("7d26a2f0-f4df-4dc3-8c49-078ca1c9b1ec")
    credentials_uuid = bluetooth.UUID("7d26a2f1-f4df-4dc3-8c49-078ca1c9b1ec")
    status_uuid = bluetooth.UUID("7d26a2f2-f4df-4dc3-8c49-078ca1c9b1ec")

    service = aioble.Service(service_uuid)
    credentials_characteristic = aioble.Characteristic(
        service,
        credentials_uuid,
        write=True,
        capture=True,
    )
    status_characteristic = aioble.Characteristic(
        service,
        status_uuid,
        read=True,
        notify=True,
    )

    try:
        aioble.register_services(service)

        status_characteristic.write(b"probe-ready")
        assert status_characteristic.read() == b"probe-ready"
        assert callable(getattr(credentials_characteristic, "written", None))
        assert callable(getattr(status_characteristic, "notify", None))
        print("PASS aioble service registration and local status read/write")

        written_task = asyncio.create_task(credentials_characteristic.written())
        await asyncio.sleep_ms(50)
        await _cancel_and_expect(written_task)
        print("PASS aioble captured-write wait cancellation")

        advertise_task = asyncio.create_task(
            aioble.advertise(
                250000,
                name="PlanterPal-Probe",
                services=[service_uuid],
            )
        )
        await asyncio.sleep_ms(200)
        await _stop_advertising(advertise_task)
    finally:
        stop = getattr(aioble, "stop", None)
        if stop_after and stop is not None:
            stop()
            print("PASS aioble controller cleanup")


async def probe_ble_provisioner_lifecycle(aioble, bluetooth):
    # main.py imports this larger module only after reserving NimBLE.
    from lib.ble_provisioning import BleProvisioner

    audited_aioble = AuditedAioble(aioble)
    provisioner = BleProvisioner(
        SingleValueChannel(),
        aioble_module=audited_aioble,
        bluetooth_module=bluetooth,
        device_name="PlanterPal-Probe",
    )
    task = asyncio.create_task(provisioner.run())

    for _ in range(100):
        if (
            provisioner.running
            and provisioner.command_characteristic is not None
            and provisioner.status_characteristic is not None
        ):
            break
        await asyncio.sleep_ms(20)
    else:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        provisioner.raise_if_failed()
        raise AssertionError("BleProvisioner did not register and start")

    assert provisioner.current_request is None
    assert provisioner.failure is None
    assert provisioner.status_characteristic.read() == b'{"status":"ready"}'
    assert callable(getattr(provisioner.command_characteristic, "written", None))
    assert callable(getattr(provisioner.status_characteristic, "notify", None))
    assert callable(getattr(provisioner.status_characteristic, "indicate", None))
    assert aioble.config("mtu") == 259
    print("PASS BleProvisioner registered service and began advertising")

    # No central is required. Cancelling while the production wrapper is
    # advertising exercises the aioble version-specific cancellation path.
    await asyncio.sleep_ms(200)
    await _cancel_and_expect(task)

    assert not provisioner.running
    assert provisioner.failure is None
    assert provisioner.current_request is None
    assert provisioner.command_characteristic is None
    assert provisioner.status_characteristic is None
    assert getattr(provisioner, "_service", None) is None
    assert getattr(provisioner, "_service_uuid", None) is None
    assert audited_aioble.stop_calls == 1
    print("PASS BleProvisioner cancellation and controller/service cleanup")


async def probe_ble():
    # Match main.py: the event loop and credential code exist first, but the
    # controller is reserved before importing the larger provisioning graph.
    gc.collect()
    prepare_ble_controller()

    import aioble
    import bluetooth

    try:
        # BleProvisioner owns activation, MTU configuration, GATT registration,
        # advertising, and shutdown. Starting a separate generic lifecycle in
        # the same interpreter is redundant and can time out on ESP32 NimBLE.
        await probe_ble_provisioner_lifecycle(aioble, bluetooth)
    finally:
        stop = getattr(aioble, "stop", None)
        if stop is not None:
            stop()


def run():
    try:
        print("Firmware implementation:", getattr(sys, "implementation", "unknown"))
        print("Firmware platform:", getattr(sys, "platform", "unknown"))
        probe_direct_nvs()
        probe_credential_store()
        try:
            asyncio.run(probe_ble())
        except ImportError as error:
            print("FAIL BLE dependencies: {}".format(error))
            raise AssertionError("BLE dependency probe failed")
        print("ALL BLE/CREDENTIAL HARDWARE TESTS PASSED")
    finally:
        _cleanup_nvs()


run()
