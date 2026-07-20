class LCDPresenceProbe:
    """Detect an LCD address without taking ownership of the device."""

    def __init__(self, bus, bus_lock, address):
        self._bus = bus
        self._bus_lock = bus_lock
        self._address = address

    async def is_present(self):
        async with self._bus_lock:
            return self._address in self._bus.scan()
