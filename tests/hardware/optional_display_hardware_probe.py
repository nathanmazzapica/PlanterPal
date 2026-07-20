"""Exercise Application's once-per-boot optional LCD selection.

Deploy the changed running-mode modules before running this probe:

    mpremote connect <port> fs cp display/null_display.py :display/null_display.py
    mpremote connect <port> fs cp display/probe.py :display/probe.py
    mpremote connect <port> fs cp app/application.py :app/application.py
    mpremote connect <port> run tests/hardware/optional_display_hardware_probe.py

Run it once with the LCD connected. To exercise headless selection, power the
board off, disconnect only the LCD, power it back on, and run the same command.
Reattach I2C devices only while the board is powered off. Live hot-plugging is
not tested or supported. The probe changes LCD contents when one is selected
and always takes one real BH1750 reading.
"""

import asyncio
import sys

import device_hardware as hardware
from app.application import Application
from display.display import Display, LCD_ADDR
from display.null_display import NullDisplay
from display.probe import LCDPresenceProbe
from lib.async_channel import SingleValueChannel
from lib.bh1750 import BH1750


class RecordingProbe:
    def __init__(self, probe):
        self._probe = probe
        self.result = None
        self.calls = 0

    async def is_present(self):
        self.calls += 1
        self.result = await self._probe.is_present()
        return self.result


async def main():
    shared_lock = asyncio.Lock()
    display = Display(
        hardware.SENSOR_BUS,
        shared_lock,
        SingleValueChannel(),
    )
    probe = RecordingProbe(
        LCDPresenceProbe(hardware.SENSOR_BUS, shared_lock, LCD_ADDR)
    )
    sensor = BH1750(hardware.SENSOR_BUS, shared_lock)
    application = Application(
        None,
        display,
        None,
        None,
        None,
        probe,
        NullDisplay,
    )

    assert display._bus_lock is shared_lock
    assert probe._probe._bus_lock is shared_lock
    assert sensor.I2C_LOCK is shared_lock
    print("[optional-display] PASS all I2C owners share the exact lock")

    try:
        await application._start_display()
        assert probe.calls == 1

        if application.display is display:
            assert probe.result is True
            print("[optional-display] PASS real Display selected")
            await application.display.write_line("Optional LCD", 0)
            await application.display.write_line("reading lux...", 1)
        else:
            assert isinstance(application.display, NullDisplay)
            print("[optional-display] PASS NullDisplay selected")
            await application.display.write_line("ignored headless", 0)

        lux = await sensor.lux()
        print("[optional-display] PASS BH1750 sampled; lux=", lux)

        if application.display is display:
            await application.display.write_line("Probe PASS", 0)
            await application.display.write_line("Lux: {:.1f}".format(lux), 1)
    finally:
        await application._stop_display()

    assert application._display_task is None
    print("[optional-display] PASS selected display task settled")
    print("ALL OPTIONAL DISPLAY HARDWARE TESTS PASSED")


print("Firmware implementation:", getattr(sys, "implementation", "unknown"))
print("Firmware platform:", getattr(sys, "platform", "unknown"))
asyncio.run(main())
