import asyncio
import config as cfg
from lib.bh1750 import BH1750
from lib.ek1940 import EK1940
from lib.ws2811b import WS2811B
from led.controller import Controller
from lib.async_channel import SingleValueChannel

from web.client import Client
from web.reporter import Reporter
from web.wifi import NetworkManager
from web.wifi_config import cfg as wifi_cfg

from display.display import Display
from app.state import State
from sensors import light, moisture


class Application:
    """Owns the components and lifecycle of the running device."""

    def __init__(self, reporter, display, state, state_led, network_manager):
        self.reporter = reporter
        self.display = display
        self.state = state
        self.state_led = state_led
        self.network_manager = network_manager
        self._network_task = None
        self._reporter_task = None

    async def run(self):
        self.state_led.set_state("provisioning")
        self._network_task = asyncio.create_task(self.network_manager.run())

        try:
            await self._connect_wifi()
            await self._ping_server()
            self._reporter_task = asyncio.create_task(self.reporter.run())
            await self._run_loop()
        finally:
            try:
                await self._stop_reporter()
            finally:
                await self._stop_network_manager()

    async def _connect_wifi(self):
        try:
            self.display.write_line("connecting wifi", 0)
            await self.network_manager.wait_until_connected()
            self.display.write_line("wifi connected", 0)
            self.state_led.set_state("ready")
        except:
            self.display.display_err("Failed to connect to WiFi", 1)
            self.state_led.set_state("error")
            raise

    async def _ping_server(self):
        try:
            self.display.write_line("pinging server", 0)
            code = await self.reporter.ping()
            self.display.write_line(f"{code}", 0)
            await asyncio.sleep(0.2)
        except:
            self.display.display_err("Failed to reach API", 2)
            self.state_led.set_state("error")
            raise

    async def _run_loop(self):
        tick = 0
        cfg.STATUS_LED.on()
        while True:
            self.network_manager.raise_if_failed()
            self.reporter.raise_if_failed()
            await self.state.update()
            self.display.render(self.state)
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

    async def _stop_network_manager(self):
        if self._network_task is None:
            return

        self._network_task.cancel()

        try:
            await self._network_task
        except asyncio.CancelledError:
            pass
        finally:
            self._network_task = None


def create_application():
    client = Client()
    report_channel = SingleValueChannel()
    reporter = Reporter(client, report_channel, cfg.STATUS_LED.off)
    sensor_bus_lock = asyncio.Lock()
    bh1750 = BH1750(cfg.SENSOR_BUS, sensor_bus_lock)
    ek1940 = EK1940(cfg.EK1940_PIN)
    lm = light.LightMonitor(bh1750)
    mm = moisture.MoistureMonitor(ek1940)
    display = Display(cfg.SENSOR_BUS)
    state = State(lm, mm)
    state_led = Controller(WS2811B(21))
    network_manager = NetworkManager(wifi_cfg["ssid"], wifi_cfg["pw"])

    return Application(reporter, display, state, state_led, network_manager)


async def main():
    application = create_application()
    await application.run()


if __name__ == '__main__':
    asyncio.run(main())
