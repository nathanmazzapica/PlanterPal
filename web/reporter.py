import asyncio

from web.exceptions import ErrHttpStatus, ErrNetwork


class Reporter:
    """Owns backend delivery and consumes immutable payloads one at a time."""

    def __init__(self, client, channel, on_network_error=None):
        self._client = client
        self._channel = channel
        self._on_network_error = on_network_error
        self._failure = None
        self._running = False

    @property
    def failure(self):
        return self._failure

    def raise_if_failed(self):
        if self._failure is not None:
            raise self._failure

    async def ping(self):
        return await self._client.ping()

    async def submit(self, payload):
        await self._channel.put(payload)

    async def run(self):
        if self._running:
            raise RuntimeError("Reporter is already running")

        self._running = True
        self._failure = None

        try:
            while True:
                payload = await self._channel.get()

                try:
                    await self._client.report(payload)
                except ErrNetwork as error:
                    if isinstance(error, ErrHttpStatus):
                        print("Report rejected; dropping payload:", error)
                    else:
                        print("Report delivery failed; dropping payload:", error)
                        if self._on_network_error is not None:
                            self._on_network_error()

                    # Failed reports are intentionally not retried for now.
                    # Readings accumulate, so a later payload generally
                    # supersedes a failed one. Revisit this when daily rollover
                    # is implemented: a failed final pre-rollover delivery
                    # could lose that day's final accumulation.
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failure = error
        finally:
            self._running = False
