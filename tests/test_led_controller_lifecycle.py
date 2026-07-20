import asyncio
import unittest

from led.controller import Controller


class AuditedPixel:
    def __init__(self):
        self.calls = []
        self.blink_started = asyncio.Event()
        self.blink_cancelled = asyncio.Event()

    def set_color(self, red, green, blue):
        self.calls.append(("set_color", red, green, blue))

    def on(self):
        self.calls.append(("on",))

    def off(self):
        self.calls.append(("off",))

    async def blink(self):
        self.calls.append(("blink-start",))
        self.blink_started.set()
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            self.calls.append(("blink-stop",))
            self.blink_cancelled.set()


class SlowCleanupPixel(AuditedPixel):
    def __init__(self):
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.release_cleanup = asyncio.Event()

    async def blink(self):
        self.calls.append(("blink-start",))
        self.blink_started.set()
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            self.cleanup_started.set()
            await self.release_cleanup.wait()
            self.calls.append(("blink-stop",))
            self.blink_cancelled.set()


class LedControllerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_writes_off_even_before_first_state(self):
        pixel = AuditedPixel()
        controller = Controller(pixel)

        await controller.stop()

        self.assertEqual(pixel.calls, [("off",)])
        self.assertIsNone(controller.state)

    async def test_connecting_fades_cyan_until_ready(self):
        pixel = AuditedPixel()
        controller = Controller(pixel)

        await controller.set_state("connecting")
        await asyncio.wait_for(pixel.blink_started.wait(), timeout=0.25)

        self.assertEqual(pixel.calls[0], ("set_color", 0, 25, 25))
        self.assertIn(("blink-start",), pixel.calls)

        await controller.set_state("ready")
        await asyncio.wait_for(pixel.blink_cancelled.wait(), timeout=0.25)

        self.assertEqual(controller.state, "ready")
        self.assertIn(("blink-stop",), pixel.calls)
        self.assertIn(("set_color", 0, 25, 0), pixel.calls)
        self.assertIn(("on",), pixel.calls)

    async def test_stop_owns_animation_cancellation_and_turns_pixel_off(self):
        pixel = AuditedPixel()
        controller = Controller(pixel)
        await controller.set_state("provisioning")
        await asyncio.wait_for(pixel.blink_started.wait(), timeout=0.25)

        await controller.stop()

        self.assertTrue(pixel.blink_cancelled.is_set())
        self.assertEqual(pixel.calls[-2:], [("blink-stop",), ("off",)])
        self.assertIsNone(controller.state)
        self.assertIsNone(
            controller._task,
            "Controller must not retain a stopped animation task",
        )

    async def test_stop_is_idempotent_and_controller_can_be_used_again(self):
        pixel = AuditedPixel()
        controller = Controller(pixel)
        await controller.set_state("provisioning")
        await asyncio.wait_for(pixel.blink_started.wait(), timeout=0.25)

        await controller.stop()
        await controller.stop()
        await controller.set_state("ready")

        self.assertEqual(pixel.calls.count(("blink-stop",)), 1)
        self.assertEqual(pixel.calls[-2:], [("set_color", 0, 25, 0), ("on",)])
        self.assertEqual(controller.state, "ready")

    async def test_reconnect_flapping_settles_every_replaced_animation(self):
        pixel = AuditedPixel()
        controller = Controller(pixel)

        for _ in range(3):
            await controller.set_state("connecting")
            for _ in range(10):
                if pixel.calls.count(("blink-start",)) > pixel.calls.count(
                    ("blink-stop",)
                ):
                    break
                await asyncio.sleep(0)
            await controller.set_state("ready")

        await controller.stop()

        self.assertEqual(
            pixel.calls.count(("blink-start",)),
            pixel.calls.count(("blink-stop",)),
        )
        self.assertIsNone(controller._task)
        self.assertEqual(pixel.calls[-1], ("off",))

    async def test_error_is_solid_and_remains_latched_until_explicit_stop(self):
        pixel = AuditedPixel()
        controller = Controller(pixel)
        await controller.set_state("connecting")
        await asyncio.wait_for(pixel.blink_started.wait(), timeout=0.25)

        await controller.set_state("error")
        await asyncio.sleep(0)

        self.assertEqual(controller.state, "error")
        self.assertIsNone(controller._task)
        self.assertEqual(pixel.calls[-2:], [("set_color", 25, 0, 0), ("on",)])
        self.assertNotEqual(pixel.calls[-1], ("off",))

        await controller.stop()
        self.assertEqual(pixel.calls[-1], ("off",))

    async def test_cancelling_transition_propagates_and_retains_cleanup_ownership(self):
        pixel = SlowCleanupPixel()
        controller = Controller(pixel)
        await controller.set_state("connecting")
        await asyncio.wait_for(pixel.blink_started.wait(), timeout=0.25)

        transition = asyncio.create_task(controller.set_state("ready"))
        await asyncio.wait_for(pixel.cleanup_started.wait(), timeout=0.25)
        transition.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await transition

        self.assertEqual(controller.state, "connecting")
        self.assertIsNotNone(controller._task)

        pixel.release_cleanup.set()
        await controller.stop()

        self.assertTrue(pixel.blink_cancelled.is_set())
        self.assertIsNone(controller._task)
        self.assertEqual(pixel.calls[-1], ("off",))


if __name__ == "__main__":
    unittest.main()
