import asyncio
import config as cfg
from lib.bh1750 import BH1750
from lib.ek1940 import EK1940
from lib.ws2811b import WS2811B
from led.controller import Controller

from web import  wifi
from web.client import Client
from web.exceptions import ErrNetwork

from display.display import Display
from app.state import State
from sensors import light, moisture


async def main():
    client = Client()
    bh1750 = BH1750(cfg.SENSOR_BUS)
    ek1940 = EK1940(cfg.EK1940_PIN)
    lm = light.LightMonitor(bh1750)
    mm = moisture.MoistureMonitor(ek1940)
    display = Display(cfg.SENSOR_BUS)
    state = State(lm, mm)
    state_led = Controller(WS2811B(21))
    state_led.set_state("provisioning")

    try:
        display.write_line("connecting wifi", 0)
        await wifi.connect_wifi()
        display.write_line("wifi connected", 0)
        state_led.set_state("ready")
    except:
        display.display_err("Failed to connect to WiFi", 1)
        raise

    try:
        display.write_line("pinging server", 0)
        code = client.ping()
        display.write_line(f"{code}", 0)
        await asyncio.sleep(0.2)
    except:
        display.display_err("Failed to reach API", 2)
        raise

    tick = 0
    cfg.STATUS_LED.on()
    while True:
        state.update()
        display.render(state)
        await asyncio.sleep(cfg.INTERVAL_S)
        tick += 1

        if tick % 5:
            try:
                client.report(state)
            except ErrNetwork:
                cfg.STATUS_LED.off()


if __name__ == '__main__':
    asyncio.run(main())
