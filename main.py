"""
    Project uses BH1750 for light measurement
    Standard ESP32S dev board
"""

from machine import Pin, I2C, ADC
import time
import os

from lib.pcf8574 import PCF8574
from lib.hd44780 import HD44780
from lib.lcd import LCD

STATUS_LED = Pin(2, Pin.OUT)
SCL = Pin(27) # white
SDA = Pin(26) # purple

# LIGHT SENSOR
BH1750_MEASUREMENT_CONSTANT_S = 0.5
BH1750_ADDR = 0x23

# SOIL MOISTURE SENSOR
MAX_WET = 14_000
MAX_DRY = 40_000
CAP_SOIL_SENSOR = ADC(Pin(32))

LCD_ADDR = 0x27

# measured in seconds
INTERVAL = 1
MAX_TIME = 300

# refresh the display every N ticks
DISPLAY_EVERY = 1


LOG_PATH = "light.csv"

SENSOR_BUS = I2C(0, scl=SCL, sda=SDA, freq=100_000)

pcf = PCF8574(SENSOR_BUS, address=LCD_ADDR)
hd = HD44780(pcf, num_lines=2, num_columns=16)
DISPLAY_LCD = LCD(hd, pcf)

def measure_lux():
    STATUS_LED.on()
    SENSOR_BUS.writeto(BH1750_ADDR, bytes([0b0010_0000]))
    time.sleep(BH1750_MEASUREMENT_CONSTANT_S)
    STATUS_LED.off()
    return SENSOR_BUS.readfrom(BH1750_ADDR, 2)

def measure_moisture():
    """
        Measures the raw moisture and returns % between 0 and 100
    """
    raw = CAP_SOIL_SENSOR.read_u16()
    return max(100 - ((raw - MAX_WET) * 100) / (MAX_DRY - MAX_WET), 0)

def convert(data):
    return (data[0] << 8 | data[1]) / 1.2

def run():
    lux = convert(measure_lux())
    return (time.ticks_ms(), lux)


def get_ticks() -> int:
    """
        Returns number of ticks to hit MAX_TIME run-time
    """
    return int(MAX_TIME // (INTERVAL + BH1750_MEASUREMENT_CONSTANT_S))

def log_exists() -> bool:
    try:
        os.stat(LOG_PATH)
        return True
    except OSError:
        return False

def initialize_log():
    with open(LOG_PATH, "w") as log:
        log.write("timestamp_ms,lux\n")

def append_log(point):
    with open(LOG_PATH, "a") as log:
        timestamp_ms, lux = point
        log.write(f"{timestamp_ms},{lux}\n")


def estimate_dli(lux):
    return lux / (54 * 1_000_000)

def format_lux(lux):
    if lux < 1_000:
        return f"{lux:.0f}"

    k_lux = lux / 1_000

    if lux < 100_000:
        return f"{k_lux:.1f}K"

    return f"{k_lux:.0f}K"

def display(lux, dli, moisture):
    DISPLAY_LCD.write_line(f"Lux:{format_lux(lux)}s|M:{moisture:.0f}%", 0)
    DISPLAY_LCD.write_line(f"DLI:{dli}", 1)
    
def trapezoid_area(a, b):
    t1, h1 = a
    t2, h2 = b

    interval_s = time.ticks_diff(t2, t1) / 1000

    return interval_s * (h1 + h2) / 2

if __name__ == '__main__':
    DISPLAY_LCD.backlight_on()
    DISPLAY_LCD.write_line(" PENDING  FIRST", 0)
    DISPLAY_LCD.write_line("      READ", 1)
    if not log_exists():
        initialize_log()
    
    previous = None
    lux_seconds = 0
    moisture = measure_moisture()
        
    tick = 0
    #for a in range(get_ticks()):
    while True:
        # TODO: run() does an I2C read that can raise OSError on a bus glitch
        # (loose wiring, noise, sensor not ACKing). Uncaught here it kills the
        # whole loop and stops logging. Wrap in try/except, skip the tick, and
        # signal the fault (e.g. blink STATUS_LED) so unattended runs survive.
        point = run()
        append_log(point)
        
        if previous:
            lux_seconds += trapezoid_area(previous, point)
        
        previous = point
        
        time.sleep(INTERVAL)
        tick += 1
        
        if tick == 1:
            continue

        if tick % DISPLAY_EVERY == 0:
            moisture = measure_moisture()
        
        if tick % DISPLAY_EVERY == 0:
            est_dli = estimate_dli(lux_seconds)
            display(lux_seconds, est_dli, moisture)
    

