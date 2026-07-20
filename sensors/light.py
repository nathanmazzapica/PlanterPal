from lib.bh1750 import BH1750
import time

class LightMonitor():
    def __init__(self, bh1750: BH1750):
        self._sensor = bh1750
        self._previous = None

        self.lux_seconds = 0 # rolling total of lux seconds over uptime
        self.current_lux = 0 # Current instantaneous lux seen by the sensor
        self.dli = 0


    async def _measure(self):
        """
           Gets the current lux and returns the point
           (time_ms, lux)
        """
        lux = await self._sensor.lux()
        return (time.ticks_ms(), lux)

    def _estimate_dli(self):
        return self.lux_seconds / (54 * 1_000_000)

    def _trapezoid_area(self, a, b):
        t1, h1 = a
        t2, h2 = b

        interval_s = time.ticks_diff(t2, t1) / 1000

        return interval_s * (h1 + h2) / 2

    async def update(self):
        data = await self._measure()
        lux_seconds = self.lux_seconds

        if self._previous:
            lux_seconds += self._trapezoid_area(self._previous, data)

        dli = lux_seconds / (54 * 1_000_000)

        self.current_lux = data[1]
        self.lux_seconds = lux_seconds
        self._previous = data
        self.dli = dli
