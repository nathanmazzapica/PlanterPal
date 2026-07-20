import asyncio


class NullDisplay:
    """Lifecycle-compatible display sink for headless running mode."""

    def __init__(self):
        self._ready = asyncio.Event()
        self._state_changed = asyncio.Event()
        self._lifetime = asyncio.Event()
        self._started = False
        self._running = False

    @property
    def failure(self):
        return None

    def raise_if_failed(self):
        pass

    async def wait_until_ready(self):
        while True:
            if self._ready.is_set():
                return

            if self._started and not self._running:
                raise RuntimeError("Display stopped before initialization completed")

            await self._state_changed.wait()
            self._state_changed.clear()

    async def run(self):
        if self._running:
            raise RuntimeError("Display is already running")

        self._started = True
        self._running = True
        self._ready.set()
        self._state_changed.set()

        try:
            await self._lifetime.wait()
        finally:
            self._running = False
            self._ready.clear()
            self._state_changed.set()

    async def render(self, lux_seconds, moisture, dli):
        self._raise_if_unavailable()

    async def write(self, body):
        await self.write_line(body, 0)

    async def write_line(self, body, line):
        self._raise_if_unavailable()

    async def display_err(self, desc, errno):
        self._raise_if_unavailable()

    def _raise_if_unavailable(self):
        if not self._ready.is_set():
            raise RuntimeError("Display is not ready")
