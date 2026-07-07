from lib.bh1750 import BH1750
import time

class LightMonitor():
    def __init__(self, bh1750: BH1750):
        self._sensor = bh1750
        self._previous = None

        self.lux_seconds = 0 # rolling total of lux seconds over uptime
        self.dli = 0


    def _measure(self):
        """
           Gets the current lux and returns the point
           (time_ms, lux)
        """
        lux = self._sensor.lux()
        return (time.ticks_ms(), lux)

    def _calculate_dli(self):
        return self.lux_seconds / (54 * 1_000_000)

    def _trapezoid_area(self, a, b):
        t1, h1 = a
        t2, h2 = b

        interval_s = time.ticks_diff(t2, t1) / 1000

        return interval_s * (h1 + h2) / 2

    def update(self):
        data = self._measure()

        if self._previous:
            self.lux_seconds += self._trapezoid_area(self._previous, data)

        self._previous = data
        self.dli = self._calculate_dli()

