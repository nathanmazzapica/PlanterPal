"""Network timing policy without hardware side effects.

Provisioning imports ``web.wifi`` before the normal application graph. Keeping
these values outside ``config.py`` prevents that import from constructing the
I2C bus and GPIO devices while BLE is reserving its heap.
"""

WIFI_CONNECT_TIMEOUT_S = 20
WIFI_POLL_INTERVAL_S = 0.5
WIFI_MONITOR_INTERVAL_S = 1
WIFI_RECONNECT_BACKOFF_S = (1, 2, 4, 8, 16, 30)
