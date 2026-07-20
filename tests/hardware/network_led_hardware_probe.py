"""Exercise transient WLAN recovery and the running NeoPixel lifecycle.

This probe uses the saved NVS credentials and the real ESP32 station and
NeoPixel. It injects one transient ``ifconfig()`` failure and one failed
reconnect call through a WLAN wrapper so cyan remains visible during backoff.
It does not change or persist credentials.

Run after deploying ``web/wifi.py``, ``led/controller.py``, and
``app/application.py``:

    mpremote connect <port> run tests/hardware/network_led_hardware_probe.py

Expected visible sequence: cyan -> green -> cyan -> green -> red -> off.
The red phase lasts five seconds. Production fatal red remains latched until
reboot; this finite probe turns it off during deliberate cleanup.
"""

import asyncio
import network

from app.application import Application
from led.controller import Controller
from lib.ws2811b import WS2811B
from web.credentials import CredentialStore
from web.wifi import NetworkManager


NEOPIXEL_PIN = 21
VISIBLE_HOLD_S = 2
FATAL_HOLD_S = 5


class FaultInjectingWLAN:
    """Delegate to a real WLAN while injecting one controlled read fault."""

    def __init__(self, station):
        self._station = station
        self._fail_next_ifconfig = False
        self._fail_next_connect = False

    def inject_transient_reconnect(self):
        self._fail_next_ifconfig = True
        self._fail_next_connect = True

    def active(self, value):
        return self._station.active(value)

    def connect(self, ssid, password):
        if self._fail_next_connect:
            self._fail_next_connect = False
            raise OSError(16)
        return self._station.connect(ssid, password)

    def disconnect(self):
        return self._station.disconnect()

    def isconnected(self):
        return self._station.isconnected()

    def ifconfig(self):
        if self._fail_next_ifconfig:
            self._fail_next_ifconfig = False
            raise OSError(116)
        return self._station.ifconfig()

    def status(self):
        return self._station.status()


class LedCoordinatorHarness:
    def __init__(self, network_manager, state_led):
        self.network_manager = network_manager
        self.state_led = state_led


async def wait_for_led(controller, state, timeout_s=5):
    async def wait():
        while controller.state != state:
            await asyncio.sleep(0.05)

    await asyncio.wait_for(wait(), timeout_s)


async def cancel_and_expect(task, label):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError(label + " did not propagate cancellation")


async def main():
    credentials = CredentialStore().load()
    if credentials is None:
        raise RuntimeError("network/LED probe requires saved Wi-Fi credentials")

    station = FaultInjectingWLAN(network.WLAN(network.STA_IF))
    manager = NetworkManager(credentials=credentials, wlan=station)
    controller = Controller(WS2811B(NEOPIXEL_PIN))
    network_task = None
    coordinator_task = None

    try:
        print("[probe] cyan: initial Wi-Fi connection")
        await controller.set_state("connecting")
        network_task = asyncio.create_task(manager.run())
        await asyncio.wait_for(manager.wait_until_connected(), 30)

        print("[probe] green: initial connection established")
        await controller.set_state("ready")
        coordinator = LedCoordinatorHarness(manager, controller)
        coordinator_task = asyncio.create_task(
            Application._coordinate_network_led(coordinator)
        )
        await asyncio.sleep(VISIBLE_HOLD_S)

        initial_version = manager.connection_version
        station.inject_transient_reconnect()
        await manager.wait_for_connection_change(initial_version)
        await wait_for_led(controller, "connecting")
        assert not manager.is_connected()
        assert manager.failure is None
        print("[probe] cyan: transient ifconfig failure entered reconnect/backoff")

        await asyncio.wait_for(manager.wait_until_connected(), 30)
        await wait_for_led(controller, "ready")
        assert manager.connection_version >= initial_version + 2
        assert manager.failure is None
        print("[probe] green: same NetworkManager task recovered")
        await asyncio.sleep(VISIBLE_HOLD_S)

        await cancel_and_expect(coordinator_task, "LED coordinator")
        coordinator_task = None
        await controller.set_state("error")
        print("[probe] red: simulated fatal state (held for 5 seconds)")
        await asyncio.sleep(FATAL_HOLD_S)
        print("[probe] PASS transient recovery and LED lifecycle")
    finally:
        if coordinator_task is not None:
            await cancel_and_expect(coordinator_task, "LED coordinator")
        if network_task is not None:
            await cancel_and_expect(network_task, "NetworkManager")
        await controller.stop()
        assert not manager.is_connected()
        print("[probe] off: deliberate probe cleanup settled all tasks")


asyncio.run(main())
