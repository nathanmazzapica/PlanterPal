import asyncio
import network
import time
import web.network_config as cfg


class ConnectionResult:
    """Immutable result of a bounded credential connection attempt."""

    CONNECTED = "connected"
    WRONG_PASSWORD = "wrong_password"
    NO_AP = "no_ap"
    TIMEOUT = "timeout"
    CONNECT_FAILED = "connect_failed"

    __slots__ = ("_success", "_reason")

    def __init__(self, success, reason):
        if not isinstance(success, bool):
            raise TypeError("success must be a boolean")
        if reason not in (
            self.CONNECTED,
            self.WRONG_PASSWORD,
            self.NO_AP,
            self.TIMEOUT,
            self.CONNECT_FAILED,
        ):
            raise ValueError("unknown connection result reason")
        if success != (reason == self.CONNECTED):
            raise ValueError("only a connected result may be successful")

        self._success = success
        self._reason = reason

    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise AttributeError("ConnectionResult is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise AttributeError("ConnectionResult is immutable")

    @property
    def success(self):
        return self._success

    @property
    def reason(self):
        return self._reason

    def __bool__(self):
        return self.success

    def __eq__(self, other):
        return (
            isinstance(other, ConnectionResult)
            and self.success == other.success
            and self.reason == other.reason
        )

    def __repr__(self):
        return "ConnectionResult(success={!r}, reason={!r})".format(
            self.success,
            self.reason,
        )

    __str__ = __repr__


class NetworkManager:
    """Owns the station interface and keeps its connection alive."""

    def __init__(self, ssid=None, password=None, wlan=None, credentials=None):
        if credentials is not None:
            if ssid is not None or password is not None:
                raise TypeError(
                    "provide either credentials or ssid/password, not both"
                )
            ssid, password = self._credential_values(credentials)
        elif ssid is None and password is None:
            pass
        elif ssid is None or password is None:
            raise TypeError("ssid and password must be provided together")
        else:
            self._validate_credential_values(ssid, password)

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

    @property
    def running(self):
        return self._running

    @property
    def busy(self):
        """Whether this owner currently controls the station interface."""

        return self._running or self._connecting

    @property
    def has_credentials(self):
        return self._ssid is not None and self._password is not None

    @property
    def active_ssid(self):
        """Return the active SSID without exposing the active password."""

        return self._ssid

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

    async def try_credentials(self, credentials, timeout_s=None):
        """Try one credential value without starting the reconnect loop.

        A successful candidate is adopted as the active in-memory credential
        value. A failed or cancelled candidate leaves the previous active
        value untouched, but leaves the station disconnected.
        """

        if self._running:
            raise RuntimeError("cannot test credentials while NetworkManager is running")
        if self._connecting:
            raise RuntimeError("WiFi connection attempt is already in progress")

        ssid, password = self._credential_values(credentials)
        if timeout_s is None:
            timeout_s = cfg.WIFI_CONNECT_TIMEOUT_S
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
            raise TypeError("timeout_s must be a number")
        if timeout_s < 0:
            raise ValueError("timeout_s must not be negative")

        previous_ssid = self._ssid
        previous_password = self._password

        self._disconnect()
        self._set_disconnected()

        try:
            result = await self._attempt_connection(ssid, password, timeout_s)
            if result.success:
                self._ssid = ssid
                self._password = password
                self._set_connected()
                return result

            self._disconnect()
            self._set_disconnected()
            return result
        except asyncio.CancelledError:
            self._disconnect()
            self._set_disconnected()
            raise
        except Exception:
            self._disconnect()
            self._set_disconnected()
            raise
        finally:
            if not self.is_connected():
                self._ssid = previous_ssid
                self._password = previous_password

    def disconnect(self):
        """Disconnect while idle, preserving the active credentials."""

        self._require_idle()
        self._disconnect()
        self._set_disconnected()

    def forget_active_credentials(self):
        """Disconnect and discard active credentials from memory while idle."""

        self._require_idle()
        self._disconnect()
        self._set_disconnected()
        self._ssid = None
        self._password = None

    async def run(self):
        if self._running:
            raise RuntimeError("NetworkManager is already running")
        if self._connecting:
            raise RuntimeError("WiFi connection attempt is already in progress")
        if not self.has_credentials:
            raise RuntimeError("NetworkManager has no active credentials")

        self._running = True
        self._failure = None
        backoff_index = 0

        try:
            while True:
                try:
                    wlan_connected = self._wlan.isconnected()
                except OSError:
                    wlan_connected = False

                if wlan_connected and self._has_valid_ip():
                    self._set_connected()
                    backoff_index = 0
                    await asyncio.sleep(cfg.WIFI_MONITOR_INTERVAL_S)
                    continue

                self._set_disconnected()

                result = await self._connect_once()
                if result.success:
                    self._set_connected()
                    backoff_index = 0
                    continue

                print("WiFi connection failed:", result.reason)
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
        return await self._attempt_connection(
            self._ssid,
            self._password,
            cfg.WIFI_CONNECT_TIMEOUT_S,
        )

    async def _attempt_connection(self, ssid, password, timeout_s):
        if self._connecting:
            raise RuntimeError("WiFi connection attempt is already in progress")

        self._connecting = True

        try:
            print("connecting to network...")
            try:
                self._wlan.active(True)
                self._wlan.connect(ssid, password)
            except OSError:
                self._disconnect()
                return ConnectionResult(False, ConnectionResult.CONNECT_FAILED)

            started_at = time.ticks_ms()
            timeout_ms = int(timeout_s * 1000)

            while True:
                try:
                    if self._wlan.isconnected() and self._has_valid_ip():
                        return ConnectionResult(True, ConnectionResult.CONNECTED)

                    terminal_reason = self._terminal_failure_reason()
                except OSError:
                    self._disconnect()
                    return ConnectionResult(False, ConnectionResult.CONNECT_FAILED)

                if terminal_reason is not None:
                    self._disconnect()
                    return ConnectionResult(False, terminal_reason)

                if time.ticks_diff(time.ticks_ms(), started_at) >= timeout_ms:
                    self._disconnect()
                    return ConnectionResult(False, ConnectionResult.TIMEOUT)

                await asyncio.sleep(cfg.WIFI_POLL_INTERVAL_S)
        finally:
            self._connecting = False

    def _terminal_failure_reason(self):
        status_method = getattr(self._wlan, "status", None)
        if status_method is None:
            return None

        status = status_method()
        statuses = (
            (getattr(network, "STAT_WRONG_PASSWORD", None), ConnectionResult.WRONG_PASSWORD),
            (getattr(network, "STAT_NO_AP_FOUND", None), ConnectionResult.NO_AP),
            (getattr(network, "STAT_CONNECT_FAIL", None), ConnectionResult.CONNECT_FAILED),
        )
        for status_value, reason in statuses:
            if status_value is not None and status == status_value:
                return reason

        return None

    def _has_valid_ip(self):
        config = self._wlan.ifconfig()
        if not config:
            return False

        address = config[0]
        return bool(address) and address != "0.0.0.0"

    def _require_idle(self):
        if self._running:
            raise RuntimeError("NetworkManager is running")
        if self._connecting:
            raise RuntimeError("WiFi connection attempt is in progress")

    @staticmethod
    def _credential_values(credentials):
        try:
            ssid = credentials.ssid
            password = credentials.password
        except AttributeError:
            raise TypeError("credentials must expose ssid and password")

        NetworkManager._validate_credential_values(ssid, password)
        return ssid, password

    @staticmethod
    def _validate_credential_values(ssid, password):
        if not isinstance(ssid, str):
            raise TypeError("ssid must be a string")
        if not ssid:
            raise ValueError("ssid must not be empty")
        if not isinstance(password, str):
            raise TypeError("password must be a string")

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
        print("\nnetwork config:", self._wlan.ifconfig())

    def _set_disconnected(self):
        if not self.is_connected():
            return

        self.connected.clear()
        self._state_changed.set()
