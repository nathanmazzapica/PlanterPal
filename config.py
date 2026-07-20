TEST_PLANT = {
    "min_moisture": 30,
    "max_moisture": 85,
}

STATUS_LED_PIN = 2
SCL_PIN = 27  # white
SDA_PIN = 26  # purple
SENSOR_BUS_ID = 0
SENSOR_BUS_FREQ = 100_000

EK1940_PIN = 32

_TICKRATE = 120
INTERVAL_S = _TICKRATE / 60

# Backend configuration is not a Wi-Fi credential and must be available even
# on a factory-fresh device that has not been provisioned yet.
API_HOST = "api.com"
