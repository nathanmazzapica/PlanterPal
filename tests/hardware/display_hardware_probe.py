"""Exercise the LCD and BH1750 through one audited physical I2C lock.

This probe imports the deployed display and sensor modules from the device.
Copy the changed modules before running it directly from the host:

    mpremote connect <port> fs cp display/display.py :display/display.py
    mpremote connect <port> fs cp lib/hd44780.py :lib/hd44780.py
    mpremote connect <port> fs cp lib/lcd.py :lib/lcd.py
    mpremote connect <port> run tests/hardware/display_hardware_probe.py

It temporarily replaces the LCD contents and takes one real lux reading.
"""

import asyncio

import config as cfg
from display.display import Display, LCD_ADDR
from lib.async_channel import SingleValueChannel
from lib.bh1750 import BH1750


class AuditedLock:
    """Track whether the one underlying asyncio lock guards each bus call."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self.held = False
        self.acquisitions = 0

    async def __aenter__(self):
        await self._lock.acquire()
        assert not self.held
        self.held = True
        self.acquisitions += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        assert self.held
        self.held = False
        self._lock.release()
        return False


class AuditedI2C:
    """Delegate to the physical bus only while the shared lock is held."""

    def __init__(self, bus, lock):
        self._bus = bus
        self._lock = lock
        self.operations = []

    def writeto(self, address, payload):
        assert self._lock.held, "I2C write occurred outside the shared lock"
        self.operations.append(("write", address))
        return self._bus.writeto(address, payload)

    def readfrom(self, address, length):
        assert self._lock.held, "I2C read occurred outside the shared lock"
        self.operations.append(("read", address))
        return self._bus.readfrom(address, length)


async def stop_display(display, task):
    print("[probe] cancelling Display")
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        print("[probe] PASS: display cancellation propagated")
    else:
        display.raise_if_failed()
        raise AssertionError("Display cancellation did not propagate")


async def assert_lock_reacquirable(lock):
    async with lock:
        assert lock.held

    assert not lock.held
    print("[probe] PASS: shared lock released and reacquirable")


async def main():
    shared_lock = AuditedLock()
    audited_bus = AuditedI2C(cfg.SENSOR_BUS, shared_lock)
    display_channel = SingleValueChannel()
    display = Display(audited_bus, shared_lock, display_channel)
    light_sensor = BH1750(audited_bus, shared_lock)

    assert display._bus_lock is shared_lock
    assert light_sensor.I2C_LOCK is shared_lock
    print("[probe] PASS: Display and BH1750 share the exact lock object")

    display_task = asyncio.create_task(display.run())

    try:
        await display.wait_until_ready()
        print("[probe] PASS: LCD initialized under the audited lock")

        await display.write_line("I2C lock probe", 0)
        await display.write_line("reading lux...", 1)

        sensor_task = asyncio.create_task(light_sensor.lux())
        await asyncio.sleep_ms(20)
        await display.write_line("shared lock OK", 0)
        lux = await sensor_task

        sensor_write = next(
            index
            for index, operation in enumerate(audited_bus.operations)
            if operation == ("write", light_sensor.ADDR)
        )
        sensor_read = next(
            index
            for index, operation in enumerate(audited_bus.operations)
            if operation == ("read", light_sensor.ADDR)
        )
        lcd_between = any(
            operation == ("write", LCD_ADDR)
            for operation in audited_bus.operations[sensor_write + 1:sensor_read]
        )

        assert sensor_write < sensor_read
        assert lcd_between, "LCD did not use the bus during BH1750 conversion"
        assert not shared_lock.held
        print("[probe] PASS: LCD ran during unlocked BH1750 conversion wait")
        print("[probe] lux=", lux, "bus_operations=", len(audited_bus.operations))

        await display.write_line("Probe PASS", 0)
        await display.write_line("Lux: {:.1f}".format(lux), 1)
    finally:
        await stop_display(display, display_task)
        await assert_lock_reacquirable(shared_lock)

    print("ALL DISPLAY HARDWARE TESTS PASSED")


asyncio.run(main())
