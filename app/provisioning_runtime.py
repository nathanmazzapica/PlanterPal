"""Minimal, import-isolated runtime for factory provisioning.

The ESP32 cannot reliably hold NimBLE plus the complete running application
graph in this firmware. This module therefore imports only provisioning
dependencies, then resets after a verified credential transaction. The next
boot loads the mutually exclusive running graph from ``app.application``.
"""

import asyncio
import gc


BLE_SETTLE_S = 0.5
MONITOR_INTERVAL_S = 0.1


class ProvisioningRuntime:
    """Own BLE provisioning, candidate Wi-Fi testing, cleanup, and reboot."""

    def __init__(self, credential_store, reset=None):
        self.credential_store = credential_store
        self._reset = reset

        self.station = None
        self.network_manager = None
        self.ble_provisioner = None
        self.coordinator = None
        self.indicator = None

        self._ble_task = None
        self._coordinator_task = None
        self.ready = asyncio.Event()

    async def run(self):
        succeeded = False
        primary_error = None

        # Reserve the station object before NimBLE. NetworkManager receives
        # this exact handle and remains the only component that activates,
        # connects, disconnects, or otherwise mutates Wi-Fi state.
        import network

        self.station = network.WLAN(network.STA_IF)
        gc.collect()

        # Delay BLE imports until the mode decision and station reservation.
        from lib.ble_bootstrap import (
            prepare_ble_controller,
            release_ble_controller,
        )

        try:
            prepare_ble_controller()

            # ESP32 NimBLE starts native controller tasks during config(). Give
            # them one cooperative settling period before allocating Python's
            # remaining provisioning graph.
            await asyncio.sleep(BLE_SETTLE_S)
            gc.collect()

            from lib.ble_provisioning import BleProvisioner
            from lib.async_channel import SingleValueChannel
            from web.wifi import NetworkManager
            from app.provisioning import ProvisioningCoordinator

            request_channel = SingleValueChannel()
            self.network_manager = NetworkManager(wlan=self.station)
            self.ble_provisioner = BleProvisioner(request_channel)
            self.coordinator = ProvisioningCoordinator(
                self.network_manager,
                self.credential_store,
                request_channel,
            )

            self._ble_task = self._create_task(self.ble_provisioner.run())
            self._coordinator_task = self._create_task(self.coordinator.run())
            await self._wait_for_ble_service()
            self._start_indicator_best_effort()
            self.ready.set()

            while not self.coordinator.provisioned.is_set():
                self.ble_provisioner.raise_if_failed()
                self.coordinator.raise_if_failed()
                await asyncio.sleep(MONITOR_INTERVAL_S)

            self.ble_provisioner.raise_if_failed()
            self.coordinator.raise_if_failed()
            succeeded = True
        except BaseException as error:
            primary_error = error

        try:
            await self._cleanup(release_ble_controller)
        except BaseException:
            if primary_error is None:
                raise

        if primary_error is not None:
            raise primary_error

        if succeeded:
            # ProvisioningCoordinator has persisted the verified credentials
            # and waited for BLE's terminal response before setting its event.
            # Reset is the explicit mode boundary; running components are not
            # imported into the constrained provisioning interpreter.
            if self._reset is None:
                from machine import reset

                reset()
            else:
                self._reset()

    async def _cleanup(self, release_ble_controller):
        self.ready.clear()
        cleanup_error = None

        try:
            await self._stop_task("_coordinator_task")
        except BaseException as error:
            cleanup_error = error

        self._stop_indicator_best_effort()

        try:
            await self._stop_task("_ble_task")
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error

        try:
            self._disconnect_network_manager()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error

        try:
            release_ble_controller()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error

        if cleanup_error is not None:
            raise cleanup_error

    async def _wait_for_ble_service(self):
        while self.ble_provisioner.status_characteristic is None:
            self.ble_provisioner.raise_if_failed()
            self.coordinator.raise_if_failed()
            await asyncio.sleep(0)

    def _start_indicator_best_effort(self):
        try:
            import config as cfg
            from led.provisioning_indicator import ProvisioningIndicator

            indicator = ProvisioningIndicator(cfg.STATUS_LED_PIN)
            indicator.start()
            self.indicator = indicator
        except Exception:
            # Local indication is useful but must never make the credential
            # recovery path unavailable. Reclaim any failed import/allocation.
            self.indicator = None
            gc.collect()

    def _stop_indicator_best_effort(self):
        indicator = self.indicator
        self.indicator = None
        if indicator is None:
            return

        try:
            indicator.stop()
        except Exception:
            # Provisioning, BLE, or cancellation remains the primary outcome.
            pass

    async def _stop_task(self, attribute):
        task = getattr(self, attribute)
        if task is None:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            setattr(self, attribute, None)

    def _disconnect_network_manager(self):
        if self.network_manager is None:
            return

        disconnect = getattr(self.network_manager, "disconnect", None)
        if callable(disconnect):
            disconnect()

    @staticmethod
    def _create_task(coroutine):
        try:
            return asyncio.create_task(coroutine)
        except BaseException:
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            raise


async def run_provisioning(credential_store):
    await ProvisioningRuntime(credential_store).run()
