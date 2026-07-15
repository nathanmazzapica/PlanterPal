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

    def _apply(self):
        # Stop any running animation before switching states.
        if self._task is not None:
            self._task.cancel()
            self._task = None

        if self._state == "provisioning":
            # Fade in and out blue.
            self._ws2811.set_color(0, 0, 255)
            self._task = asyncio.create_task(self._ws2811.blink())
        elif self._state == "ready":
            # Solid green.
            self._ws2811.set_color(0, 255, 0)
            self._ws2811.on()
        elif self._state == "error":
            # Solid red.
            self._ws2811.set_color(255, 0, 0)
            self._ws2811.on()
