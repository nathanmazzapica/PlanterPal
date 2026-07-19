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
        self.current_lux = 0
        self.dli = 0
        self.moisture = 0

        self.plant_status = ""
        self.health_score = 0

    async def update(self):
        await self.LIGHT_MONITOR.update()
        self.MOISTURE_MONITOR.update()

        lux_seconds = self.LIGHT_MONITOR.lux_seconds
        current_lux = self.LIGHT_MONITOR.current_lux
        dli = self.LIGHT_MONITOR.dli
        moisture = self.MOISTURE_MONITOR.moisture_percent

        if moisture < self.CONFIG["min_moisture"]:
            plant_status = "THIRSTY"
        elif moisture > self.CONFIG["max_moisture"]:
            plant_status = "DROWNING"
        else:
            plant_status = "hydrated :)"

        self.lux_seconds = lux_seconds
        self.current_lux = current_lux
        self.dli = dli
        self.moisture = moisture
        self.plant_status = plant_status

    def to_dict(self):
        return {
            "current_lux": self.current_lux,
            "lux_seconds": self.lux_seconds,
            "dli": self.dli,
            "moisture": self.moisture,
            "plant_status": self.plant_status,
            "health_score": self.health_score,
        }

    def to_json(self):
        return json.dumps(self.to_dict())
