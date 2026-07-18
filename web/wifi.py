import asyncio
import network
import time
import config as cfg


class NetworkManager:
    """Owns the station interface and keeps its connection alive."""

    def __init__(self, ssid, password, wlan=None):
        self._ssid = ssid
        self._password = password
        self._wlan = wlan if wlan is not None else network.WLAN(network.STA_IF)

        self.connected = asyncio.Event()
        self._state_changed = asyncio.Event()
        self._failure = None
        self._running = False
        self._connecting = False

    @property
    def failure(self):
        return self._failure

    def is_connected(self):
        return self.connected.is_set()

    def raise_if_failed(self):
        if self._failure is not None:
            raise self._failure

    async def wait_until_connected(self):
        while True:
            self.raise_if_failed()

            if self.is_connected():
                return

            await self._state_changed.wait()
            self._state_changed.clear()

    async def run(self):
        if self._running:
            raise RuntimeError("NetworkManager is already running")

        self._running = True
        self._failure = None
        backoff_index = 0

        try:
            while True:
                try:
                    wlan_connected = self._wlan.isconnected()
                except OSError:
                    wlan_connected = False

                if wlan_connected:
                    self._set_connected()
                    backoff_index = 0
                    await asyncio.sleep(cfg.WIFI_MONITOR_INTERVAL_S)
                    continue

                self._set_disconnected()

                try:
                    connected = await self._connect_once()
                except OSError as error:
                    print("WiFi connection failed:", error)
                    self._disconnect()
                    connected = False

                if connected:
                    self._set_connected()
                    backoff_index = 0
                    continue

                delay = cfg.WIFI_RECONNECT_BACKOFF_S[backoff_index]
                if backoff_index < len(cfg.WIFI_RECONNECT_BACKOFF_S) - 1:
                    backoff_index += 1
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failure = error
            self._state_changed.set()
        finally:
            self._disconnect()
            self._set_disconnected()
            self._running = False

    async def _connect_once(self):
        if self._connecting:
            raise RuntimeError("WiFi connection attempt is already in progress")

        self._connecting = True

        try:
            print('connecting to network...')
            self._wlan.active(True)
            self._wlan.connect(self._ssid, self._password)
            started_at = time.ticks_ms()

            while not self._wlan.isconnected():
                if time.ticks_diff(time.ticks_ms(), started_at) >= int(
                    cfg.WIFI_CONNECT_TIMEOUT_S * 1000
                ):
                    self._disconnect()
                    return False

                await asyncio.sleep(cfg.WIFI_POLL_INTERVAL_S)
                print('not connected')
                print('.', end='')

            return True
        finally:
            self._connecting = False

    def _disconnect(self):
        try:
            self._wlan.disconnect()
        except OSError:
            pass

    def _set_connected(self):
        if self.is_connected():
            return

        self.connected.set()
        self._state_changed.set()
        print('\nnetwork config:', self._wlan.ifconfig())

    def _set_disconnected(self):
        if not self.is_connected():
            return

        self.connected.clear()
        self._state_changed.set()
