from machine import Pin, I2C, ADC
import time
import sys
from lib.bh1750 import BH1750
from lib.ek1940 import EK1940
from lib.pcf8574 import PCF8574
from lib.hd44780 import HD44780
from lib.lcd import LCD

from web import client, wifi
from web.exceptions import ErrNetwork

from display.display import Display
from app.state import State
from sensors import light, moisture

STATUS_LED = Pin(2, Pin.OUT)
SCL = Pin(27) # white
SDA = Pin(26) # purple

LCD_ADDR = 0x27

SENSOR_BUS = I2C(0, scl=SCL, sda=SDA, freq=100_000)

pcf = PCF8574(SENSOR_BUS, address=LCD_ADDR)
hd = HD44780(pcf, num_lines=2, num_columns=16)
DISPLAY_LCD = LCD(hd, pcf)

if __name__ == '__main__':
    bh1750 = BH1750(SENSOR_BUS)
    ek1940 = EK1940(32)
    lm = light.LightMonitor(bh1750)
    mm = moisture.MoistureMonitor(ek1940)
    display = Display(DISPLAY_LCD)
    state = State(lm, mm)

    try:
        from web import wifi
        DISPLAY_LCD.write_line("connecting wifi", 0)
        wifi.connect_wifi()
        DISPLAY_LCD.write_line("wifi connected", 0)
    except:
        display.display_err("Failed to connect to WiFi", 1)
        raise

    try:
        DISPLAY_LCD.write_line("pinging server", 0)
        code = client.ping()
        DISPLAY_LCD.write_line(f"{code}", 0)
        time.sleep(0.2)
    except:
        display.display_err("Failed to reach API", 2)
        raise

    tick = 0
    STATUS_LED.on()
    while True:
        state.update()
        display.render(state)
        time.sleep(0.25)
        tick += 1

        if tick % 5:
            try:
                client.report(state)
            except ErrNetwork:
                STATUS_LED.off()



