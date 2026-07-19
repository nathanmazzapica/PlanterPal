import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path

from lib.async_channel import SingleValueChannel


def import_display_module():
    module_names = (
        "machine",
        "lib.pcf8574",
        "lib.hd44780",
        "lib.lcd",
        "display.display",
    )
    old_modules = {name: sys.modules.get(name) for name in module_names}

    machine = types.ModuleType("machine")
    machine.I2C = object
    sys.modules["machine"] = machine

    for module_name, class_name in (
        ("lib.pcf8574", "PCF8574"),
        ("lib.hd44780", "HD44780"),
        ("lib.lcd", "LCD"),
    ):
        module = types.ModuleType(module_name)
        setattr(module, class_name, object)
        sys.modules[module_name] = module

    sys.modules.pop("display.display", None)
    try:
        return importlib.import_module("display.display")
    finally:
        for name, previous in old_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


display_module = import_display_module()


def import_real_lcd_stack():
    lib_root = Path(__file__).resolve().parents[1] / "lib"
    module_names = (
        "machine",
        "utime",
        "hd44780_4bit_payload",
        "hd44780_4bit_driver",
        "backlight_driver",
        "pcf8574",
        "hd44780",
        "lcd",
    )
    old_modules = {name: sys.modules.get(name) for name in module_names}
    old_path = list(sys.path)

    machine = types.ModuleType("machine")
    machine.I2C = object
    utime = types.ModuleType("utime")
    utime.sleep_ms = lambda delay: None
    utime.sleep_us = lambda delay: None
    sys.modules["machine"] = machine
    sys.modules["utime"] = utime
    sys.path.insert(0, str(lib_root))

    for name in module_names[2:]:
        sys.modules.pop(name, None)

    try:
        pcf_module = importlib.import_module("pcf8574")
        hd_module = importlib.import_module("hd44780")
        lcd_module = importlib.import_module("lcd")

        async def cooperative_sleep_ms(delay):
            await asyncio.sleep(0)

        hd_module.asyncio = types.SimpleNamespace(sleep_ms=cooperative_sleep_ms)
        return types.SimpleNamespace(
            PCF8574=pcf_module.PCF8574,
            HD44780=hd_module.HD44780,
            LCD=lcd_module.LCD,
        )
    finally:
        sys.path[:] = old_path
        for name, previous in old_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


real_lcd_stack = import_real_lcd_stack()


class RecordingLock:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.owner = None
        self.acquisitions = 0

    async def __aenter__(self):
        await self._lock.acquire()
        self.owner = asyncio.current_task()
        self.acquisitions += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.owner = None
        self._lock.release()
        return False

    def locked(self):
        return self._lock.locked()


class RecordingChannel(SingleValueChannel):
    def __init__(self):
        super().__init__()
        self.put_values = []

    async def put(self, value):
        self.put_values.append(value)
        await super().put(value)


class RecordingI2C:
    def __init__(self, lock):
        self.lock = lock
        self.writes = []

    def writeto(self, address, payload):
        self.writes.append(
            (address, payload, self.lock.locked(), asyncio.current_task())
        )


