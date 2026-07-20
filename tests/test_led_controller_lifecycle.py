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


class LedControllerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_connecting_fades_cyan_until_ready(self):
        pixel = AuditedPixel()
        controller = Controller(pixel)

        controller.set_state("connecting")
        await asyncio.wait_for(pixel.blink_started.wait(), timeout=0.25)

        self.assertEqual(pixel.calls[0], ("set_color", 0, 25, 25))
        self.assertIn(("blink-start",), pixel.calls)

        controller.set_state("ready")
        await asyncio.wait_for(pixel.blink_cancelled.wait(), timeout=0.25)

        self.assertEqual(controller.state, "ready")
        self.assertIn(("blink-stop",), pixel.calls)
        self.assertIn(("set_color", 0, 25, 0), pixel.calls)
        self.assertIn(("on",), pixel.calls)

    async def test_stop_owns_animation_cancellation_and_turns_pixel_off(self):
        pixel = AuditedPixel()
        controller = Controller(pixel)
        controller.set_state("provisioning")
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
        controller.set_state("provisioning")
        await asyncio.wait_for(pixel.blink_started.wait(), timeout=0.25)

        await controller.stop()
        await controller.stop()
        controller.set_state("ready")

        self.assertEqual(pixel.calls.count(("blink-stop",)), 1)
        self.assertEqual(pixel.calls[-2:], [("set_color", 0, 25, 0), ("on",)])
        self.assertEqual(controller.state, "ready")


if __name__ == "__main__":
    unittest.main()
