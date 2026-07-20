import asyncio
import inspect
import unittest

from tests.async_test_fakes import (
    DeferredLightMonitor,
    DeferredLuxSensor,
    RecordingClock,
    RecordingMoistureMonitor,
    SequenceClock,
    bounded_wait,
    install_machine_stub,
)


install_machine_stub()

import app.state as state_module
import sensors.light as light_module


def light_values(monitor):
    return (
        monitor._previous,
        monitor.lux_seconds,
        monitor.current_lux,
        monitor.dli,
    )


def state_values(state):
    return dict(state.to_dict())


class DeferredLightMonitorTests(unittest.IsolatedAsyncioTestCase):
    def make_monitor(self, sensor):
        monitor = light_module.LightMonitor(sensor)
        monitor._previous = (500, 8)
        monitor.lux_seconds = 123
        monitor.current_lux = 8
        monitor.dli = 123 / 54_000_000
        return monitor

    def start_update(self, monitor):
        operation = monitor.update()
        self.assertTrue(
            inspect.isawaitable(operation),
            "LightMonitor.update must return an awaitable operation",
        )
        return asyncio.ensure_future(operation)

    async def test_monitor_remains_unchanged_while_reading_is_pending(self):
        sensor = DeferredLuxSensor(value=30)
        monitor = self.make_monitor(sensor)
        before = light_values(monitor)
        original_time = light_module.time
        light_module.time = SequenceClock([1_500])
        try:
            task = self.start_update(monitor)
            await bounded_wait(sensor.requested.wait(), "light sensor was not awaited")
            self.assertEqual(light_values(monitor), before)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await bounded_wait(task, "cancelled LightMonitor update did not finish")
        finally:
            light_module.time = original_time

        self.assertEqual(light_values(monitor), before)

    async def test_monitor_remains_unchanged_when_reading_is_cancelled(self):
        sensor = DeferredLuxSensor(value=30)
        monitor = self.make_monitor(sensor)
        before = light_values(monitor)
        original_time = light_module.time
        light_module.time = SequenceClock([1_500])
        try:
            task = self.start_update(monitor)
            await bounded_wait(sensor.requested.wait(), "light sensor was not awaited")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await bounded_wait(task, "cancelled LightMonitor update did not finish")
        finally:
            light_module.time = original_time

        self.assertEqual(light_values(monitor), before)

    async def test_monitor_remains_unchanged_when_reading_fails(self):
        sentinel = OSError("sensor read failed")
        sensor = DeferredLuxSensor(value=30, error=sentinel)
        monitor = self.make_monitor(sensor)
        before = light_values(monitor)
        original_time = light_module.time
        light_module.time = SequenceClock([1_500])
        try:
            task = self.start_update(monitor)
            await bounded_wait(sensor.requested.wait(), "light sensor was not awaited")
            sensor.release.set()
            with self.assertRaises(OSError) as raised:
                await bounded_wait(task, "failed LightMonitor update did not finish")
        finally:
            light_module.time = original_time

        self.assertIs(raised.exception, sentinel)
        self.assertEqual(light_values(monitor), before)

    async def test_monitor_publishes_only_after_complete_reading(self):
        sensor = DeferredLuxSensor(value=30)
        monitor = self.make_monitor(sensor)
        original_time = light_module.time
        light_module.time = SequenceClock([1_500])
        try:
            task = self.start_update(monitor)
            await bounded_wait(sensor.requested.wait(), "light sensor was not awaited")
            sensor.release.set()
            await bounded_wait(task, "successful LightMonitor update did not finish")
        finally:
            light_module.time = original_time

        self.assertEqual(monitor._previous, (1_500, 30))
        self.assertEqual(monitor.current_lux, 30)
        self.assertEqual(monitor.lux_seconds, 142.0)
        self.assertEqual(monitor.dli, 142.0 / 54_000_000)

    async def test_timestamp_is_captured_only_after_lux_completes(self):
        sensor = DeferredLuxSensor(value=30)
        monitor = self.make_monitor(sensor)
        clock = RecordingClock(1_500)
        original_time = light_module.time
        light_module.time = clock
        try:
            task = self.start_update(monitor)
            await bounded_wait(sensor.requested.wait(), "light sensor was not awaited")
            self.assertEqual(clock.ticks_ms_calls, 0)
            sensor.release.set()
            await bounded_wait(task, "timestamp placement update did not finish")
        finally:
            light_module.time = original_time

        self.assertEqual(clock.ticks_ms_calls, 1)
        self.assertEqual(monitor._previous, (1_500, 30))


