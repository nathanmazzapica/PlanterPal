import asyncio
import inspect
import unittest

from tests.async_test_fakes import (
    FakeI2C,
    GateLock,
    SleepBarrier,
    bounded_wait,
    install_machine_stub,
    patched_driver_sleep,
)


install_machine_stub()

import lib.bh1750 as bh1750_module


class BH1750AsyncContractTests(unittest.IsolatedAsyncioTestCase):
    def make_driver(self, i2c, lock):
        self.assertTrue(
            inspect.iscoroutinefunction(bh1750_module.BH1750.lux),
            "BH1750.lux must be an async operation",
        )
        return bh1750_module.BH1750(i2c, lock)

    async def cancel_task(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await bounded_wait(task, "cancelled BH1750 task did not finish")

    async def assert_lock_reusable(self, lock):
        async def acquire_and_release():
            async with lock:
                self.assertTrue(lock.locked())

        await bounded_wait(acquire_and_release(), "I2C lock was not reusable")
        self.assertFalse(lock.locked())

    async def test_write_and_read_are_locked_separately_and_conversion_yields_bus(self):
        lock = GateLock(acquisition_count=3, initially_open=(0, 1, 2))
        i2c = FakeI2C(data=b"\x01\x20", lock=lock)
        driver = self.make_driver(i2c, lock)
        conversion = SleepBarrier()

        with patched_driver_sleep(bh1750_module, conversion):
            reading = asyncio.create_task(driver.lux())
            await bounded_wait(
                conversion.entered.wait(),
                "BH1750 never reached the conversion delay",
            )

            self.assertEqual(i2c.operations, [("write", 0x23, b"\x20", True)])
            self.assertFalse(lock.locked(), "conversion delay must not hold the I2C lock")

            other_entered = asyncio.Event()
            other_release = asyncio.Event()

            async def other_bus_user():
                async with lock:
                    other_entered.set()
                    await bounded_wait(
                        other_release.wait(),
                        "test did not release the competing bus user",
                    )

            other = asyncio.create_task(other_bus_user())
            await bounded_wait(
                other_entered.wait(),
                "another bus user could not acquire the lock during conversion",
            )
            self.assertTrue(lock.locked())
            other_release.set()
            await bounded_wait(other, "other bus user did not release the I2C lock")

            conversion.release.set()
            lux = await bounded_wait(reading, "BH1750 reading did not complete")

        self.assertEqual(conversion.delays, [0.5])
        self.assertEqual(
            i2c.operations,
            [
                ("write", 0x23, b"\x20", True),
                ("read", 0x23, 2, True),
            ],
        )
        self.assertEqual(lux, 0x0120 / 1.2)

    async def test_cancellation_while_waiting_for_command_lock_does_no_io(self):
        lock = GateLock(acquisition_count=2, initially_open=(1,))
        i2c = FakeI2C(lock=lock)
        driver = self.make_driver(i2c, lock)
        conversion = SleepBarrier()

        with patched_driver_sleep(bh1750_module, conversion):
            task = asyncio.create_task(driver.lux())
            await bounded_wait(
                lock.requested[0].wait(),
                "BH1750 never requested the command lock",
            )
            await self.cancel_task(task)

        self.assertEqual(i2c.operations, [])
        self.assertFalse(conversion.entered.is_set())
        await self.assert_lock_reusable(lock)

    async def test_cancellation_during_conversion_does_not_read(self):
        lock = GateLock(acquisition_count=2, initially_open=(0, 1))
        i2c = FakeI2C(lock=lock)
        driver = self.make_driver(i2c, lock)
        conversion = SleepBarrier()

        with patched_driver_sleep(bh1750_module, conversion):
            task = asyncio.create_task(driver.lux())
            await bounded_wait(
                conversion.entered.wait(),
                "BH1750 never reached the conversion delay",
            )
            await self.cancel_task(task)

        self.assertEqual(i2c.operations, [("write", 0x23, b"\x20", True)])
        await self.assert_lock_reusable(lock)

    async def test_cancellation_while_waiting_for_read_lock_does_not_read(self):
        lock = GateLock(acquisition_count=3, initially_open=(0, 2))
        i2c = FakeI2C(lock=lock)
        driver = self.make_driver(i2c, lock)
        conversion = SleepBarrier(initially_released=True)

        with patched_driver_sleep(bh1750_module, conversion):
            task = asyncio.create_task(driver.lux())
            await bounded_wait(
                lock.requested[1].wait(),
                "BH1750 never requested the read lock",
            )
            await self.cancel_task(task)

        self.assertEqual(i2c.operations, [("write", 0x23, b"\x20", True)])
        await self.assert_lock_reusable(lock)

    async def test_write_oserror_propagates_and_releases_lock(self):
        sentinel = OSError("write failed")
        lock = asyncio.Lock()
        i2c = FakeI2C(lock=lock, write_error=sentinel)
        driver = self.make_driver(i2c, lock)
        conversion = SleepBarrier(initially_released=True)

        with patched_driver_sleep(bh1750_module, conversion):
            with self.assertRaises(OSError) as raised:
                await bounded_wait(driver.lux(), "BH1750 write failure did not terminate")

        self.assertIs(raised.exception, sentinel)
        self.assertEqual(i2c.operations, [("write", 0x23, b"\x20", True)])
        self.assertFalse(conversion.entered.is_set())
        await self.assert_lock_reusable(lock)

    async def test_read_oserror_propagates_and_releases_lock(self):
        sentinel = OSError("read failed")
        lock = asyncio.Lock()
        i2c = FakeI2C(lock=lock, read_error=sentinel)
        driver = self.make_driver(i2c, lock)
        conversion = SleepBarrier(initially_released=True)

        with patched_driver_sleep(bh1750_module, conversion):
            with self.assertRaises(OSError) as raised:
                await bounded_wait(driver.lux(), "BH1750 read failure did not terminate")

        self.assertIs(raised.exception, sentinel)
        self.assertEqual(
            i2c.operations,
            [
                ("write", 0x23, b"\x20", True),
                ("read", 0x23, 2, True),
            ],
        )
        self.assertEqual(conversion.delays, [0.5])
        await self.assert_lock_reusable(lock)


if __name__ == "__main__":
    unittest.main()
