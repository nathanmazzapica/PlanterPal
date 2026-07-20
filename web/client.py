import asyncio
import errno
from web.exceptions import ErrNetwork, ErrHostUnreachable, ErrTimedOut, ErrConnectionReset


def _legacy_configured_host():
    """Read only the backend host from the pre-provisioning config format."""

    try:
        from web.wifi_config import cfg
    except ImportError:
        return None

    return cfg.get("host")


class Client():
    def __init__(self, host=None, open_connection=None):
        self.host = host if host is not None else _legacy_configured_host()
        if not isinstance(self.host, str) or not self.host:
            raise RuntimeError("backend host is not configured")
        self._open_connection = (
            open_connection
            if open_connection is not None
            else asyncio.open_connection
        )

    async def ping(self):
        status_code = await self._request("GET", "/healthz")
        print("Status code:", status_code)
        return status_code

    async def report(self, payload):
        return await self._request("POST", "/api/v1/readings", payload)

    async def _request(self, method, path, payload=None):
        connection_host, connection_port = self._connection_address()
        host_header = self._host_header()
        writer = None

        try:
            reader, writer = await self._open_connection(
                connection_host,
                connection_port,
            )
            body = b"" if payload is None else payload.encode("utf-8")
            headers = (
                f"{method} {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Connection: close\r\n"
            )

            if payload is not None:
                headers += (
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                )

            writer.write(headers.encode("utf-8") + b"\r\n" + body)
            await writer.drain()

            status_line = await reader.readline()
            status_code = self._parse_status_code(status_line)
            await self._read_headers(reader)
            return status_code
        except OSError as error:
            self._raise_network_error(error)
        finally:
            if writer is not None:
                await self._close_writer(writer)

    def _connection_address(self):
        host = self._host_header()

        hostname, separator, port = host.rpartition(":")
        if separator and port.isdigit():
            return hostname, int(port)

        return host, 80

    def _host_header(self):
        if self.host.startswith("http://"):
            return self.host[len("http://"):]

        return self.host

    def _parse_status_code(self, status_line):
        parts = status_line.split(None, 2)
        if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
            raise ValueError("Invalid HTTP status line")

        return int(parts[1])

    async def _read_headers(self, reader):
        while True:
            line = await reader.readline()
            if line in (b"", b"\r\n", b"\n"):
                return

    async def _close_writer(self, writer):
        try:
            writer.close()
            wait_closed = getattr(writer, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()
        except OSError:
            pass

    def _raise_network_error(self, error):
        error_number = getattr(error, "errno", None)
        if error_number is None and error.args:
            error_number = error.args[0]

        if error_number == errno.EHOSTUNREACH:
            raise ErrHostUnreachable
        if error_number == errno.ETIMEDOUT:
            raise ErrTimedOut
        if error_number == errno.ECONNRESET:
            raise ErrConnectionReset
        raise ErrNetwork
