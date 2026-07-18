"""Verify the MicroPython asyncio behavior used by the application.

Run this file directly from the host without copying it to the device:

    mpremote connect <port> run tests/hardware/asyncio_contract_probe.py
"""

import asyncio


async def return_value():
    await asyncio.sleep_ms(10)
    return 42


async def wait_for_event(event):
    await event.wait()
    return "released"


async def cancellable_worker(started):
    try:
        started.set()

        while True:
            await asyncio.sleep_ms(100)
    finally:
        print("worker finally block executed")


async def main():
    print("Testing Event...")

    event = asyncio.Event()
    waiter = asyncio.create_task(wait_for_event(event))

    await asyncio.sleep_ms(20)
    event.set()

    result = await waiter
    assert result == "released"
    print("PASS: Event wakes a waiting task")

    print("Testing task result...")

    task = asyncio.create_task(return_value())
    result = await task

    assert result == 42
    print("PASS: task can be awaited and returns its value")

    print("Testing cancellation...")

    started = asyncio.Event()
    task = asyncio.create_task(cancellable_worker(started))

    await started.wait()
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        print("PASS: awaiting cancelled task raises CancelledError")
    else:
        raise AssertionError("Cancelled task did not raise CancelledError")

    print("ALL ASYNCIO CONTRACT TESTS PASSED")


asyncio.run(main())
