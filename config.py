from machine import Pin, I2C
TEST_PLANT = {
    "min_moisture": 30,
    "max_moisture": 85,
}

STATUS_LED = Pin(2, Pin.OUT)
SCL = Pin(27) # white
SDA = Pin(26) # purple

SENSOR_BUS = I2C(0, scl=SCL, sda=SDA, freq=100_000)

EK1940_PIN = 32

_TICKRATE = 120
INTERVAL_S = _TICKRATE / 60

WIFI_CONNECT_TIMEOUT_S = 20
WIFI_POLL_INTERVAL_S = 0.5
WIFI_MONITOR_INTERVAL_S = 1
WIFI_RECONNECT_BACKOFF_S = (1, 2, 4, 8, 16, 30)
