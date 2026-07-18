import asyncio
import config as cfg
from lib.bh1750 import BH1750
from lib.ek1940 import EK1940
from lib.ws2811b import WS2811B
from led.controller import Controller

from web import wifi
from web.client import Client
from web.exceptions import ErrNetwork

from display.display import Display
from app.state import State
from sensors import light, moisture


class Application:
    """Owns the components and lifecycle of the running device."""

    def __init__(self, client, display, state, state_led):
        self.client = client
        self.display = display
        self.state = state
        self.state_led = state_led

    async def run(self):
        self.state_led.set_state("provisioning")
        await self._connect_wifi()
        await self._ping_server()
        await self._run_loop()

    async def _connect_wifi(self):
        try:
            self.display.write_line("connecting wifi", 0)
            await wifi.connect_wifi()
            self.display.write_line("wifi connected", 0)
            self.state_led.set_state("ready")
        except:
            self.display.display_err("Failed to connect to WiFi", 1)
            self.state_led.set_state("error")
            raise

    async def _ping_server(self):
        try:
            self.display.write_line("pinging server", 0)
            code = self.client.ping()
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
            self.state.update()
            self.display.render(self.state)
            await asyncio.sleep(cfg.INTERVAL_S)
            tick += 1

            if tick % 5:
                try:
                    self.client.report(self.state)
                except ErrNetwork:
                    cfg.STATUS_LED.off()


def create_application():
    client = Client()
    bh1750 = BH1750(cfg.SENSOR_BUS)
    ek1940 = EK1940(cfg.EK1940_PIN)
    lm = light.LightMonitor(bh1750)
    mm = moisture.MoistureMonitor(ek1940)
    display = Display(cfg.SENSOR_BUS)
    state = State(lm, mm)
    state_led = Controller(WS2811B(21))

    return Application(client, display, state, state_led)


async def main():
    application = create_application()
    await application.run()


if __name__ == '__main__':
    asyncio.run(main())
