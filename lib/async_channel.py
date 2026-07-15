import asyncio


class SingleValueChannel:
    """A single-slot async channel for MicroPython.

    MicroPython's asyncio does not provide asyncio.Queue, so this offers a
    minimal replacement built on asyncio.Event. It holds at most one value:
    a newer put() replaces an older, unconsumed value.
    """

    def __init__(self):
        self._value = None
        self._available = asyncio.Event()

    async def put(self, value):
        self._value = value
        self._available.set()

    async def get(self):
        await self._available.wait()
        value = self._value
        self._value = None
        self._available.clear()
        return value