class DeferredStateTests(unittest.IsolatedAsyncioTestCase):
    def make_state(self, light, moisture):
        state = state_module.State(
            light,
            moisture,
            config={"min_moisture": 20, "max_moisture": 80},
        )
        state.lux_seconds = 10
        state.current_lux = 2
        state.dli = 10 / 54_000_000
        state.moisture = 45
        state.plant_status = "hydrated :)"
        state.health_score = 7
        return state

    def start_update(self, state):
        try:
            operation = state.update()
        except Exception as error:
            self.fail(f"State.update raised synchronously instead of returning an awaitable: {error!r}")
        self.assertTrue(
            inspect.isawaitable(operation),
            "State.update must return an awaitable operation",
        )
        return asyncio.ensure_future(operation)

    async def test_state_and_moisture_remain_unchanged_while_light_is_pending(self):
        light = DeferredLightMonitor(lux_seconds=200, current_lux=40, dli=0.25)
        moisture = RecordingMoistureMonitor(moisture_percent=55)
        state = self.make_state(light, moisture)
        before = state_values(state)

        task = self.start_update(state)
        await bounded_wait(light.requested.wait(), "State did not await light completion")

        self.assertEqual(state_values(state), before)
        self.assertEqual(moisture.update_calls, 0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await bounded_wait(task, "cancelled State update did not finish")
        self.assertEqual(state_values(state), before)

    async def test_state_remains_unchanged_when_light_fails(self):
        sentinel = OSError("light update failed")
        light = DeferredLightMonitor(error=sentinel)
        moisture = RecordingMoistureMonitor(moisture_percent=55)
        state = self.make_state(light, moisture)
        before = state_values(state)

        task = self.start_update(state)
        await bounded_wait(light.requested.wait(), "State did not await light completion")
        light.release.set()
        with self.assertRaises(OSError) as raised:
            await bounded_wait(task, "failed State update did not finish")

        self.assertIs(raised.exception, sentinel)
        self.assertEqual(state_values(state), before)
        self.assertEqual(moisture.update_calls, 0)

    async def test_light_completes_before_synchronous_moisture_update(self):
        order = []
        light = DeferredLightMonitor(
            lux_seconds=200,
            current_lux=40,
            dli=0.25,
            order=order,
        )
        moisture = RecordingMoistureMonitor(moisture_percent=55, order=order)
        state = self.make_state(light, moisture)

        task = self.start_update(state)
        await bounded_wait(light.requested.wait(), "State did not await light completion")
        self.assertEqual(order, [])
        light.release.set()
        await bounded_wait(task, "ordered State update did not finish")

        self.assertEqual(order, ["light_complete", "moisture"])

    async def test_successful_cycle_publishes_complete_aggregate(self):
        light = DeferredLightMonitor(lux_seconds=200, current_lux=40, dli=0.25)
        moisture = RecordingMoistureMonitor(moisture_percent=55)
        state = self.make_state(light, moisture)

        task = self.start_update(state)
        await bounded_wait(light.requested.wait(), "State did not await light completion")
        light.release.set()
        await bounded_wait(task, "successful State update did not finish")

        self.assertEqual(
            state.to_dict(),
            {
                "current_lux": 40,
                "lux_seconds": 200,
                "dli": 0.25,
                "moisture": 55,
                "plant_status": "hydrated :)",
                "health_score": 7,
            },
        )
        self.assertEqual(moisture.update_calls, 1)

    async def test_moisture_failure_does_not_partially_publish_aggregate(self):
        sentinel = OSError("moisture update failed")
        order = []
        light = DeferredLightMonitor(
            lux_seconds=200,
            current_lux=40,
            dli=0.25,
            order=order,
        )
        moisture = RecordingMoistureMonitor(
            moisture_percent=55,
            order=order,
            error=sentinel,
        )
        state = self.make_state(light, moisture)
        before = state_values(state)

        task = self.start_update(state)
        await bounded_wait(light.requested.wait(), "State did not await light completion")
        light.release.set()
        with self.assertRaises(OSError) as raised:
            await bounded_wait(task, "moisture failure did not terminate State update")

        self.assertIs(raised.exception, sentinel)
        self.assertEqual(order, ["light_complete", "moisture"])
        self.assertEqual(state_values(state), before)


if __name__ == "__main__":
    unittest.main()
