import asyncio
import unittest

from display.null_display import NullDisplay


class NullDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def start_display(self):
        display = NullDisplay()
        task = asyncio.create_task(display.run())
        await asyncio.wait_for(display.wait_until_ready(), timeout=0.25)
        return display, task

    async def stop_display(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_ready_display_accepts_the_complete_sink_interface(self):
        display, task = await self.start_display()
        try:
            await display.render(12, 34, 0.5)
            await display.write("hello")
            await display.write_line("world", 1)
            await display.display_err("ignored", 7)
            display.raise_if_failed()
            self.assertIsNone(display.failure)
        finally:
            await self.stop_display(task)

    async def test_commands_before_start_are_rejected(self):
        display = NullDisplay()

        for command in (
            display.render(12, 34, 0.5),
            display.write("hello"),
            display.write_line("world", 1),
            display.display_err("ignored", 7),
        ):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                await command

    async def test_cancellation_propagates_and_waiters_observe_stop(self):
        display, task = await self.start_display()
        await self.stop_display(task)

        with self.assertRaisesRegex(RuntimeError, "stopped before initialization"):
            await display.wait_until_ready()
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            await display.render(12, 34, 0.5)

    async def test_concurrent_run_is_rejected(self):
        display, task = await self.start_display()
        try:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                await display.run()
        finally:
            await self.stop_display(task)


if __name__ == "__main__":
    unittest.main()