class DisplayFixture:
    def __init__(self):
        fixture = self
        self.bus = object()
        self.lock = RecordingLock()
        self.channel = RecordingChannel()
        self.operations = []
        self.write_calls = []
        self.block_predicate = None
        self.write_started = asyncio.Event()
        self.release_write = asyncio.Event()
        self.write_error = None
        self.block_init = False
        self.init_started = asyncio.Event()
        self.release_init = asyncio.Event()
        self.sleep_calls = []
        self.sleep_started = asyncio.Event()
        self.release_sleep = asyncio.Event()
        self.block_sleep = False

        class FakePCF:
            def __init__(self, bus, address):
                self.bus = bus
                self.address = address
                fixture.operations.append(
                    ("pcf", fixture.lock.locked(), asyncio.current_task())
                )

        class FakeHD:
            def __init__(self, pcf, num_lines, num_columns):
                self.pcf = pcf
                self.num_lines = num_lines
                self.num_columns = num_columns

            async def initialize(self):
                fixture.operations.append(
                    ("initialize", fixture.lock.locked(), asyncio.current_task())
                )
                fixture.init_started.set()
                if fixture.block_init:
                    await fixture.release_init.wait()
                await asyncio.sleep(0)

        class FakeLCD:
            def __init__(self, hd, pcf):
                self.hd44780 = hd
                self.pcf = pcf

            def backlight_on(self):
                fixture.operations.append(
                    ("backlight", fixture.lock.locked(), asyncio.current_task())
                )

            async def write_line(self, text, line=0):
                fixture.write_calls.append(
                    (
                        text,
                        line,
                        fixture.lock.locked(),
                        asyncio.current_task(),
                        fixture.lock.acquisitions,
                    )
                )
                if fixture.write_error is not None:
                    raise fixture.write_error
                if (
                    fixture.block_predicate is not None
                    and fixture.block_predicate(text, line)
                ):
                    fixture.block_predicate = None
                    fixture.write_started.set()
                    await fixture.release_write.wait()
                await asyncio.sleep(0)

        async def fake_sleep(delay):
            fixture.sleep_calls.append((delay, fixture.lock.locked()))
            fixture.sleep_started.set()
            if fixture.block_sleep:
                await fixture.release_sleep.wait()
            else:
                await asyncio.sleep(0)

        self.display = display_module.Display(
            self.bus,
            self.lock,
            self.channel,
            pcf_type=FakePCF,
            hd_type=FakeHD,
            lcd_type=FakeLCD,
            sleep=fake_sleep,
        )


async def wait_until(predicate, message):
    async def poll():
        while not predicate():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(poll(), timeout=0.25)
    except asyncio.TimeoutError as error:
        raise AssertionError(message) from error


class DisplayAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def start_display(self, fixture):
        task = asyncio.create_task(fixture.display.run())
        await asyncio.wait_for(
            fixture.display.wait_until_ready(),
            timeout=0.25,
        )
        return task

    async def cancel(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_constructor_performs_no_hardware_work(self):
        fixture = DisplayFixture()

        self.assertEqual(fixture.operations, [])
        self.assertEqual(fixture.write_calls, [])

    async def test_lux_formatting_boundaries_are_preserved(self):
        display = DisplayFixture().display

        self.assertEqual(
            [
                display._format_lux(value)
                for value in (999, 1_000, 99_999, 100_000, 999_999, 1_000_000)
            ],
            ["999", "1.0K", "100.0K", "100K", "1000K", "1.0M"],
        )

    async def test_readiness_waits_for_locked_initialization(self):
        fixture = DisplayFixture()
        fixture.block_init = True
        task = asyncio.create_task(fixture.display.run())
        ready = asyncio.create_task(fixture.display.wait_until_ready())
        await asyncio.wait_for(fixture.init_started.wait(), timeout=0.25)

        self.assertFalse(ready.done())
        self.assertTrue(fixture.lock.locked())
        fixture.release_init.set()
        await asyncio.wait_for(ready, timeout=0.25)

        await self.cancel(task)

    async def test_cancellation_during_initialization_releases_bus_and_waiter(self):
        fixture = DisplayFixture()
        fixture.block_init = True
        task = asyncio.create_task(fixture.display.run())
        ready = asyncio.create_task(fixture.display.wait_until_ready())
        await asyncio.wait_for(fixture.init_started.wait(), timeout=0.25)

        await self.cancel(task)
        with self.assertRaisesRegex(RuntimeError, "initialization"):
            await asyncio.wait_for(ready, timeout=0.25)
        async with fixture.lock:
            self.assertTrue(fixture.lock.locked())

    async def test_initialization_and_lcd_mutation_use_one_owner_and_bus_lock(self):
        fixture = DisplayFixture()
        task = await self.start_display(fixture)

        await fixture.display.write_line("ready", 0)
        await fixture.display.render(10, 50, 0.25)
        await wait_until(
            lambda: len(fixture.write_calls) == 3,
            "render frame was not completed",
        )

        owner_tasks = {operation[2] for operation in fixture.operations}
        owner_tasks.update(call[3] for call in fixture.write_calls)
        self.assertEqual(owner_tasks, {task})
        self.assertTrue(all(operation[1] for operation in fixture.operations))
        self.assertTrue(all(call[2] for call in fixture.write_calls))
        await self.cancel(task)

    async def test_real_lcd_stack_never_writes_i2c_outside_shared_lock(self):
        lock = RecordingLock()
        bus = RecordingI2C(lock)
        channel = SingleValueChannel()

        async def cooperative_sleep(delay):
            await asyncio.sleep(0)

        display = display_module.Display(
            bus,
            lock,
            channel,
            pcf_type=real_lcd_stack.PCF8574,
            hd_type=real_lcd_stack.HD44780,
            lcd_type=real_lcd_stack.LCD,
            sleep=cooperative_sleep,
        )
        task = asyncio.create_task(display.run())
        await asyncio.wait_for(display.wait_until_ready(), timeout=0.25)
        await display.write_line("ready", 0)
        await display.render(10, 50, 0.25)
        await wait_until(
            lambda: len(bus.writes) >= 233,
            "real LCD stack did not finish the reading frame",
        )

        self.assertTrue(bus.writes)
        self.assertTrue(all(write[2] for write in bus.writes))
        self.assertEqual({write[3] for write in bus.writes}, {task})
        await self.cancel(task)

    async def test_complete_two_line_frame_is_one_bus_critical_section(self):
        fixture = DisplayFixture()
        task = await self.start_display(fixture)

        await fixture.display.render(10, 50, 0.25)
        await wait_until(
            lambda: len(fixture.write_calls) == 2,
            "render frame was not completed",
        )

        self.assertEqual(fixture.write_calls[0][4], fixture.write_calls[1][4])
        self.assertEqual(fixture.write_calls[0][1:3], (0, True))
        self.assertEqual(fixture.write_calls[1][1:3], (1, True))
        self.assertEqual(fixture.write_calls[0][0], "Lux:10s|M:50%")
        self.assertEqual(fixture.write_calls[1][0], "DLI:0.25")
        await self.cancel(task)

    async def test_competing_i2c_user_cannot_enter_between_frame_lines(self):
        fixture = DisplayFixture()
        fixture.block_predicate = lambda text, line: line == 0
        task = await self.start_display(fixture)
        await fixture.display.render(10, 50, 0.25)
        await asyncio.wait_for(fixture.write_started.wait(), timeout=0.25)
        sensor_acquired = asyncio.Event()

        async def sensor_transaction():
            async with fixture.lock:
                sensor_acquired.set()

        sensor = asyncio.create_task(sensor_transaction())
        await asyncio.sleep(0)
        self.assertFalse(sensor_acquired.is_set())

        fixture.release_write.set()
        await asyncio.wait_for(sensor_acquired.wait(), timeout=0.25)
        self.assertEqual(
            [call[1] for call in fixture.write_calls[:2]],
            [0, 1],
        )
        await sensor
        await self.cancel(task)

    async def test_idle_display_does_not_hold_shared_bus_lock(self):
        fixture = DisplayFixture()
        task = await self.start_display(fixture)

        async def acquire_once():
            async with fixture.lock:
                return True

        self.assertTrue(
            await asyncio.wait_for(acquire_once(), timeout=0.25)
        )
        await self.cancel(task)

    async def test_latest_pending_render_replaces_stale_frame(self):
        fixture = DisplayFixture()
        fixture.block_predicate = lambda text, line: text.startswith("Lux:1")
        task = await self.start_display(fixture)

        await fixture.display.render(1, 1, 1)
        await asyncio.wait_for(fixture.write_started.wait(), timeout=0.25)
        await fixture.display.render(2, 2, 2)
        await fixture.display.render(3, 3, 3)
        fixture.release_write.set()
        await wait_until(
            lambda: any(call[0] == "DLI:3" for call in fixture.write_calls),
            "latest display frame was not rendered",
        )

        rendered_text = [call[0] for call in fixture.write_calls]
        self.assertFalse(any(text.startswith("Lux:2") for text in rendered_text))
        self.assertNotIn("DLI:2", rendered_text)
        self.assertTrue(any(text.startswith("Lux:3") for text in rendered_text))
        self.assertIn("DLI:3", rendered_text)
        await self.cancel(task)

    async def test_pending_critical_message_cannot_be_replaced_by_render(self):
        fixture = DisplayFixture()
        fixture.block_predicate = lambda text, line: text.startswith("Lux:1")
        task = await self.start_display(fixture)

        await fixture.display.render(1, 1, 1)
        await asyncio.wait_for(fixture.write_started.wait(), timeout=0.25)
        critical = asyncio.create_task(fixture.display.write_line("critical", 0))
        await wait_until(
            lambda: any(command.kind == "line" for command in fixture.channel.put_values),
            "critical command was not submitted",
        )
        await fixture.display.render(99, 99, 99)
        fixture.release_write.set()
        await asyncio.wait_for(critical, timeout=0.25)

        rendered_text = [call[0] for call in fixture.write_calls]
        self.assertIn("critical", rendered_text)
        self.assertFalse(any(text.startswith("Lux:99") for text in rendered_text))
        await self.cancel(task)

    async def test_concurrent_critical_messages_remain_ordered(self):
        fixture = DisplayFixture()
        fixture.block_predicate = lambda text, line: text == "first"
        task = await self.start_display(fixture)

        first = asyncio.create_task(fixture.display.write_line("first", 0))
        await asyncio.wait_for(fixture.write_started.wait(), timeout=0.25)
        second = asyncio.create_task(fixture.display.write_line("second", 0))
        await asyncio.sleep(0)

        self.assertNotIn("second", [call[0] for call in fixture.write_calls])
        fixture.release_write.set()
        await asyncio.wait_for(first, timeout=0.25)
        await asyncio.wait_for(second, timeout=0.25)

        critical_text = [
            call[0]
            for call in fixture.write_calls
            if call[0] in ("first", "second")
        ]
        self.assertEqual(critical_text, ["first", "second"])
        await self.cancel(task)

    async def test_marquee_releases_bus_during_inter_frame_delay(self):
        fixture = DisplayFixture()
        fixture.block_sleep = True
        task = await self.start_display(fixture)
        error_message = asyncio.create_task(
            fixture.display.display_err("oops", 7)
        )
        await asyncio.wait_for(fixture.sleep_started.wait(), timeout=0.25)

        self.assertEqual(fixture.sleep_calls[0], (0.2, False))
        async with fixture.lock:
            self.assertTrue(fixture.lock.locked())

        fixture.release_sleep.set()
        await asyncio.wait_for(error_message, timeout=0.25)
        line_one_calls = [call for call in fixture.write_calls if call[1] == 1]
        self.assertEqual(len(line_one_calls), len("oops") + 17)
        self.assertEqual(len(fixture.sleep_calls), len("oops") + 17)
        await self.cancel(task)

    async def test_cancellation_during_marquee_delay_stops_future_frames(self):
        fixture = DisplayFixture()
        fixture.block_sleep = True
        task = await self.start_display(fixture)
        error_message = asyncio.create_task(
            fixture.display.display_err("oops", 7)
        )
        await asyncio.wait_for(fixture.sleep_started.wait(), timeout=0.25)
        calls_before_cancel = list(fixture.write_calls)

        await self.cancel(task)
        with self.assertRaisesRegex(RuntimeError, "Display stopped"):
            await asyncio.wait_for(error_message, timeout=0.25)
        await asyncio.sleep(0)

        self.assertEqual(fixture.write_calls, calls_before_cancel)
        self.assertFalse(fixture.lock.locked())

    async def test_cancellation_releases_bus_and_wakes_critical_waiter(self):
        fixture = DisplayFixture()
        fixture.block_predicate = lambda text, line: text == "critical"
        task = await self.start_display(fixture)
        critical = asyncio.create_task(fixture.display.write_line("critical", 0))
        await asyncio.wait_for(fixture.write_started.wait(), timeout=0.25)

        await self.cancel(task)
        with self.assertRaisesRegex(RuntimeError, "Display stopped"):
            await asyncio.wait_for(critical, timeout=0.25)
        async with fixture.lock:
            self.assertTrue(fixture.lock.locked())

    async def test_cancellation_wakes_active_and_queued_critical_callers(self):
        fixture = DisplayFixture()
        fixture.block_predicate = lambda text, line: text == "first"
        task = await self.start_display(fixture)
        first = asyncio.create_task(fixture.display.write_line("first", 0))
        await asyncio.wait_for(fixture.write_started.wait(), timeout=0.25)
        second = asyncio.create_task(fixture.display.write_line("second", 0))
        await asyncio.sleep(0)

        await self.cancel(task)

        for caller in (first, second):
            with self.assertRaisesRegex(RuntimeError, "Display"):
                await asyncio.wait_for(caller, timeout=0.25)
        async with fixture.lock:
            self.assertTrue(fixture.lock.locked())

    async def test_new_submissions_after_stop_fail_immediately(self):
        fixture = DisplayFixture()
        task = await self.start_display(fixture)
        await self.cancel(task)

        with self.assertRaisesRegex(RuntimeError, "not ready"):
            await asyncio.wait_for(
                fixture.display.write_line("late", 0),
                timeout=0.25,
            )
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            await fixture.display.render(1, 2, 3)

    async def test_cancellation_while_waiting_for_bus_does_no_lcd_work(self):
        fixture = DisplayFixture()
        task = await self.start_display(fixture)

        async with fixture.lock:
            await fixture.display.render(1, 2, 3)
            await asyncio.sleep(0)
            await self.cancel(task)
            self.assertEqual(fixture.write_calls, [])

        async with fixture.lock:
            self.assertTrue(fixture.lock.locked())

    async def test_idle_cancellation_is_not_recorded_as_failure(self):
        fixture = DisplayFixture()
        task = await self.start_display(fixture)

        await self.cancel(task)

        self.assertIsNone(fixture.display.failure)
        async with fixture.lock:
            self.assertTrue(fixture.lock.locked())

    async def test_second_concurrent_owner_is_rejected(self):
        fixture = DisplayFixture()
        task = await self.start_display(fixture)

        second = asyncio.create_task(fixture.display.run())
        with self.assertRaisesRegex(RuntimeError, "already running"):
            await asyncio.wait_for(second, timeout=0.25)

        await self.cancel(task)

    async def test_unexpected_lcd_failure_is_visible_and_releases_lock(self):
        fixture = DisplayFixture()
        failure = ValueError("lcd fault")
        fixture.write_error = failure
        task = await self.start_display(fixture)

        await fixture.display.render(1, 2, 3)
        await asyncio.wait_for(task, timeout=0.25)

        self.assertIs(fixture.display.failure, failure)
        with self.assertRaisesRegex(ValueError, "lcd fault"):
            fixture.display.raise_if_failed()
        async with fixture.lock:
            self.assertTrue(fixture.lock.locked())

    async def test_critical_lcd_failure_reaches_waiting_caller(self):
        fixture = DisplayFixture()
        failure = ValueError("lcd fault")
        fixture.write_error = failure
        task = await self.start_display(fixture)

        with self.assertRaisesRegex(ValueError, "lcd fault"):
            await asyncio.wait_for(
                fixture.display.write_line("critical", 0),
                timeout=0.25,
            )
        await asyncio.wait_for(task, timeout=0.25)


if __name__ == "__main__":
    unittest.main()
