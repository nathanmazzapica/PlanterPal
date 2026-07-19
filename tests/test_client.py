import asyncio
import errno
import importlib
import sys
import types
import unittest


def import_client():
    old_config = sys.modules.get("web.wifi_config")
    old_client = sys.modules.pop("web.client", None)
    config = types.ModuleType("web.wifi_config")
    config.cfg = {"host": "unused.example"}
    sys.modules["web.wifi_config"] = config

    try:
        return importlib.import_module("web.client").Client
    finally:
        sys.modules.pop("web.client", None)
        if old_client is not None:
            sys.modules["web.client"] = old_client
        if old_config is None:
            sys.modules.pop("web.wifi_config", None)
        else:
            sys.modules["web.wifi_config"] = old_config


Client = import_client()


class FakeReader:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        await asyncio.sleep(0)
        return self._lines.pop(0) if self._lines else b""


class BlockingReader:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def readline(self):
        self.started.set()
        await self.release.wait()
        return b"HTTP/1.1 200 OK\r\n"


class FakeWriter:
    def __init__(self):
        self.writes = []
        self.drain_calls = 0
        self.closed = False
        self.wait_closed_calls = 0

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        self.drain_calls += 1
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.wait_closed_calls += 1
        await asyncio.sleep(0)


class ConnectionFactory:
    def __init__(self, reader=None, writer=None, error=None):
        self.reader = reader
        self.writer = writer
        self.error = error
        self.calls = []

    async def __call__(self, host, port):
        self.calls.append((host, port))
        if self.error is not None:
            raise self.error
        return self.reader, self.writer


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def response_reader(self, status=b"201 Created"):
        return FakeReader([
            b"HTTP/1.1 " + status + b"\r\n",
            b"Content-Length: 0\r\n",
            b"\r\n",
        ])

    async def test_report_uses_async_http_and_serialized_payload(self):
        writer = FakeWriter()
        connection = ConnectionFactory(self.response_reader(), writer)
        client = Client(
            host="api.example:8080",
            open_connection=connection,
        )

        code = await client.report('{"lux": 12}')

        request = b"".join(writer.writes)
        self.assertEqual(code, 201)
        self.assertEqual(connection.calls, [("api.example", 8080)])
        self.assertIn(b"POST /api/v1/readings HTTP/1.1\r\n", request)
        self.assertIn(b"Host: api.example:8080\r\n", request)
        self.assertIn(b"Content-Type: application/json\r\n", request)
        self.assertIn(b"Content-Length: 11\r\n", request)
        self.assertTrue(request.endswith(b'\r\n\r\n{"lux": 12}'))
        self.assertEqual(writer.drain_calls, 1)
        self.assertTrue(writer.closed)
        self.assertEqual(writer.wait_closed_calls, 1)

    async def test_ping_preserves_health_endpoint_and_status_code(self):
        writer = FakeWriter()
        connection = ConnectionFactory(
            self.response_reader(status=b"204 No Content"),
            writer,
        )
        client = Client(host="api.example", open_connection=connection)

        code = await client.ping()

        request = b"".join(writer.writes)
        self.assertEqual(code, 204)
        self.assertEqual(connection.calls, [("api.example", 80)])
        self.assertTrue(request.startswith(b"GET /healthz HTTP/1.1\r\n"))
        self.assertNotIn(b"Content-Length", request)
        self.assertTrue(writer.closed)

    async def test_optional_http_prefix_is_not_sent_in_host_header(self):
        writer = FakeWriter()
        connection = ConnectionFactory(self.response_reader(), writer)
        client = Client(
            host="http://api.example:8080",
            open_connection=connection,
        )

        await client.report("{}")

        request = b"".join(writer.writes)
        self.assertEqual(connection.calls, [("api.example", 8080)])
        self.assertIn(b"Host: api.example:8080\r\n", request)

    async def test_known_socket_errors_are_translated(self):
        from web.exceptions import (
            ErrConnectionReset,
            ErrHostUnreachable,
            ErrTimedOut,
        )

        cases = (
            (errno.EHOSTUNREACH, ErrHostUnreachable),
            (errno.ETIMEDOUT, ErrTimedOut),
            (errno.ECONNRESET, ErrConnectionReset),
        )

        for error_number, expected in cases:
            with self.subTest(error_number=error_number):
                connection = ConnectionFactory(error=OSError(error_number, "socket"))
                client = Client(host="api.example", open_connection=connection)
                with self.assertRaises(expected):
                    await client.report("{}")

    async def test_unknown_socket_error_is_translated_to_base_network_error(self):
        from web.exceptions import ErrNetwork

        connection = ConnectionFactory(error=OSError(errno.EIO, "socket"))
        client = Client(host="api.example", open_connection=connection)

        with self.assertRaises(ErrNetwork):
            await client.report("{}")

    async def test_protocol_error_closes_connection_and_remains_unexpected(self):
        writer = FakeWriter()
        reader = FakeReader([b"not-http\r\n"])
        client = Client(
            host="api.example",
            open_connection=ConnectionFactory(reader, writer),
        )

        with self.assertRaisesRegex(ValueError, "Invalid HTTP status line"):
            await client.report("{}")

        self.assertTrue(writer.closed)
        self.assertEqual(writer.wait_closed_calls, 1)

    async def test_cancellation_closes_connection_and_propagates(self):
        writer = FakeWriter()
        reader = BlockingReader()
        client = Client(
            host="api.example",
            open_connection=ConnectionFactory(reader, writer),
        )
        task = asyncio.create_task(client.report("{}"))
        await asyncio.wait_for(reader.started.wait(), timeout=0.25)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(writer.closed)
        self.assertEqual(writer.wait_closed_calls, 1)


if __name__ == "__main__":
    unittest.main()
