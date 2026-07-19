import asyncio
import inspect
import json
import types
import unittest

from tests.async_test_fakes import (
    AwaitableFloat,
    DeferredResult,
    FakeI2C,
    SequenceClock,
    call_maybe_async,
    install_machine_stub,
    patched_driver_sleep,
)


install_machine_stub()

import app.state as state_module
import lib.bh1750 as bh1750_module
import sensors.light as light_module


def make_driver(i2c, address=None):
    parameters = list(inspect.signature(bh1750_module.BH1750).parameters.values())
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    args = [i2c]
    kwargs = {}
    if len(positional) >= 3:
        args.append(asyncio.Lock())
    if address is not None:
        kwargs["addr"] = address
    return bh1750_module.BH1750(*args, **kwargs)


async def read_lux(driver):
    async def cooperative_noop(delay):
        return None

    with patched_driver_sleep(
        bh1750_module,
        cooperative_noop,
        synchronous_replacement=lambda delay: None,
    ):
        return await call_maybe_async(driver.lux)


class SequenceLuxSensor:
    def __init__(self, values):
        self._values = iter(values)

    def lux(self):
        return AwaitableFloat(next(self._values))


class CharacterizedLightMonitor:
    def __init__(self):
        self.lux_seconds = 3600
        self.current_lux = 125
        self.dli = 3600 / 54_000_000

    def update(self):
        requested = asyncio.Event()
        release = asyncio.Event()
        release.set()
        return DeferredResult(requested, release)


class CharacterizedMoistureMonitor:
    def __init__(self, moisture_percent=55):
        self.moisture_percent = moisture_percent

    def update(self):
        return None


class BH1750CharacterizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_address_command_read_size_and_conversion(self):
        i2c = FakeI2C(data=b"\x01\x20")
        driver = make_driver(i2c)

        lux = await read_lux(driver)

        self.assertEqual(
            i2c.operations,
            [
                ("write", 0x23, b"\x20", None),
                ("read", 0x23, 2, None),
            ],
        )
        self.assertEqual(lux, 0x0120 / 1.2)

    async def test_custom_address_is_used_for_command_and_read(self):
        i2c = FakeI2C(data=b"\x00\x0c")
        driver = make_driver(i2c, address=0x5C)

        lux = await read_lux(driver)

        self.assertEqual(
            i2c.operations,
            [
                ("write", 0x5C, b"\x20", None),
                ("read", 0x5C, 2, None),
            ],
        )
        self.assertEqual(lux, 12 / 1.2)


class LightMonitorCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    async def _update(self, monitor):
        await call_maybe_async(monitor.update)

    async def test_first_sample_sets_current_lux_without_integrating_area(self):
        sensor = SequenceLuxSensor([120])
        monitor = light_module.LightMonitor(sensor)
        original_time = light_module.time
        light_module.time = SequenceClock([1_000])
        try:
            await self._update(monitor)
        finally:
            light_module.time = original_time

        self.assertEqual(monitor.current_lux, 120)
        self.assertEqual(monitor.lux_seconds, 0)
        self.assertEqual(monitor.dli, 0)

    async def test_trapezoid_and_dli_divisor_are_preserved(self):
        sensor = SequenceLuxSensor([10, 30])
        monitor = light_module.LightMonitor(sensor)
        original_time = light_module.time
        light_module.time = SequenceClock([1_000, 3_000])
        try:
            await self._update(monitor)
            await self._update(monitor)
        finally:
            light_module.time = original_time

        self.assertEqual(monitor.current_lux, 30)
        self.assertEqual(monitor.lux_seconds, 40)
        self.assertEqual(monitor.dli, 40 / 54_000_000)

    async def test_tick_wrap_uses_ticks_diff_semantics(self):
        period = 1 << 30
        sensor = SequenceLuxSensor([100, 200])
        monitor = light_module.LightMonitor(sensor)
        original_time = light_module.time
        light_module.time = SequenceClock([period - 100, 100], period=period)
        try:
            await self._update(monitor)
            await self._update(monitor)
        finally:
            light_module.time = original_time

        self.assertEqual(monitor.lux_seconds, 0.2 * (100 + 200) / 2)


class StateWireCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_decoded_wire_keys_and_values_are_preserved(self):
        light = CharacterizedLightMonitor()
        moisture = CharacterizedMoistureMonitor()
        state = state_module.State(
            light,
            moisture,
            config={"min_moisture": 20, "max_moisture": 80},
        )

        await call_maybe_async(state.update)
        payload = json.loads(state.to_json())

        self.assertEqual(
            payload,
            {
                "current_lux": 125,
                "lux_seconds": 3600,
                "dli": 3600 / 54_000_000,
                "moisture": 55,
                "plant_status": "hydrated :)",
                "health_score": 0,
            },
        )

    async def test_moisture_threshold_boundaries_remain_strict(self):
        cases = (
            (19, "THIRSTY"),
            (20, "hydrated :)"),
            (80, "hydrated :)"),
            (81, "DROWNING"),
        )
        for moisture_value, expected_status in cases:
            with self.subTest(moisture=moisture_value):
                state = state_module.State(
                    CharacterizedLightMonitor(),
                    CharacterizedMoistureMonitor(moisture_value),
                    config={"min_moisture": 20, "max_moisture": 80},
                )
                await call_maybe_async(state.update)
                self.assertEqual(state.moisture, moisture_value)
                self.assertEqual(state.plant_status, expected_status)


if __name__ == "__main__":
    unittest.main()
