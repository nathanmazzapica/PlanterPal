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
        "display.null_display",
        "display.probe",
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
        "display.null_display": ("NullDisplay", object),
        "display.probe": ("LCDPresenceProbe", object),
        "app.provisioning": ("ProvisioningCoordinator", object),
        "app.state": ("State", object),
    }
    for module_name, (symbol, value) in exports.items():
        module = types.ModuleType(module_name)
        setattr(module, symbol, value)
        sys.modules[module_name] = module

    sys.modules["display.display"].LCD_ADDR = 0x27

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
    def __init__(
        self,
        cleanup_order,
        label="display",
        initialization_error=None,
        block_initialization=False,
        render_error=None,
    ):
        self.cleanup_order = cleanup_order
        self.label = label
        self.initialization_error = initialization_error
        self.block_initialization = block_initialization
        self.render_error = render_error
        self.ready = asyncio.Event()
        self.started = asyncio.Event()
        self.release_initialization = asyncio.Event()
        self.calls = []
        self.rendered = asyncio.Event()
        self.run_calls = 0

    async def run(self):
        self.run_calls += 1
        self.started.set()
        try:
            if self.block_initialization:
                await self.release_initialization.wait()
            if self.initialization_error is not None:
                return

            self.ready.set()
            while True:
                await asyncio.sleep(1)
        finally:
            self.cleanup_order.append(self.label)

    async def wait_until_ready(self):
        await self.started.wait()
        if self.initialization_error is not None:
            raise self.initialization_error
        await self.ready.wait()

    async def write_line(self, body, line):
        self.calls.append(("line", str(body), line))

    async def display_err(self, desc, error_number):
        self.calls.append(("error", desc, error_number))

    async def render(self, lux_seconds, moisture, dli):
        if self.render_error is not None:
            raise self.render_error
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
        self.connection_version = 1 if connected else 0
        self.state_changed = asyncio.Event()

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
            self.transition(True)

    def is_connected(self):
        return self.connected

    async def wait_for_connection_change(self, previous_version):
        while self.connection_version == previous_version:
            self.state_changed.clear()
            if self.connection_version != previous_version:
                break
            await self.state_changed.wait()
        return self.connection_version

    def transition(self, connected):
        if connected == self.connected:
            return
        self.connected = connected
        self.connection_version += 1
        self.state_changed.set()

    def raise_if_failed(self):
        pass


class FakeReporter:
    def __init__(self, cleanup_order, block_ping=False, ping_error=None):
        self.cleanup_order = cleanup_order
        self.block_ping = block_ping
        self.ping_error = ping_error
        self.pinging = asyncio.Event()

    async def ping(self):
        self.pinging.set()
        if self.block_ping:
            await asyncio.Event().wait()
        if self.ping_error is not None:
            raise self.ping_error
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
        self.updated = asyncio.Event()

    async def update(self):
        self.updated.set()

    def to_json(self):
        return "{}"


class FakeStateLed:
    def __init__(self):
        self.states = []
        self.stop_calls = 0
        self.state = None

    async def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        self.states.append(state)

    async def stop(self):
        self.stop_calls += 1
        self.state = None


class FakeDisplayProbe:
    def __init__(self, outcome=True, blocked=False):
        self.outcome = outcome
        self.blocked = blocked
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def is_present(self):
        self.calls += 1
        self.started.set()
        if self.blocked:
            await self.release.wait()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeNullDisplayFactory:
    def __init__(self, cleanup_order):
        self.cleanup_order = cleanup_order
        self.instances = []

    def __call__(self):
        display = FakeDisplay(self.cleanup_order, label="null")
        self.instances.append(display)
        return display


class ApplicationDisplayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def application(
        self,
        connected=False,
        block_ping=False,
        ping_error=None,
        connection_error=None,
        display_present=True,
        probe_error=None,
        block_probe=False,
        display_error=None,
        block_display_initialization=False,
        render_error=None,
    ):
        cleanup_order = []
        display = FakeDisplay(
            cleanup_order,
            initialization_error=display_error,
            block_initialization=block_display_initialization,
            render_error=render_error,
        )
        reporter = FakeReporter(
            cleanup_order,
            block_ping=block_ping,
            ping_error=ping_error,
        )
        network = FakeNetworkManager(
            cleanup_order,
            connected=connected,
            connection_error=connection_error,
        )
        led = FakeStateLed()
        probe = FakeDisplayProbe(
            outcome=probe_error if probe_error is not None else display_present,
            blocked=block_probe,
        )
        null_display_factory = FakeNullDisplayFactory(cleanup_order)
        application = application_module.Application(
            reporter,
            display,
            FakeState(),
            led,
            network,
            probe,
            null_display_factory,
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
        self.assertIsNone(app._network_led_task)
        self.assertIsNone(app._reporter_task)

    async def test_present_lcd_retains_configured_display(self):
        app, display, _, network, _, _ = self.application()
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(network.waiting.wait(), timeout=0.25)

        self.assertIs(app.display, display)
        self.assertEqual(app._display_probe.calls, 1)
        self.assertEqual(display.run_calls, 1)
        self.assertEqual(app._null_display_factory.instances, [])

        await self.cancel_application(task)

    async def test_absent_lcd_selects_null_without_starting_real_display(self):
        app, display, _, _, _, cleanup_order = self.application(
            connected=True,
            display_present=False,
        )
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(app.state.updated.wait(), timeout=0.25)

        self.assertEqual(app._display_probe.calls, 1)
        self.assertEqual(display.run_calls, 0)
        self.assertEqual(len(app._null_display_factory.instances), 1)
        self.assertIs(app.display, app._null_display_factory.instances[0])

        await self.cancel_application(task)
        self.assertEqual(cleanup_order, ["reporter", "null", "network"])

    async def test_lcd_initialization_oserror_settles_real_then_selects_null(self):
        app, display, _, _, _, cleanup_order = self.application(
            connected=True,
            display_error=OSError(116),
        )
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(app.state.updated.wait(), timeout=0.25)

        self.assertEqual(display.run_calls, 1)
        self.assertEqual(cleanup_order, ["display"])
        self.assertEqual(len(app._null_display_factory.instances), 1)
        self.assertIs(app.display, app._null_display_factory.instances[0])

        await self.cancel_application(task)
        self.assertEqual(
            cleanup_order,
            ["display", "reporter", "null", "network"],
        )

    async def test_probe_oserror_is_fatal_and_does_not_select_null(self):
        app, display, _, _, _, cleanup_order = self.application(
            probe_error=OSError(116),
        )

        with self.assertRaises(OSError):
            await app.run()

        self.assertEqual(display.run_calls, 0)
        self.assertEqual(app._null_display_factory.instances, [])
        self.assertEqual(cleanup_order, [])

    async def test_unexpected_initialization_error_remains_fatal(self):
        app, display, _, _, _, cleanup_order = self.application(
            display_error=ValueError("broken display contract"),
        )

        with self.assertRaisesRegex(ValueError, "broken display contract"):
            await app.run()

        self.assertEqual(display.run_calls, 1)
        self.assertEqual(app._null_display_factory.instances, [])
        self.assertEqual(cleanup_order, ["display"])

    async def test_cancellation_during_probe_does_not_start_a_display(self):
        app, display, _, _, _, cleanup_order = self.application(block_probe=True)
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(app._display_probe.started.wait(), timeout=0.25)

        await self.cancel_application(task)

        self.assertEqual(display.run_calls, 0)
        self.assertEqual(app._null_display_factory.instances, [])
        self.assertEqual(cleanup_order, [])

    async def test_cancellation_during_real_initialization_does_not_fallback(self):
        app, display, _, _, _, cleanup_order = self.application(
            block_display_initialization=True,
        )
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(display.started.wait(), timeout=0.25)

        await self.cancel_application(task)

        self.assertEqual(app._null_display_factory.instances, [])
        self.assertEqual(cleanup_order, ["display"])

    async def test_oserror_after_readiness_remains_fatal(self):
        app, _, _, _, _, cleanup_order = self.application(
            connected=True,
            render_error=OSError(116),
        )

        with self.assertRaises(OSError):
            await app.run()

        self.assertEqual(app._null_display_factory.instances, [])
        self.assertEqual(cleanup_order, ["reporter", "display", "network"])

    async def test_cancellation_while_pinging_does_not_render_error(self):
        app, display, reporter, _, led, cleanup_order = self.application(
            connected=True,
            block_ping=True,
        )
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(reporter.pinging.wait(), timeout=0.25)

        await self.cancel_application(task)

        self.assertFalse(any(call[0] == "error" for call in display.calls))
        self.assertEqual(led.states, ["connecting"])
        self.assertEqual(led.stop_calls, 1)
        self.assertEqual(cleanup_order, ["display", "network"])

    async def test_rejected_health_check_never_enters_ready_or_running(self):
        from web.exceptions import ErrHttpStatus

        failure = ErrHttpStatus(503)
        app, display, _, _, led, cleanup_order = self.application(
            connected=True,
            ping_error=failure,
        )

        with self.assertRaises(ErrHttpStatus) as raised:
            await app.run()

        self.assertIs(raised.exception, failure)
        self.assertFalse(app.state.updated.is_set())
        self.assertEqual(led.states, ["connecting", "error"])
        self.assertEqual(led.stop_calls, 0)
        self.assertEqual(led.state, "error")
        self.assertIn(("error", "Failed to reach API", 2), display.calls)
        self.assertEqual(app.mode, "stopped")
        self.assertIsNone(app._reporter_task)
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
        self.assertEqual(led.stop_calls, 0)
        self.assertEqual(led.state, "error")
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

    async def test_network_loss_and_recovery_drive_cyan_then_green(self):
        app, _, _, network, led, _ = self.application(connected=True)
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(app.state.updated.wait(), timeout=0.25)

        network.transition(False)
        for _ in range(100):
            if led.state == "connecting":
                break
            await asyncio.sleep(0)
        self.assertEqual(led.state, "connecting")

        network.transition(True)
        for _ in range(100):
            if led.state == "ready":
                break
            await asyncio.sleep(0)
        self.assertEqual(led.state, "ready")
        self.assertEqual(
            led.states,
            ["connecting", "ready", "connecting", "ready"],
        )

        await self.cancel_application(task)
        self.assertEqual(led.stop_calls, 1)
        self.assertIsNone(led.state)

    async def test_unexpected_runtime_failure_latches_red_without_stopping_led(self):
        app, _, _, _, led, cleanup_order = self.application(
            connected=True,
            render_error=ValueError("broken reading frame"),
        )

        with self.assertRaisesRegex(ValueError, "broken reading frame"):
            await app.run()

        self.assertEqual(led.states, ["connecting", "ready", "error"])
        self.assertEqual(led.stop_calls, 0)
        self.assertEqual(led.state, "error")
        self.assertIsNone(app._network_led_task)
        self.assertEqual(cleanup_order, ["reporter", "display", "network"])


if __name__ == "__main__":
    unittest.main()
