import asyncio
import config as cfg
import device_hardware as hardware

from app.state import State
from display.display import Display, LCD_ADDR
from display.null_display import NullDisplay
from display.probe import LCDPresenceProbe
from led.controller import Controller
from lib.async_channel import SingleValueChannel
from lib.bh1750 import BH1750
from lib.ek1940 import EK1940
from lib.ws2811b import WS2811B
from sensors import light, moisture
from web.client import Client
from web.reporter import Reporter
from web.wifi import NetworkManager


class Application:
    """Own the components and lifecycle of the running device.

    This module is intentionally running-mode only. Provisioning completes in
    a smaller import graph and resets the machine before this module is loaded.
    """

    def __init__(
        self,
        reporter,
        display,
        state,
        state_led,
        network_manager,
        display_probe,
        null_display_factory,
    ):
        self.reporter = reporter
        self._configured_display = display
        self.display = display
        self.state = state
        self.state_led = state_led
        self.network_manager = network_manager
        self._display_probe = display_probe
        self._null_display_factory = null_display_factory

        self._network_task = None
        self._network_led_task = None
        self._reporter_task = None
        self._display_task = None
        self._mode = "stopped"

    @property
    def mode(self):
        return self._mode

    async def run(self):
        self._mode = "starting"
        fatal_failure = False

        try:
            await self._start_display()
            self._mode = "connecting"
            self._network_task = asyncio.create_task(self.network_manager.run())
            await self._connect_wifi()
            await self._ping_server()
            await self.state_led.set_state("ready")
            self._network_led_task = self._create_task(
                self._coordinate_network_led()
            )
            self._reporter_task = asyncio.create_task(self.reporter.run())
            self._mode = "running"
            await self._run_loop()
        except asyncio.CancelledError:
            raise
        except BaseException:
            fatal_failure = True
            raise
        finally:
            self._mode = "stopping"
            try:
                await self._stop_network_led()
            finally:
                try:
                    if fatal_failure:
                        # Solid colors need no live task. Leave red latched in
                        # the NeoPixel until the next reset makes a new choice.
                        await self.state_led.set_state("error")
                finally:
                    try:
                        await self._stop_reporter()
                    finally:
                        try:
                            await self._stop_display()
                        finally:
                            try:
                                await self._stop_network_manager()
                            finally:
                                try:
                                    if not fatal_failure:
                                        await self._stop_state_led()
                                finally:
                                    try:
                                        hardware.STATUS_LED.off()
                                    finally:
                                        self._mode = "stopped"

    async def _start_display(self):
        self.display = self._configured_display

        if not await self._display_probe.is_present():
            print("LCD not detected; continuing headless")
            await self._activate_display(self._null_display_factory())
            return

        try:
            await self._activate_display(self._configured_display)
        except asyncio.CancelledError:
            raise
        except OSError as error:
            await self._stop_display()
            print("LCD unavailable; continuing headless:", error)
            await self._activate_display(self._null_display_factory())

    async def _activate_display(self, display):
        if self._display_task is not None:
            raise RuntimeError("a display task is already active")

        self.display = display
        self._display_task = self._create_task(self.display.run())
        await self.display.wait_until_ready()

    async def _connect_wifi(self):
        try:
            await self.state_led.set_state("connecting")
            await self.display.write_line("connecting wifi", 0)
            await self.network_manager.wait_until_connected()
            await self.display.write_line("wifi connected", 0)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.display.display_err("Failed to connect to WiFi", 1)
            raise

    async def _ping_server(self):
        try:
            await self.display.write_line("pinging server", 0)
            code = await self.reporter.ping()
            await self.display.write_line(f"{code}", 0)
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.display.display_err("Failed to reach API", 2)
            raise

    async def _coordinate_network_led(self):
        """Translate owned NetworkManager state into Controller commands."""

        version = self.network_manager.connection_version

        while True:
            if self.network_manager.is_connected():
                await self.state_led.set_state("ready")
            else:
                await self.state_led.set_state("connecting")

            version = await self.network_manager.wait_for_connection_change(
                version
            )

    async def _run_loop(self):
        tick = 0
        hardware.STATUS_LED.on()
        while True:
            self.network_manager.raise_if_failed()
            self.reporter.raise_if_failed()
            self.display.raise_if_failed()
            await self.state.update()
            await self.display.render(
                self.state.lux_seconds,
                self.state.moisture,
                self.state.dli,
            )
            await asyncio.sleep(cfg.INTERVAL_S)
            tick += 1

            if tick % 5:
                await self.reporter.submit(self.state.to_json())

    async def _stop_reporter(self):
        if self._reporter_task is None:
            return

        self._reporter_task.cancel()

        try:
            await self._reporter_task
        except asyncio.CancelledError:
            pass
        finally:
            self._reporter_task = None

    async def _stop_network_led(self):
        if self._network_led_task is None:
            return

        self._network_led_task.cancel()

        try:
            await self._network_led_task
        except asyncio.CancelledError:
            pass
        finally:
            self._network_led_task = None

    async def _stop_display(self):
        if self._display_task is None:
            return

        self._display_task.cancel()

        try:
            await self._display_task
        except asyncio.CancelledError:
            pass
        finally:
            self._display_task = None

    async def _stop_network_manager(self):
        task = self._network_task
        try:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            self._network_task = None
            disconnect = getattr(self.network_manager, "disconnect", None)
            if callable(disconnect):
                disconnect()

    async def _stop_state_led(self):
        stop = getattr(self.state_led, "stop", None)
        if callable(stop):
            await stop()

    @staticmethod
    def _create_task(coroutine):
        try:
            return asyncio.create_task(coroutine)
        except BaseException:
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            raise


def create_application(credentials):
    """Compose the running graph from already-validated credentials."""

    if credentials is None:
        raise ValueError("running application requires credentials")

    client = Client(host=_backend_host())
    report_channel = SingleValueChannel()
    reporter = Reporter(client, report_channel, hardware.STATUS_LED.off)
    sensor_bus_lock = asyncio.Lock()
    bh1750 = BH1750(hardware.SENSOR_BUS, sensor_bus_lock)
    ek1940 = EK1940(cfg.EK1940_PIN)
    lm = light.LightMonitor(bh1750)
    mm = moisture.MoistureMonitor(ek1940)
    display_channel = SingleValueChannel()
    display = Display(hardware.SENSOR_BUS, sensor_bus_lock, display_channel)
    display_probe = LCDPresenceProbe(
        hardware.SENSOR_BUS,
        sensor_bus_lock,
        LCD_ADDR,
    )
    state = State(lm, mm)
    state_led = Controller(WS2811B(21))
    network_manager = NetworkManager(credentials=credentials)

    return Application(
        reporter,
        display,
        state,
        state_led,
        network_manager,
        display_probe,
        NullDisplay,
    )


def _backend_host():
    """Preserve legacy deployments while allowing credential-free boot."""

    try:
        from web.wifi_config import cfg as legacy_config
    except ImportError:
        return cfg.API_HOST

    return legacy_config.get("host", cfg.API_HOST)
