import json


class Credentials:
    """Immutable Wi-Fi credentials value.

    The password is intentionally omitted from its string representation so an
    exception or diagnostic cannot accidentally disclose it.
    """

    VERSION = 1
    MAX_SSID_BYTES = 32
    MAX_PASSWORD_BYTES = 64

    __slots__ = ("_ssid", "_password", "_version")

    def __init__(self, ssid, password, version=VERSION):
        self._validate_text("ssid", ssid, allow_empty=False)
        self._validate_text("password", password, allow_empty=True)

        if len(ssid.encode("utf-8")) > self.MAX_SSID_BYTES:
            raise ValueError("ssid exceeds 32 UTF-8 bytes")
        if len(password.encode("utf-8")) > self.MAX_PASSWORD_BYTES:
            raise ValueError("password exceeds 64 UTF-8 bytes")
        if not isinstance(version, int) or isinstance(version, bool):
            raise TypeError("version must be an integer")

        self._ssid = ssid
        self._password = password
        self._version = version

    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise AttributeError("Credentials are immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise AttributeError("Credentials are immutable")

    @staticmethod
    def _validate_text(name, value, allow_empty):
        if not isinstance(value, str):
            raise TypeError("{} must be a string".format(name))
        if not allow_empty and not value:
            raise ValueError("{} must not be empty".format(name))

    @property
    def ssid(self):
        return self._ssid

    @property
    def password(self):
        return self._password

    @property
    def version(self):
        return self._version

    def __eq__(self, other):
        return (
            isinstance(other, Credentials)
            and self.ssid == other.ssid
            and self.password == other.password
            and self.version == other.version
        )

    def __repr__(self):
        return "Credentials(ssid={!r}, password=<redacted>, version={!r})".format(
            self.ssid,
            self.version,
        )

    __str__ = __repr__


class CredentialStore:
    """Owns the persisted Wi-Fi credential record in ESP32 NVS.

    This component deliberately knows nothing about network state. The
    provisioning coordinator calls ``save`` only after NetworkManager has
    confirmed association and obtained an IP address.
    """

    NAMESPACE = "planterpal"
    RECORD_KEY = "wifi"
    RECORD_VERSION = Credentials.VERSION
    MAX_RECORD_BYTES = 512

    def __init__(self, nvs=None):
        self._nvs = nvs if nvs is not None else self._create_nvs()

    @classmethod
    def _create_nvs(cls):
        try:
            import esp32
        except ImportError:
            raise RuntimeError("esp32.NVS is unavailable")

        return esp32.NVS(cls.NAMESPACE)

    def load(self):
        buffer = bytearray(self.MAX_RECORD_BYTES)

        try:
            length = self._nvs.get_blob(self.RECORD_KEY, buffer)
        except OSError:
            return None

        if not isinstance(length, int) or length <= 0 or length > len(buffer):
            return None

        try:
            record = json.loads(bytes(buffer[:length]).decode("utf-8"))
            if not isinstance(record, dict) or len(record) != 3:
                return None
            if record.get("version") != self.RECORD_VERSION:
                return None

            return Credentials(
                record["ssid"],
                record["password"],
                version=record["version"],
            )
        except (KeyError, TypeError, ValueError, UnicodeError):
            return None

    def save(self, credentials):
        if not isinstance(credentials, Credentials):
            raise TypeError("credentials must be a Credentials value")
        if credentials.version != self.RECORD_VERSION:
            raise ValueError("unsupported credential record version")

        record = {
            "version": credentials.version,
            "ssid": credentials.ssid,
            "password": credentials.password,
        }
        payload = json.dumps(record).encode("utf-8")
        if len(payload) > self.MAX_RECORD_BYTES:
            raise ValueError("credential record is too large")

        self._nvs.set_blob(self.RECORD_KEY, payload)
        self._nvs.commit()

    def clear(self):
        self._nvs.erase_key(self.RECORD_KEY)
        self._nvs.commit()
