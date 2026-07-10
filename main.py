import config as cfg
import time
from lib.bh1750 import BH1750
from lib.ek1940 import EK1940

from web import client, wifi
from web.exceptions import ErrNetwork

from display.display import Display
from app.state import State
from sensors import light, moisture

if __name__ == '__main__':
    bh1750 = BH1750(cfg.SENSOR_BUS)
    ek1940 = EK1940(cfg.EK1940_PIN)
    lm = light.LightMonitor(bh1750)
    mm = moisture.MoistureMonitor(ek1940)
    display = Display(cfg.SENSOR_BUS)
    state = State(lm, mm)

    try:
        display.write_line("connecting wifi", 0)
        wifi.connect_wifi()
        display.write_line("wifi connected", 0)
    except:
        display.display_err("Failed to connect to WiFi", 1)
        raise

    try:
        display.write_line("pinging server", 0)
        code = client.ping()
        display.write_line(f"{code}", 0)
        time.sleep(0.2)
    except:
        display.display_err("Failed to reach API", 2)
        raise

    tick = 0
    cfg.STATUS_LED.on()
    while True:
        state.update()
        display.render(state)
        time.sleep(cfg.INTERVAL_S)
        tick += 1

        if tick % 5:
            try:
                client.report(state)
            except ErrNetwork:
                cfg.STATUS_LED.off()



