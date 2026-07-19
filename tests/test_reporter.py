import asyncio
import unittest

from lib.async_channel import SingleValueChannel
from web.exceptions import ErrTimedOut
from web.reporter import Reporter


class BlockingFirstClient:
    def __init__(self):
        self.calls = []
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.second_finished = asyncio.Event()

    async def ping(self):
        return 204

    async def report(self, payload):
        self.calls.append(payload)
        if len(self.calls) == 1:
            self.first_started.set()
            await self.release_first.wait()
        else:
            self.second_finished.set()
        return 201


class OutcomeClient:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []
        self.success = asyncio.Event()

    async def ping(self):
        return 204

    async def report(self, payload):
        self.calls.append(payload)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.success.set()
        return outcome


class ReporterTests(unittest.IsolatedAsyncioTestCase):
    async def _cancel(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_ping_is_delegated_to_owned_client(self):
        reporter = Reporter(BlockingFirstClient(), SingleValueChannel())

        self.assertEqual(await reporter.ping(), 204)

    async def test_only_latest_pending_payload_is_delivered(self):
        client = BlockingFirstClient()
        reporter = Reporter(client, SingleValueChannel())
        task = asyncio.create_task(reporter.run())

        await reporter.submit("first")
        await asyncio.wait_for(client.first_started.wait(), timeout=0.25)
        await reporter.submit("superseded")
        await reporter.submit("latest")
        client.release_first.set()
        await asyncio.wait_for(client.second_finished.wait(), timeout=0.25)

        self.assertEqual(client.calls, ["first", "latest"])
        await self._cancel(task)

    async def test_network_failure_is_dropped_and_next_payload_is_attempted(self):
        client = OutcomeClient([ErrTimedOut(), 202])
        failure_observed = asyncio.Event()
        reporter = Reporter(
            client,
            SingleValueChannel(),
            failure_observed.set,
        )
        task = asyncio.create_task(reporter.run())

        await reporter.submit("failed")
        await asyncio.wait_for(failure_observed.wait(), timeout=0.25)
        reporter.raise_if_failed()
        await reporter.submit("next")
        await asyncio.wait_for(client.success.wait(), timeout=0.25)

        self.assertEqual(client.calls, ["failed", "next"])
        self.assertFalse(task.done())
        await self._cancel(task)

    async def test_network_failure_is_not_retried(self):
        client = OutcomeClient([ErrTimedOut()])
        failure_observed = asyncio.Event()
        reporter = Reporter(
            client,
            SingleValueChannel(),
            failure_observed.set,
        )
        task = asyncio.create_task(reporter.run())

        await reporter.submit("failed-once")
        await asyncio.wait_for(failure_observed.wait(), timeout=0.25)
        await asyncio.sleep(0)

        self.assertEqual(client.calls, ["failed-once"])
        self.assertFalse(task.done())
        await self._cancel(task)

    async def test_unexpected_failure_stops_reporter_and_is_visible(self):
        failure = ValueError("malformed response")
        client = OutcomeClient([failure])
        reporter = Reporter(client, SingleValueChannel())
        task = asyncio.create_task(reporter.run())

        await reporter.submit("payload")
        await asyncio.wait_for(task, timeout=0.25)

        self.assertIs(reporter.failure, failure)
        with self.assertRaisesRegex(ValueError, "malformed response"):
            reporter.raise_if_failed()

    async def test_cancellation_propagates(self):
        reporter = Reporter(BlockingFirstClient(), SingleValueChannel())
        task = asyncio.create_task(reporter.run())
        await asyncio.sleep(0)

        await self._cancel(task)
        self.assertIsNone(reporter.failure)


if __name__ == "__main__":
    unittest.main()
