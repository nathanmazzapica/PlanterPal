import asyncio
import errno
import time

import config as cfg
from web.exceptions import (
    ErrConnectionReset,
    ErrHostUnreachable,
    ErrHttpStatus,
    ErrNetwork,
    ErrTimedOut,
)


def _ticks_ms():
    ticks_ms = getattr(time, "ticks_ms", None)
    if ticks_ms is not None:
        return ticks_ms()
    return int(time.monotonic() * 1000)


def _ticks_diff(new, old):
    ticks_diff = getattr(time, "ticks_diff", None)
    if ticks_diff is not None:
        return ticks_diff(new, old)
    return new - old


class _RequestDeadline:
    def __init__(self, timeout_s, wait_for, ticks_ms, ticks_diff):
        self._timeout_ms = int(timeout_s * 1000)
        self._wait_for = wait_for
        self._ticks_ms = ticks_ms
        self._ticks_diff = ticks_diff
        self._started_at = ticks_ms()

    async def wait(self, awaitable):
        elapsed_ms = self._ticks_diff(self._ticks_ms(), self._started_at)
        remaining_ms = self._timeout_ms - elapsed_ms

        if remaining_ms <= 0:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise asyncio.TimeoutError

        return await self._wait_for(awaitable, remaining_ms / 1000)


def _legacy_configured_host():
    """Read only the backend host from the pre-provisioning config format."""

    try:
        from web.wifi_config import cfg
    except ImportError:
        return None

    return cfg.get("host")


class Client():
    def __init__(
        self,
        host=None,
        open_connection=None,
        request_timeout_s=None,
        _wait_for=None,
        _ticks_ms_fn=None,
        _ticks_diff_fn=None,
    ):
        self.host = host if host is not None else _legacy_configured_host()
        if not isinstance(self.host, str) or not self.host:
            raise RuntimeError("backend host is not configured")

        if request_timeout_s is None:
            request_timeout_s = cfg.HTTP_REQUEST_TIMEOUT_S
        if (
            not isinstance(request_timeout_s, (int, float))
            or isinstance(request_timeout_s, bool)
        ):
            raise TypeError("request_timeout_s must be a number")
        try:
            timeout_ms = int(request_timeout_s * 1000)
        except (OverflowError, ValueError):
            raise ValueError("request_timeout_s must be finite")
        if request_timeout_s <= 0 or timeout_ms <= 0:
            raise ValueError("request_timeout_s must be at least 0.001")

        self.request_timeout_s = request_timeout_s
        self._open_connection = (
            open_connection
            if open_connection is not None
            else asyncio.open_connection
        )
        self._wait_for = _wait_for if _wait_for is not None else asyncio.wait_for
        self._ticks_ms = _ticks_ms_fn if _ticks_ms_fn is not None else _ticks_ms
        self._ticks_diff = (
            _ticks_diff_fn if _ticks_diff_fn is not None else _ticks_diff
        )

    async def ping(self):
        status_code = await self._request("GET", "/healthz")
        print("Status code:", status_code)
        return status_code

    async def report(self, payload):
        return await self._request("POST", "/api/v1/readings", payload)

    async def _request(self, method, path, payload=None):
        deadline = _RequestDeadline(
            self.request_timeout_s,
            self._wait_for,
            self._ticks_ms,
            self._ticks_diff,
        )
        connection_host, connection_port = self._connection_address()
        host_header = self._host_header()
        writer = None
        request_succeeded = False
        skip_shutdown_wait = False

        try:
            reader, writer = await deadline.wait(
                self._open_connection(
                    connection_host,
                    connection_port,
                )
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
            await deadline.wait(writer.drain())

            status_line = await deadline.wait(reader.readline())
            status_code = self._parse_status_code(status_line)
            await self._read_headers(reader, deadline)
            self._raise_for_status(status_code)
            request_succeeded = True
            return status_code
        except asyncio.CancelledError:
            skip_shutdown_wait = True
            raise
        except asyncio.TimeoutError:
            skip_shutdown_wait = True
            raise ErrTimedOut("HTTP request deadline expired")
        except OSError as error:
            self._raise_network_error(error)
        finally:
            if writer is not None:
                close_timed_out = await self._close_writer(
                    writer,
                    deadline,
                    await_shutdown=not skip_shutdown_wait,
                )
                if close_timed_out and request_succeeded:
                    raise ErrTimedOut("HTTP request deadline expired")

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

    def _raise_for_status(self, status_code):
        if status_code < 200 or status_code >= 300:
            raise ErrHttpStatus(status_code)

    async def _read_headers(self, reader, deadline):
        while True:
            line = await deadline.wait(reader.readline())
            if line in (b"", b"\r\n", b"\n"):
                return

    async def _close_writer(self, writer, deadline, await_shutdown=True):
        try:
            writer.close()
            wait_closed = getattr(writer, "wait_closed", None)
            if wait_closed is not None and await_shutdown:
                try:
                    await deadline.wait(wait_closed())
                except asyncio.TimeoutError:
                    return True
        except OSError:
            pass
        return False

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
