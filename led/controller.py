import asyncio


class Controller:
    """Drives a status LED from a high-level device state."""

    def __init__(self, ws2811):
        self._ws2811 = ws2811
        self._state = None
        # Task running the current animation, if any.
        self._task = None
        self._task_stopped = None
        self._task_cancel_requested = False
        self._transition_lock = asyncio.Lock()

    @property
    def state(self):
        return self._state

    async def set_state(self, state):
        """Set one device state after settling the previous animation."""

        async with self._transition_lock:
            if state == self._state:
                return

            await self._stop_animation()
            self._state = state
            self._apply()

    async def stop(self):
        """Stop the owned animation and leave the NeoPixel off."""

        async with self._transition_lock:
            try:
                await self._stop_animation()
            finally:
                self._state = None
                self._ws2811.off()

    async def _stop_animation(self):
        task = self._task
        if task is None:
            return

        if not self._task_cancel_requested:
            self._task_cancel_requested = True
            task.cancel()
        await self._task_stopped.wait()

        if self._task is task:
            self._task = None
            self._task_stopped = None
            self._task_cancel_requested = False

    async def _run_animation(self, stopped):
        try:
            await self._ws2811.blink()
        finally:
            stopped.set()

    def _start_animation(self):
        stopped = asyncio.Event()
        self._task_stopped = stopped
        self._task_cancel_requested = False
        self._task = asyncio.create_task(self._run_animation(stopped))

    def _apply(self):
        if self._state == "provisioning":
            # Fade in and out blue.
            self._ws2811.set_color(0, 0, 25)
            self._start_animation()
        elif self._state == "connecting":
            # Fade in and out cyan while Wi-Fi is connecting.
            self._ws2811.set_color(0, 25, 25)
            self._start_animation()
        elif self._state == "ready":
            # Solid green.
            self._ws2811.set_color(0, 25, 0)
            self._ws2811.on()
        elif self._state == "error":
            # Solid red.
            self._ws2811.set_color(25, 0, 0)
            self._ws2811.on()
