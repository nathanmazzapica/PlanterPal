"""Verify the MicroPython asyncio behavior used by the application.

Run this file directly from the host without copying it to the device:

    mpremote connect <port> run tests/hardware/asyncio_contract_probe.py
"""

import asyncio
import sys


async def return_value():
    await asyncio.sleep_ms(10)
    return 42


async def wait_for_event(event):
    await event.wait()
    return "released"


async def mark_scheduled(event):
    event.set()


async def wait_then_mark(wait_for, completed):
    await wait_for.wait()
    completed.set()


async def cancellable_worker(started):
    try:
        started.set()

        while True:
            await asyncio.sleep_ms(100)
    finally:
        print("worker finally block executed")


async def deadline_worker(started, cancelled):
    started.set()
    try:
        while True:
            await asyncio.sleep_ms(100)
    except asyncio.CancelledError:
        cancelled.set()
        raise


async def acquire_lock(lock, acquired):
    async with lock:
        acquired.set()


async def hold_lock(lock, started):
    async with lock:
        started.set()

        while True:
            await asyncio.sleep_ms(100)


async def expect_cancelled(task, message):
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        return

    raise AssertionError(message)


async def assert_lock_reacquirable(lock):
    acquired = asyncio.Event()
    task = asyncio.create_task(acquire_lock(lock, acquired))

    for _ in range(100):
        if acquired.is_set():
            await task
            return

        await asyncio.sleep_ms(10)

    await expect_cancelled(task, "Timed-out Lock waiter did not cancel")
    raise AssertionError("Lock could not be reacquired after cancellation")


async def main():
    print("Testing Event...")

    event = asyncio.Event()
    waiter = asyncio.create_task(wait_for_event(event))

    await asyncio.sleep_ms(20)
    event.set()

    result = await waiter
    assert result == "released"
    print("PASS: Event wakes a waiting task")

    print("Testing Event clear...")

    event.set()
    event.clear()
    completed = asyncio.Event()
    waiter = asyncio.create_task(wait_then_mark(event, completed))
    await asyncio.sleep_ms(20)

    assert not completed.is_set()
    event.set()
    await waiter

    assert completed.is_set()
    print("PASS: cleared Event blocks until it is set again")

    print("Testing zero-delay cooperative yield...")

    scheduled = asyncio.Event()
    task = asyncio.create_task(mark_scheduled(scheduled))
    assert not scheduled.is_set()
    await asyncio.sleep_ms(0)

    assert scheduled.is_set()
    await task
    print("PASS: sleep_ms(0) yields to another ready task")

    print("Testing task result...")

    task = asyncio.create_task(return_value())
    result = await task

    assert result == 42
    print("PASS: task can be awaited and returns its value")

    print("Testing cancellation...")

    started = asyncio.Event()
    task = asyncio.create_task(cancellable_worker(started))

    await started.wait()
    await expect_cancelled(task, "Cancelled task did not raise CancelledError")
    print("PASS: awaiting cancelled task raises CancelledError")

    print("Testing wait_for deadline expiry...")

    deadline_started = asyncio.Event()
    deadline_cancelled = asyncio.Event()
    try:
        await asyncio.wait_for(
            deadline_worker(deadline_started, deadline_cancelled),
            0.02,
        )
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        raise AssertionError("wait_for timeout looked like external cancellation")
    else:
        raise AssertionError("wait_for did not enforce its finite timeout")

    assert deadline_started.is_set()
    assert deadline_cancelled.is_set()
    print("PASS: wait_for timeout cancels its worker and raises TimeoutError")

    print("Testing external cancellation around wait_for...")

    external_started = asyncio.Event()
    external_cancelled = asyncio.Event()
    wrapped = asyncio.create_task(
        asyncio.wait_for(
            deadline_worker(external_started, external_cancelled),
            10,
        )
    )
    await external_started.wait()
    await expect_cancelled(
        wrapped,
        "external cancellation around wait_for did not propagate",
    )
    assert external_cancelled.is_set()
    print("PASS: external wait_for cancellation remains CancelledError")

    print("Testing Lock mutual exclusion...")

    lock = asyncio.Lock()
    await lock.acquire()

    acquired = asyncio.Event()
    waiter = asyncio.create_task(acquire_lock(lock, acquired))
    await asyncio.sleep_ms(20)

    assert not acquired.is_set()
    lock.release()
    await waiter

    assert acquired.is_set()
    print("PASS: Lock excludes a second task until released")

    print("Testing cancellation while waiting for Lock...")

    await lock.acquire()
    acquired = asyncio.Event()
    waiter = asyncio.create_task(acquire_lock(lock, acquired))
    await asyncio.sleep_ms(20)

    await expect_cancelled(waiter, "Cancelled Lock waiter did not raise CancelledError")
    assert not acquired.is_set()
    lock.release()
    await assert_lock_reacquirable(lock)
    print("PASS: cancelling a Lock waiter does not acquire or corrupt the Lock")

    print("Testing cancellation while holding Lock...")

    started = asyncio.Event()
    holder = asyncio.create_task(hold_lock(lock, started))
    await started.wait()

    await expect_cancelled(holder, "Cancelled Lock holder did not raise CancelledError")
    await assert_lock_reacquirable(lock)
    print("PASS: async-with releases Lock when its holder is cancelled")

    print("ALL ASYNCIO CONTRACT TESTS PASSED")


print("Firmware implementation:", getattr(sys, "implementation", "unknown"))
print("Firmware platform:", getattr(sys, "platform", "unknown"))
asyncio.run(main())
