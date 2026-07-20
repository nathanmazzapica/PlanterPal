"""Running-mode hardware construction.

``config.py`` intentionally contains only side-effect-free values so the
minimal provisioning runtime can read GPIO assignments without constructing
the LCD I2C bus or other running-mode hardware.
"""

from machine import I2C, Pin

import config as cfg


STATUS_LED = Pin(cfg.STATUS_LED_PIN, Pin.OUT)
SCL = Pin(cfg.SCL_PIN)
SDA = Pin(cfg.SDA_PIN)
SENSOR_BUS = I2C(
    cfg.SENSOR_BUS_ID,
    scl=SCL,
    sda=SDA,
    freq=cfg.SENSOR_BUS_FREQ,
)
