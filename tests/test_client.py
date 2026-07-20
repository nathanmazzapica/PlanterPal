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
        line = self._lines.pop(0) if self._lines else b""
        if isinstance(line, BlockingOperation):
            return await line.wait()
        return line


class BlockingOperation:
    def __init__(self, result=None):
        self.result = result
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return self.result


class BlockingReader:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def readline(self):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return b"HTTP/1.1 200 OK\r\n"


class FakeWriter:
    def __init__(self, drain_blocker=None, close_blocker=None):
        self.writes = []
        self.drain_calls = 0
        self.closed = False
        self.wait_closed_calls = 0
        self.drain_blocker = drain_blocker
        self.close_blocker = close_blocker

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        self.drain_calls += 1
        if self.drain_blocker is not None:
            return await self.drain_blocker.wait()
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.wait_closed_calls += 1
        if self.close_blocker is not None:
            return await self.close_blocker.wait()
        await asyncio.sleep(0)


class ConnectionFactory:
    def __init__(self, reader=None, writer=None, error=None, blocker=None):
        self.reader = reader
        self.writer = writer
        self.error = error
        self.blocker = blocker
        self.calls = []

    async def __call__(self, host, port):
        self.calls.append((host, port))
        if self.error is not None:
            raise self.error
        if self.blocker is not None:
            return await self.blocker.wait()
        return self.reader, self.writer


class SequenceClock:
    def __init__(self, values):
        self._values = list(values)

    def __call__(self):
        if not self._values:
            raise AssertionError("deadline requested more clock values than expected")
        return self._values.pop(0)


class RecordingWaitFor:
    def __init__(self):
        self.timeouts = []

    async def __call__(self, awaitable, timeout):
        self.timeouts.append(timeout)
        return await awaitable


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

    def test_request_timeout_is_finite_configurable_and_validated(self):
        import config as cfg

        client = Client(host="api.example")
        self.assertEqual(client.request_timeout_s, cfg.HTTP_REQUEST_TIMEOUT_S)
        self.assertGreater(client.request_timeout_s, 0)

        self.assertEqual(
            Client(host="api.example", request_timeout_s=2.5).request_timeout_s,
            2.5,
        )
        for value in (True, "10", None):
            if value is None:
                continue
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    Client(host="api.example", request_timeout_s=value)
        for value in (0, -1, 0.0001, float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Client(host="api.example", request_timeout_s=value)

    async def test_every_phase_consumes_one_monotonic_transaction_deadline(self):
        writer = FakeWriter()
        connection = ConnectionFactory(self.response_reader(), writer)
        wait_for = RecordingWaitFor()
        clock = SequenceClock([1000, 1000, 1100, 1200, 1300, 1400, 1500])
        client = Client(
            host="api.example",
            open_connection=connection,
            request_timeout_s=1,
            _wait_for=wait_for,
            _ticks_ms_fn=clock,
            _ticks_diff_fn=lambda new, old: new - old,
        )

        self.assertEqual(await client.report("{}"), 201)
        self.assertEqual(
            wait_for.timeouts,
            [1.0, 0.9, 0.8, 0.7, 0.6, 0.5],
        )

    async def test_connection_stall_times_out_and_cancels_connect(self):
        from web.exceptions import ErrTimedOut

        blocker = BlockingOperation()
        client = Client(
            host="api.example",
            open_connection=ConnectionFactory(blocker=blocker),
            request_timeout_s=0.01,
        )

        with self.assertRaises(ErrTimedOut):
            await asyncio.wait_for(client.report("{}"), timeout=0.25)

        self.assertTrue(blocker.started.is_set())
        self.assertTrue(blocker.cancelled.is_set())

    async def test_drain_stall_times_out_and_closes_writer(self):
        await self._assert_stream_phase_timeout(
            writer=FakeWriter(drain_blocker=BlockingOperation()),
            reader=self.response_reader(),
            blocker_name="drain_blocker",
        )

    async def test_status_stall_times_out_and_closes_writer(self):
        blocker = BlockingOperation()
        await self._assert_stream_phase_timeout(
            writer=FakeWriter(),
            reader=FakeReader([blocker]),
            blocker=blocker,
        )

    async def test_header_stall_times_out_and_closes_writer(self):
        blocker = BlockingOperation()
        await self._assert_stream_phase_timeout(
            writer=FakeWriter(),
            reader=FakeReader([b"HTTP/1.1 200 OK\r\n", blocker]),
            blocker=blocker,
        )

    async def test_shutdown_stall_is_part_of_transaction_deadline(self):
        from web.exceptions import ErrTimedOut

        blocker = BlockingOperation()
        writer = FakeWriter(close_blocker=blocker)
        client = Client(
            host="api.example",
            open_connection=ConnectionFactory(self.response_reader(), writer),
            request_timeout_s=0.01,
        )

        with self.assertRaises(ErrTimedOut):
            await asyncio.wait_for(client.report("{}"), timeout=0.25)

        self.assertTrue(writer.closed)
        self.assertEqual(writer.wait_closed_calls, 1)
        self.assertTrue(blocker.cancelled.is_set())

    async def _assert_stream_phase_timeout(
        self,
        writer,
        reader,
        blocker=None,
        blocker_name=None,
    ):
        from web.exceptions import ErrTimedOut

        if blocker is None:
            blocker = getattr(writer, blocker_name)
        client = Client(
            host="api.example",
            open_connection=ConnectionFactory(reader, writer),
            request_timeout_s=0.01,
        )

        with self.assertRaises(ErrTimedOut):
            await asyncio.wait_for(client.report("{}"), timeout=0.25)

        self.assertTrue(blocker.started.is_set())
        self.assertTrue(blocker.cancelled.is_set())
        self.assertTrue(writer.closed)
        self.assertEqual(writer.wait_closed_calls, 0)

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

    async def test_ping_and_report_accept_only_2xx_statuses(self):
        from web.exceptions import ErrHttpStatus, ErrNetwork

        self.assertTrue(issubclass(ErrHttpStatus, ErrNetwork))

        for operation in ("ping", "report"):
            for status_code in (200, 204, 299):
                with self.subTest(operation=operation, status_code=status_code):
                    writer = FakeWriter()
                    reader = self.response_reader(
                        status="{} Result".format(status_code).encode("ascii")
                    )
                    client = Client(
                        host="api.example",
                        open_connection=ConnectionFactory(reader, writer),
                    )
                    if operation == "ping":
                        result = await client.ping()
                    else:
                        result = await client.report('{"password":"hidden"}')
                    self.assertEqual(result, status_code)
                    self.assertTrue(writer.closed)

            for status_code in (400, 404, 500, 503):
                with self.subTest(operation=operation, status_code=status_code):
                    writer = FakeWriter()
                    reader = self.response_reader(
                        status="{} Rejected".format(status_code).encode("ascii")
                    )
                    client = Client(
                        host="api.example",
                        open_connection=ConnectionFactory(reader, writer),
                    )
                    with self.assertRaises(ErrHttpStatus) as raised:
                        if operation == "ping":
                            await client.ping()
                        else:
                            await client.report('{"password":"hidden"}')

                    self.assertEqual(raised.exception.status_code, status_code)
                    self.assertNotIn("hidden", str(raised.exception))
                    self.assertTrue(writer.closed)

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

        self.assertTrue(reader.cancelled.is_set())
        self.assertTrue(writer.closed)
        self.assertEqual(writer.wait_closed_calls, 0)


if __name__ == "__main__":
    unittest.main()
