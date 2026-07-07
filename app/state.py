from sensors.moisture import MoistureMonitor
from sensors.light import LightMonitor
from config import TEST_PLANT
import json

class State():
    def __init__(self, light_monitor: LightMonitor, moisture_monitor: MoistureMonitor, config=TEST_PLANT):
        self.LIGHT_MONITOR = light_monitor
        self.MOISTURE_MONITOR = moisture_monitor
        self.CONFIG = config

        self.lux_seconds = 0
        self.dli = 0
        self.moisture = 0

        self.plant_status = ""
        self.health_score = 0

    def update(self):
        self.LIGHT_MONITOR.update()
        self.MOISTURE_MONITOR.update()
        self.lux_seconds = self.LIGHT_MONITOR.lux_seconds
        self.dli = self.LIGHT_MONITOR.dli
        self.moisture = self.MOISTURE_MONITOR.moisture_percent

        if self.moisture < self.CONFIG["min_moisture"]:
            self.plant_status = "THIRSTY"
        elif self.moisture > self.CONFIG["max_moisture"]:
            self.plant_status = "DROWNING"
        else:
            self.plant_status = "hydrated :)"

    def to_dict(self):
        return {
            "lux_seconds": self.lux_seconds,
            "dli": self.dli,
            "moisture": self.moisture,
            "plant_status": self.plant_status,
            "health_score": self.health_score,
        }

    def to_json(self):
        return json.dumps(self.to_dict())

