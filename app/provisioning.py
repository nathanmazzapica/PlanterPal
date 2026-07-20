import asyncio

from web.credentials import Credentials


class ProvisioningCoordinator:
    """Own the connect-before-persist provisioning transaction.

    BLE owns transport and NetworkManager owns the station interface. This
    coordinator is the only runtime component allowed to join those concerns
    by asking NetworkManager to test a candidate and persisting it only after
    the candidate has both associated and obtained an IP address.
    """

    def __init__(self, network_manager, credential_store, request_channel):
        self._network_manager = network_manager
        self._credential_store = credential_store
        self._request_channel = request_channel

        self.provisioned = asyncio.Event()
        self._failure = None
        self._running = False

    @property
    def failure(self):
        return self._failure

    @property
    def running(self):
        return self._running

    def raise_if_failed(self):
        if self._failure is not None:
            raise self._failure

    async def run(self):
        if self._running:
            raise RuntimeError("ProvisioningCoordinator is already running")

        self._running = True
        self._failure = None
        self.provisioned.clear()

        try:
            while True:
                request = await self._request_channel.get()
                if request.cancelled:
                    continue

                if await self._handle_request(request):
                    self.provisioned.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failure = error
        finally:
            self._running = False

    def clear_credentials(self):
        """Clear durable and in-memory credentials while provisioning is idle."""

        if self._running:
            raise RuntimeError("cannot clear credentials while provisioning")
        if getattr(self._network_manager, "busy", False):
            raise RuntimeError("cannot clear credentials while WiFi is active")

        # Commit the durable removal before mutating the in-memory owner. If
        # flash fails, the known-good running configuration remains intact.
        self._credential_store.clear()
        self._network_manager.forget_active_credentials()

    async def _handle_request(self, request):
        credentials = Credentials(request.ssid, request.password)
        attempt = self._create_task(
            self._network_manager.try_credentials(credentials)
        )
        cancel_attempt = None

        try:
            cancel_attempt = self._create_task(
                self._cancel_on_disconnect(request, attempt)
            )
            try:
                result = await attempt
            except asyncio.CancelledError:
                # Do not rely on an event-loop-specific rule about whether
                # cancelling this coordinator also cancels an awaited Task.
                # This coordinator created the candidate attempt, so it owns
                # settling that task before it exits.
                try:
                    await self._cancel_task(attempt)
                finally:
                    attempt = None
                if request.cancelled:
                    return False
                raise
            except Exception:
                attempt = None
                raise
            else:
                attempt = None
        finally:
            try:
                if cancel_attempt is not None:
                    await self._cancel_task(cancel_attempt)
            finally:
                # If task creation for the disconnect watcher failed, the
                # already-created candidate remains ours to settle.
                if attempt is not None:
                    await self._cancel_task(attempt)

        if request.cancelled:
            if result.success:
                self._network_manager.forget_active_credentials()
            return False

        if not result.success:
            request.fail(result.reason)
            await request.wait_response_sent()
            return False

        try:
            self._credential_store.save(credentials)
        except OSError:
            self._network_manager.forget_active_credentials()
            if not request.cancelled:
                request.fail("storage_failed")
                await request.wait_response_sent()
            return False
        except Exception:
            self._network_manager.forget_active_credentials()
            raise

        # No await occurs between the durable commit and completion, so BLE
        # cannot interleave another request or a disconnect callback here.
        request.succeed()
        await request.wait_response_sent()
        return True

    @staticmethod
    async def _cancel_on_disconnect(request, attempt):
        await request.wait_cancelled()
        attempt.cancel()

    @staticmethod
    async def _cancel_task(task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _create_task(coroutine):
        """Create a task without leaking its coroutine on allocation failure."""

        try:
            return asyncio.create_task(coroutine)
        except BaseException:
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            raise
