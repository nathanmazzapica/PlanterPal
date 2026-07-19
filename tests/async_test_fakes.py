import asyncio
import inspect
import sys
import types
from contextlib import contextmanager


async def bounded_wait(awaitable, message, timeout=0.5):
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as error:
        raise AssertionError(message) from error


def install_machine_stub():
    """Install the minimum MicroPython machine module needed for host imports."""
    if "machine" in sys.modules:
        return

    machine = types.ModuleType("machine")

    class I2C:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class ADC:
        ATTN_11DB = 3

        def __init__(self, pin):
            self.pin = pin

        def atten(self, value):
            self.attenuation = value

    class Pin:
        OUT = 1

        def __init__(self, number, mode=None):
            self.number = number
            self.mode = mode

        def on(self):
            pass

        def off(self):
            pass

    machine.I2C = I2C
    machine.ADC = ADC
    machine.Pin = Pin
    sys.modules["machine"] = machine


class FakeI2C:
    def __init__(self, data=b"\x01\x20", lock=None, write_error=None, read_error=None):
        self.data = data
        self.lock = lock
        self.write_error = write_error
        self.read_error = read_error
        self.operations = []

    def writeto(self, address, payload):
        self.operations.append(("write", address, payload, self._lock_is_held()))
        if self.write_error is not None:
            raise self.write_error

    def readfrom(self, address, length):
        self.operations.append(("read", address, length, self._lock_is_held()))
        if self.read_error is not None:
            raise self.read_error
        return self.data

    def _lock_is_held(self):
        if self.lock is None:
            return None
        return self.lock.locked()


class GateLock:
    """A deterministic async lock whose acquisition points are public barriers."""

    def __init__(self, acquisition_count=4, initially_open=()):
        self.requested = [asyncio.Event() for _ in range(acquisition_count)]
        self.acquired = [asyncio.Event() for _ in range(acquisition_count)]
        self.gates = [asyncio.Event() for _ in range(acquisition_count)]
        for index in initially_open:
            self.gates[index].set()
        self._next_acquisition = 0
        self._locked = False

    async def acquire(self):
        index = self._next_acquisition
        self._next_acquisition += 1
        if index >= len(self.requested):
            raise AssertionError("unexpected lock acquisition")

        self.requested[index].set()
        await self.gates[index].wait()
        if self._locked:
            raise AssertionError("lock acquired concurrently")
        self._locked = True
        self.acquired[index].set()
        return True

    def release(self):
        if not self._locked:
            raise RuntimeError("release of unlocked lock")
        self._locked = False

    def locked(self):
        return self._locked

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.release()
        return False


class SleepBarrier:
    def __init__(self, initially_released=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if initially_released:
            self.release.set()
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)
        self.entered.set()
        await self.release.wait()


class AwaitableFloat(float):
    """A numeric sensor result that also supports an async consumer."""

    def __await__(self):
        async def resolve():
            return float(self)

        return resolve().__await__()


class DeferredResult:
    """Awaitable operation controlled entirely by public Events."""

    def __init__(self, requested, release, value=None, error=None, on_success=None):
        self.requested = requested
        self.release = release
        self.value = value
        self.error = error
        self.on_success = on_success

    def __await__(self):
        async def resolve():
            self.requested.set()
            await self.release.wait()
            if self.error is not None:
                raise self.error
            if self.on_success is not None:
                self.on_success()
            return self.value

        return resolve().__await__()

    def __radd__(self, other):
        return other + self.value


class DeferredLuxSensor:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.requested = asyncio.Event()
        self.release = asyncio.Event()

    def lux(self):
        return DeferredResult(
            self.requested,
            self.release,
            value=self.value,
            error=self.error,
        )


class DeferredLightMonitor:
    def __init__(self, lux_seconds=0, current_lux=0, dli=0, error=None, order=None):
        self.lux_seconds = lux_seconds
        self.current_lux = current_lux
        self.dli = dli
        self.error = error
        self.order = order
        self.requested = asyncio.Event()
        self.release = asyncio.Event()

    def update(self):
        def record_completion():
            if self.order is not None:
                self.order.append("light_complete")

        return DeferredResult(
            self.requested,
            self.release,
            error=self.error,
            on_success=record_completion,
        )


class RecordingMoistureMonitor:
    def __init__(self, moisture_percent=0, order=None, error=None):
        self.moisture_percent = moisture_percent
        self.order = order
        self.error = error
        self.update_calls = 0

    def update(self):
        self.update_calls += 1
        if self.order is not None:
            self.order.append("moisture")
        if self.error is not None:
            raise self.error


class SequenceClock:
    def __init__(self, values, period=1 << 30):
        self._values = iter(values)
        self._period = period

    def ticks_ms(self):
        return next(self._values)

    def ticks_diff(self, current, previous):
        half_period = self._period // 2
        return ((current - previous + half_period) % self._period) - half_period


class RecordingClock:
    def __init__(self, value):
        self.value = value
        self.ticks_ms_calls = 0

    def ticks_ms(self):
        self.ticks_ms_calls += 1
        return self.value

    def ticks_diff(self, current, previous):
        return current - previous


@contextmanager
def patched_driver_sleep(module, replacement, synchronous_replacement=None):
    """Patch either import style without mutating the host asyncio module."""
    saved = {}
    async_names = ("asyncio", "uasyncio")

    for name in async_names:
        if hasattr(module, name):
            saved[name] = getattr(module, name)
            setattr(module, name, types.SimpleNamespace(sleep=replacement))

    if hasattr(module, "sleep"):
        saved["sleep"] = getattr(module, "sleep")
        setattr(module, "sleep", synchronous_replacement or replacement)

    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(module, name, value)


async def call_maybe_async(callable_, *args, **kwargs):
    result = callable_(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result
