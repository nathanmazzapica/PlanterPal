import asyncio
from machine import I2C

from lib.pcf8574 import PCF8574
from lib.hd44780 import HD44780
from lib.lcd import LCD


LCD_ADDR = 0x27
LCD_LINES = 2
LCD_COLUMNS = 16


class _DisplayCommand:
    def __init__(self, kind, values, completed=None):
        self.kind = kind
        self.values = values
        self.completed = completed
        self.error = None
        self.succeeded = False


class Display:
    """Owns the LCD and serializes all of its work through one task."""

    def __init__(
        self,
        bus: I2C,
        bus_lock,
        channel,
        pcf_type=PCF8574,
        hd_type=HD44780,
        lcd_type=LCD,
        sleep=asyncio.sleep,
    ):
        self._bus = bus
        self._bus_lock = bus_lock
        self._channel = channel
        self._pcf_type = pcf_type
        self._hd_type = hd_type
        self._lcd_type = lcd_type
        self._sleep = sleep
        self._lcd = None

        self._ready = asyncio.Event()
        self._state_changed = asyncio.Event()
        self._failure = None
        self._started = False
        self._running = False

        # This lock serializes reliable command submissions; it never protects
        # I2C. Hardware access always uses the injected shared bus lock above.
        self._critical_submission_lock = asyncio.Lock()
        self._critical_pending = False
        self._critical_command = None

    @property
    def failure(self):
        return self._failure

    def raise_if_failed(self):
        if self._failure is not None:
            raise self._failure

    async def wait_until_ready(self):
        while True:
            self.raise_if_failed()

            if self._ready.is_set():
                return

            if self._started and not self._running:
                raise RuntimeError("Display stopped before initialization completed")

            await self._state_changed.wait()
            self._state_changed.clear()

    async def render(self, lux_seconds, moisture, dli):
        """Submit an immutable reading snapshot, replacing a stale frame."""
        self._raise_if_unavailable()

        if self._critical_pending:
            return

        await self._channel.put(
            _DisplayCommand("render", (lux_seconds, moisture, dli))
        )

    async def write(self, body):
        await self.write_line(body, 0)

    async def write_line(self, body, line: int):
        await self._submit_critical("line", (str(body), line))

    async def display_err(self, desc: str, errno: int):
        await self._submit_critical("error", (str(desc), errno))

    async def run(self):
        if self._running:
            raise RuntimeError("Display is already running")

        self._started = True
        self._running = True
        self._failure = None
        self._ready.clear()

        try:
            await self._initialize()
            self._ready.set()
            self._state_changed.set()

            while True:
                command = await self._channel.get()

                try:
                    await self._execute(command)
                    command.succeeded = True
                except Exception as error:
                    command.error = error
                    raise
                finally:
                    if command.completed is not None:
                        command.completed.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failure = error
        finally:
            self._running = False
            self._ready.clear()
            self._state_changed.set()
            self._release_critical_waiter()

    async def _initialize(self):
        async with self._bus_lock:
            pcf = self._pcf_type(self._bus, address=LCD_ADDR)
            hd = self._hd_type(
                pcf,
                num_lines=LCD_LINES,
                num_columns=LCD_COLUMNS,
            )
            await hd.initialize()
            lcd = self._lcd_type(hd, pcf)
            lcd.backlight_on()
            self._lcd = lcd

    async def _submit_critical(self, kind, values):
        async with self._critical_submission_lock:
            self._raise_if_unavailable()
            completed = asyncio.Event()
            command = _DisplayCommand(kind, values, completed)
            self._critical_pending = True
            self._critical_command = command

            try:
                await self._channel.put(command)
                await completed.wait()

                if command.error is not None:
                    raise command.error
                if not command.succeeded:
                    raise RuntimeError("Display stopped before command completed")
            finally:
                if self._critical_command is command:
                    self._critical_command = None
                self._critical_pending = False

    def _raise_if_unavailable(self):
        self.raise_if_failed()
        if not self._ready.is_set():
            raise RuntimeError("Display is not ready")

    async def _execute(self, command):
        if command.kind == "render":
            await self._render(*command.values)
            return

        if command.kind == "line":
            async with self._bus_lock:
                await self._lcd.write_line(*command.values)
            return

        if command.kind == "error":
            await self._display_error(*command.values)
            return

        raise ValueError("Unknown display command")

    async def _render(self, lux_seconds, moisture, dli):
        async with self._bus_lock:
            await self._lcd.write_line(
                f"Lux:{self._format_lux(lux_seconds)}s|M:{moisture:.0f}%",
                0,
            )
            await self._lcd.write_line(f"DLI:{dli}", 1)

    async def _display_error(self, desc, error_number):
        async with self._bus_lock:
            await self._lcd.write_line(f"Err[{str(error_number)}]", 0)

        text = " " * LCD_COLUMNS + desc + " " * LCD_COLUMNS
        for index in range(len(text) - LCD_COLUMNS + 1):
            async with self._bus_lock:
                await self._lcd.write_line(
                    text[index:index + LCD_COLUMNS],
                    1,
                )

            # The marquee delay is not an I2C transaction. Release the shared
            # bus so sensor work can proceed while the next frame waits.
            await self._sleep(0.2)

    def _release_critical_waiter(self):
        command = self._critical_command
        if command is None or command.completed.is_set():
            return

        if command.error is None and self._failure is not None:
            command.error = self._failure
        command.completed.set()

    def _format_lux(self, lux):
        if lux < 1_000:
            return f"{lux:.0f}"

        k_lux = lux / 1_000

        if lux < 100_000:
            return f"{k_lux:.1f}K"

        if lux < 1_000_000:
            return f"{k_lux:.0f}K"

        m_lux = lux / 1_000_000
        return f"{m_lux:.1f}M"
