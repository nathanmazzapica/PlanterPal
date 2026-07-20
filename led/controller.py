import asyncio


class Controller:
    """Drives a status LED from a high-level device state."""

    def __init__(self, ws2811):
        self._ws2811 = ws2811
        self._state = None
        # Task running the current animation, if any.
        self._task = None

    @property
    def state(self):
        return self._state

    def set_state(self, state):
        if state == self._state:
            return
        self._state = state
        self._apply()

    async def stop(self):
        """Stop the owned animation and leave the NeoPixel off."""

        task = self._task
        if task is None and self._state is None:
            return

        self._task = None
        self._state = None

        try:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            self._ws2811.off()

    def _apply(self):
        # Stop any running animation before switching states.
        if self._task is not None:
            self._task.cancel()
            self._task = None

        if self._state == "provisioning":
            # Fade in and out blue.
            self._ws2811.set_color(0, 0, 25)
            self._task = asyncio.create_task(self._ws2811.blink())
        elif self._state == "connecting":
            # Fade in and out cyan while Wi-Fi is connecting.
            self._ws2811.set_color(0, 25, 25)
            self._task = asyncio.create_task(self._ws2811.blink())
        elif self._state == "ready":
            # Solid green.
            self._ws2811.set_color(0, 25, 0)
            self._ws2811.on()
        elif self._state == "error":
            # Solid red.
            self._ws2811.set_color(25, 0, 0)
            self._ws2811.on()
