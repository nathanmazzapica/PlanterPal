import asyncio
import unittest

from lib.async_channel import SingleValueChannel


class SingleValueChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_value_replaces_unconsumed_value(self):
        channel = SingleValueChannel()

        await channel.put("older")
        await channel.put("newest")

        self.assertEqual(await channel.get(), "newest")

    async def test_get_waits_until_a_value_is_submitted(self):
        channel = SingleValueChannel()
        get_task = asyncio.create_task(channel.get())
        await asyncio.sleep(0)

        self.assertFalse(get_task.done())

        await channel.put("payload")
        self.assertEqual(
            await asyncio.wait_for(get_task, timeout=0.25),
            "payload",
        )


if __name__ == "__main__":
    unittest.main()
