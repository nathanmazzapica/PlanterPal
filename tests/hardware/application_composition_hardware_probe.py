"""Exercise the import-isolated provisioning composition without main.py.

Run only after the changed provisioning modules and aioble are installed:
    mpremote connect <port> run tests/hardware/application_composition_hardware_probe.py

The probe refuses all persistence and preserves any existing production
credential record. It starts the real inactive-Wi-Fi-first BLE composition,
verifies that no running application modules were imported, then cancels and
checks ordered cleanup.
"""

import asyncio
import gc
import sys
import time

from app.provisioning_runtime import ProvisioningRuntime
from web.credentials import CredentialStore


STARTUP_TIMEOUT_MS = 8_000
STABILITY_PERIOD_MS = 1_000
FORBIDDEN_RUNNING_MODULES = (
    "app.application",
    "app.state",
    "device_hardware",
    "display.display",
    "display.null_display",
    "display.probe",
    "led.controller",
    "sensors.light",
    "sensors.moisture",
    "web.client",
    "web.reporter",
)


class RejectingCredentialStore:
    """Fail closed if the bounded probe unexpectedly reaches persistence."""

    def save(self, credentials):
        raise AssertionError("composition probe must not persist credentials")

    def clear(self):
        raise AssertionError("composition probe must not clear credentials")


def _memory_checkpoint(label):
    gc.collect()
    print("[composition] memory {}: {} free".format(label, gc.mem_free()))


def _raise_component_failures(runtime):
    if runtime.ble_provisioner is not None:
        runtime.ble_provisioner.raise_if_failed()
    if runtime.coordinator is not None:
        runtime.coordinator.raise_if_failed()
    if runtime.network_manager is not None:
        runtime.network_manager.raise_if_failed()


def _is_ready(runtime):
    return (
        runtime.ready.is_set()
        and runtime.ble_provisioner is not None
        and runtime.ble_provisioner.running
        and runtime.ble_provisioner.command_characteristic is not None
        and runtime.ble_provisioner.status_characteristic is not None
        and runtime.coordinator is not None
        and runtime.coordinator.running
    )


async def _wait_until_ready(runtime):
    started_at = time.ticks_ms()
    while not _is_ready(runtime):
        _raise_component_failures(runtime)
        if time.ticks_diff(time.ticks_ms(), started_at) >= STARTUP_TIMEOUT_MS:
            raise AssertionError("provisioning composition did not become ready")
        await asyncio.sleep_ms(20)


async def _cancel_and_expect(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError("provisioning task did not propagate cancellation")


def _unexpected_reset():
    raise AssertionError("bounded composition probe must never reset the board")


def _assert_exclusive_provisioning(runtime):
    imported = [name for name in FORBIDDEN_RUNNING_MODULES if name in sys.modules]
    assert not imported, "running modules imported during provisioning: {}".format(
        imported
    )
    assert runtime.network_manager._wlan is runtime.station
    assert not runtime.station.active()
    assert not runtime.network_manager.has_credentials
    assert not runtime.network_manager.running
    assert not runtime.network_manager.busy
    assert not runtime.network_manager.is_connected()
    assert runtime._ble_task is not None
    assert runtime._coordinator_task is not None
    assert runtime.indicator is not None
    assert runtime.indicator.running
    print("[composition] PASS provisioning graph excludes running components")
    print("[composition] PASS NetworkManager owns the pre-reserved WLAN handle")
    print("[composition] PASS GPIO2 provisioning PWM is running")


def _assert_clean_shutdown(runtime, indicator):
    assert not runtime.ready.is_set()
    assert runtime._ble_task is None
    assert runtime._coordinator_task is None
    assert runtime.indicator is None
    assert not indicator.running
    assert not runtime.ble_provisioner.running
    assert runtime.ble_provisioner.command_characteristic is None
    assert runtime.ble_provisioner.status_characteristic is None
    assert not runtime.coordinator.running
    assert not runtime.network_manager.running
    assert not runtime.network_manager.busy
    assert not runtime.network_manager.is_connected()
    print("[composition] PASS provisioning owners stopped cleanly")


async def probe_application_composition():
    original_credentials = CredentialStore().load()
    if original_credentials is not None:
        print("[composition] NOTE preserving existing production credentials")

    imported = [name for name in FORBIDDEN_RUNNING_MODULES if name in sys.modules]
    assert not imported, "probe began with running modules already imported: {}".format(
        imported
    )

    _memory_checkpoint("before provisioning runtime")
    runtime = ProvisioningRuntime(
        RejectingCredentialStore(),
        reset=_unexpected_reset,
    )
    task = asyncio.create_task(runtime.run())
    indicator = None

    try:
        await _wait_until_ready(runtime)
        indicator = runtime.indicator
        _memory_checkpoint("after BLE service registration and GPIO2 PWM")
        _assert_exclusive_provisioning(runtime)
        print("[composition] PASS BLE service registered and advertising")

        await asyncio.sleep_ms(STABILITY_PERIOD_MS)
        _raise_component_failures(runtime)
        _assert_exclusive_provisioning(runtime)
        print("[composition] PASS minimal composition remained responsive")
    finally:
        await _cancel_and_expect(task)

    _assert_clean_shutdown(runtime, indicator)
    assert CredentialStore().load() == original_credentials
    print("[composition] PASS production credentials remained unchanged")
    _memory_checkpoint("after shutdown")


def run():
    print("Firmware implementation:", getattr(sys, "implementation", "unknown"))
    print("Firmware platform:", getattr(sys, "platform", "unknown"))
    asyncio.run(probe_application_composition())
    print("ALL PROVISIONING COMPOSITION HARDWARE TESTS PASSED")


run()
