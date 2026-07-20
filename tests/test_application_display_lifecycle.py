import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_PATH = PROJECT_ROOT / "app" / "application.py"


class FakeStatusPin:
    def __init__(self):
        self.calls = []

    def on(self):
        self.calls.append("on")

    def off(self):
        self.calls.append("off")


def import_application_module():
    names = (
        "config",
        "device_hardware",
        "lib.bh1750",
        "lib.ek1940",
        "lib.ws2811b",
        "lib.async_channel",
        "lib.ble_provisioning",
        "led.controller",
        "web.client",
        "web.credentials",
        "web.reporter",
        "web.wifi",
        "web.wifi_config",
        "display.display",
        "app.provisioning",
        "app.state",
        "sensors",
    )
    old_modules = {name: sys.modules.get(name) for name in names}

    config = types.ModuleType("config")
    config.INTERVAL_S = 3600
    config.PROVISIONING_MONITOR_INTERVAL_S = 0
    config.API_HOST = "api.example"
    sys.modules["config"] = config

    device_hardware = types.ModuleType("device_hardware")
    device_hardware.STATUS_LED = FakeStatusPin()
    device_hardware.SENSOR_BUS = object()
    sys.modules["device_hardware"] = device_hardware

    exports = {
        "lib.bh1750": ("BH1750", object),
        "lib.ek1940": ("EK1940", object),
        "lib.ws2811b": ("WS2811B", object),
        "lib.async_channel": ("SingleValueChannel", object),
        "lib.ble_provisioning": ("BleProvisioner", object),
        "led.controller": ("Controller", object),
        "web.client": ("Client", object),
        "web.credentials": ("CredentialStore", object),
        "web.reporter": ("Reporter", object),
        "web.wifi": ("NetworkManager", object),
        "display.display": ("Display", object),
        "app.provisioning": ("ProvisioningCoordinator", object),
        "app.state": ("State", object),
    }
    for module_name, (symbol, value) in exports.items():
        module = types.ModuleType(module_name)
        setattr(module, symbol, value)
        sys.modules[module_name] = module

    wifi_config = types.ModuleType("web.wifi_config")
    wifi_config.cfg = {"ssid": "test", "pw": "test"}
    sys.modules["web.wifi_config"] = wifi_config

    sensors = types.ModuleType("sensors")
    sensors.light = types.SimpleNamespace(LightMonitor=object)
    sensors.moisture = types.SimpleNamespace(MoistureMonitor=object)
    sys.modules["sensors"] = sensors

    spec = importlib.util.spec_from_file_location(
        "application_display_lifecycle_under_test",
        APPLICATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in old_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


application_module = import_application_module()


class FakeDisplay:
    def __init__(self, cleanup_order):
        self.cleanup_order = cleanup_order
        self.ready = asyncio.Event()
        self.calls = []
        self.rendered = asyncio.Event()

    async def run(self):
        self.ready.set()
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            self.cleanup_order.append("display")

    async def wait_until_ready(self):
        await self.ready.wait()

    async def write_line(self, body, line):
        self.calls.append(("line", str(body), line))

    async def display_err(self, desc, error_number):
        self.calls.append(("error", desc, error_number))

    async def render(self, lux_seconds, moisture, dli):
        self.calls.append(("render", lux_seconds, moisture, dli))
        self.rendered.set()

    def raise_if_failed(self):
        pass


class FakeNetworkManager:
    def __init__(self, cleanup_order, connected=False, connection_error=None):
        self.cleanup_order = cleanup_order
        self.connected = connected
        self.connection_error = connection_error
        self.has_credentials = True
        self.waiting = asyncio.Event()
        self.release_connection = asyncio.Event()

    async def run(self):
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            self.cleanup_order.append("network")

    async def wait_until_connected(self):
        self.waiting.set()
        if self.connection_error is not None:
            raise self.connection_error
        if not self.connected:
            await self.release_connection.wait()

    def raise_if_failed(self):
        pass


class FakeReporter:
    def __init__(self, cleanup_order, block_ping=False):
        self.cleanup_order = cleanup_order
        self.block_ping = block_ping
        self.pinging = asyncio.Event()

    async def ping(self):
        self.pinging.set()
        if self.block_ping:
            await asyncio.Event().wait()
        return 204

    async def run(self):
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            self.cleanup_order.append("reporter")

    async def submit(self, payload):
        pass

    def raise_if_failed(self):
        pass


class FakeState:
    def __init__(self):
        self.lux_seconds = 10
        self.moisture = 50
        self.dli = 0.25

    async def update(self):
        pass

    def to_json(self):
        return "{}"


class FakeStateLed:
    def __init__(self):
        self.states = []
        self.stop_calls = 0

    def set_state(self, state):
        self.states.append(state)

    async def stop(self):
        self.stop_calls += 1


class ApplicationDisplayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def application(self, connected=False, block_ping=False, connection_error=None):
        cleanup_order = []
        display = FakeDisplay(cleanup_order)
        reporter = FakeReporter(cleanup_order, block_ping=block_ping)
        network = FakeNetworkManager(
            cleanup_order,
            connected=connected,
            connection_error=connection_error,
        )
        led = FakeStateLed()
        application = application_module.Application(
            reporter,
            display,
            FakeState(),
            led,
            network,
        )
        return application, display, reporter, network, led, cleanup_order

    async def cancel_application(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_cancellation_while_connecting_does_not_render_error(self):
        app, display, _, network, led, cleanup_order = self.application()
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(network.waiting.wait(), timeout=0.25)

        await self.cancel_application(task)

        self.assertFalse(any(call[0] == "error" for call in display.calls))
        self.assertEqual(led.states, ["connecting"])
        self.assertEqual(led.stop_calls, 1)
        self.assertEqual(cleanup_order, ["display", "network"])
        self.assertIsNone(app._display_task)
        self.assertIsNone(app._network_task)
        self.assertIsNone(app._reporter_task)

    async def test_cancellation_while_pinging_does_not_render_error(self):
        app, display, reporter, _, led, cleanup_order = self.application(
            connected=True,
            block_ping=True,
        )
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(reporter.pinging.wait(), timeout=0.25)

        await self.cancel_application(task)

        self.assertFalse(any(call[0] == "error" for call in display.calls))
        self.assertEqual(led.states, ["connecting", "ready"])
        self.assertEqual(led.stop_calls, 1)
        self.assertEqual(cleanup_order, ["display", "network"])

    async def test_connection_failure_changes_cyan_fade_to_solid_red(self):
        failure = RuntimeError("association failed")
        app, display, _, _, led, cleanup_order = self.application(
            connection_error=failure,
        )

        with self.assertRaisesRegex(RuntimeError, "association failed"):
            await app.run()

        self.assertIn(("error", "Failed to connect to WiFi", 1), display.calls)
        self.assertEqual(led.states, ["connecting", "error"])
        self.assertEqual(led.stop_calls, 1)
        self.assertEqual(cleanup_order, ["display", "network"])

    async def test_startup_messages_complete_before_first_reading_frame(self):
        app, display, _, _, led, cleanup_order = self.application(connected=True)
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(display.rendered.wait(), timeout=0.25)

        self.assertEqual(
            display.calls[:5],
            [
                ("line", "connecting wifi", 0),
                ("line", "wifi connected", 0),
                ("line", "pinging server", 0),
                ("line", "204", 0),
                ("render", 10, 50, 0.25),
            ],
        )
        self.assertEqual(led.states, ["connecting", "ready"])

        await self.cancel_application(task)
        self.assertEqual(cleanup_order, ["reporter", "display", "network"])


if __name__ == "__main__":
    unittest.main()
