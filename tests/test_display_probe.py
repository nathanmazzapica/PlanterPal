import asyncio
import unittest

from display.probe import LCDPresenceProbe


class FakeBus:
    def __init__(self, lock, result=None, error=None):
        self.lock = lock
        self.result = [] if result is None else result
        self.error = error
        self.scan_calls = 0

    def scan(self):
        if not self.lock.locked():
            raise AssertionError("I2C scan occurred outside the shared lock")
        self.scan_calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class LCDPresenceProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_only_the_configured_address(self):
        for addresses, expected in (([0x23, 0x27], True), ([0x23], False)):
            with self.subTest(addresses=addresses):
                lock = asyncio.Lock()
                bus = FakeBus(lock, result=addresses)
                probe = LCDPresenceProbe(bus, lock, 0x27)

                self.assertIs(await probe.is_present(), expected)
                self.assertEqual(bus.scan_calls, 1)
                self.assertFalse(lock.locked())

    async def test_scan_oserror_propagates_and_releases_lock(self):
        lock = asyncio.Lock()
        failure = OSError(116)
        bus = FakeBus(lock, error=failure)
        probe = LCDPresenceProbe(bus, lock, 0x27)

        with self.assertRaises(OSError) as raised:
            await probe.is_present()

        self.assertIs(raised.exception, failure)
        self.assertEqual(bus.scan_calls, 1)
        self.assertFalse(lock.locked())

    async def test_cancellation_while_waiting_does_not_touch_bus(self):
        lock = asyncio.Lock()
        await lock.acquire()
        bus = FakeBus(lock, result=[0x27])
        probe = LCDPresenceProbe(bus, lock, 0x27)
        task = asyncio.create_task(probe.is_present())
        await asyncio.sleep(0)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        lock.release()

        self.assertEqual(bus.scan_calls, 0)


if __name__ == "__main__":
    unittest.main()
