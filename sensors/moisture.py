from lib.ek1940 import EK1940

class MoistureMonitor():
    def __init__(self, ek1940: EK1940):
        self._sensor = ek1940
        self._moisture = 0
        # NOTE: These are currently hardcoded, but a calibration setup would be nice.
        # Review later.
        self.DRY = 40_000
        self.WET = 14_000
        self.moisture_percent = 0

    def _calculate_percent(self):
        # -100 to show moistage instead of dryness
        return max(100 - ((self._moisture - self.WET) * 100) / (self.DRY - self.WET), 0)

        
    
    def update(self):
        self._moisture = self._sensor.moisture()
        self.moisture_percent = self._calculate_percent()
