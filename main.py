import asyncio

from web.credentials import CredentialStore


RECOVERY_PERIOD_S = 5


async def main(recovery_sleep=asyncio.sleep):
    # The CP2102 can reset the ESP32 when a host opens the serial port. Give
    # recovery tools a deterministic, cooperative window in which to deliver
    # KeyboardInterrupt before NVS, BLE, hardware, or application state starts.
    await recovery_sleep(RECOVERY_PERIOD_S)

    credential_store = CredentialStore()
    credentials = credential_store.load()

    if credentials is None:
        # This import boundary is deliberate. A factory-fresh boot must not
        # allocate the display, sensors, application state, HTTP client, or
        # their hardware dependencies before NimBLE has reserved its heap.
        from app.provisioning_runtime import run_provisioning

        await run_provisioning(credential_store)
        return

    # BLE and provisioning modules are never imported on a credentialed boot.
    # The running application owns reconnects for these persisted credentials.
    from app.application import create_application

    application = create_application(credentials)
    await application.run()


if __name__ == "__main__":
    asyncio.run(main())
