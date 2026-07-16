import asyncio
from machine import Pin
from neopixel import NeoPixel
from micropython import const

NUM_LEDS = const(1)

# Fade animation tuning.
_FADE_STEPS = const(20)


class WS2811B:
    """Minimal driver for a single WS2811B pixel, built on NeoPixel."""

    def __init__(self, pin):
        # Accept either a pin number or an already-built machine.Pin.
        if not isinstance(pin, Pin):
            pin = Pin(pin, Pin.OUT)
        self._np = NeoPixel(pin, NUM_LEDS)
        self._color = (0, 0, 0)
        self._is_on = False

    def set_color(self, r, g, b):
        """Set the pixel's color, applying it immediately if the pixel is on."""
        self._color = (r, g, b)
        self._update()

    def on(self):
        """Turn the pixel on, showing its current color."""
        self._is_on = True
        self._update()

    def off(self):
        """Turn the pixel off without discarding its color."""
        self._is_on = False
        self._update()

    def toggle(self):
        """Flip the pixel between on and off."""
        if self._is_on:
            self.off()
        else:
            self.on()

    def _update(self, scale=1.0):
        """Write the current color (optionally scaled) to the pixel."""
        if self._is_on:
            r, g, b = self._color
            self._np[0] = (
                int(r * scale),
                int(g * scale),
                int(b * scale),
            )
        else:
            self._np[0] = (0, 0, 0)
        self._np.write()

    async def blink(self, period_ms=1000):
        """Continuously fade the current color in and out.

        Runs until the task is cancelled. `period_ms` is the duration of one
        full in-and-out cycle. On cancellation the pixel is left as-is; the
        caller is responsible for setting its resting state.
        """
        self._is_on = True
        step_s = period_ms / (2 * _FADE_STEPS) / 1000
        while True:
            # Fade in, then out.
            for i in range(_FADE_STEPS + 1):
                self._update(i / _FADE_STEPS)
                await asyncio.sleep(step_s)
            for i in range(_FADE_STEPS, -1, -1):
                self._update(i / _FADE_STEPS)
                await asyncio.sleep(step_s)
