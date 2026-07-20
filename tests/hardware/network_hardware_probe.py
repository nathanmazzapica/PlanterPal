"""Exercise NetworkManager against the ESP32's real WLAN interface.

This script runs from the host and imports the deployed ``web.wifi`` and
``web.wifi_config`` modules from the device filesystem:

    mpremote connect <port> run tests/hardware/network_hardware_probe.py

Turn the configured access point off and on while this probe runs. Increase
``RUN_TIME_S`` for a longer memory-stability test. Set it to a value below the
configured Wi-Fi connection timeout (for example, 5) to exercise cancellation
during an active connection attempt.
"""

import asyncio
import gc
import time

from web.wifi import NetworkManager
from web.wifi_config import cfg as wifi_cfg


RUN_TIME_S = 600
MEMORY_INTERVAL_S = 5


def print_status(manager, started_at):
    gc.collect()

    print(
        "[probe]",
        "elapsed_ms=",
        time.ticks_diff(time.ticks_ms(), started_at),
        "connected=",
        manager.is_connected(),
        "mem_free=",
        gc.mem_free(),
        "mem_alloc=",
        gc.mem_alloc(),
    )


async def stop_network_manager(manager, task):
    print("[probe] cancelling NetworkManager")
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        print("[probe] PASS: cancellation propagated")
    else:
        manager.raise_if_failed()
        raise AssertionError("NetworkManager cancellation did not propagate")

    assert not manager.is_connected()
    print("[probe] PASS: connected Event cleared during cancellation")


async def main():
    manager = NetworkManager(
        wifi_cfg["ssid"],
        wifi_cfg["pw"],
    )
    network_task = asyncio.create_task(manager.run())
    started_at = time.ticks_ms()

    try:
        while time.ticks_diff(time.ticks_ms(), started_at) < RUN_TIME_S * 1000:
            # NetworkManager stores unexpected failures instead of letting its
            # long-running task fail silently. Surface one in this probe.
            manager.raise_if_failed()
            print_status(manager, started_at)
            await asyncio.sleep(MEMORY_INTERVAL_S)
    finally:
        await stop_network_manager(manager, network_task)


asyncio.run(main())
