# Async Architecture Mentoring Quiz

This exercise is based on PlanterPal's current asynchronous firmware
architecture. The most relevant source files are:

- `main.py`
- `app/application.py`
- `app/provisioning_runtime.py`
- `web/wifi.py`
- `display/display.py`
- `lib/async_channel.py`

For each answer, identify:

- The owner of each resource or state
- The invariant being protected
- The appropriate coordination mechanism
- Cancellation and failure behavior
- At least one rejected alternative and its tradeoff

A useful scoring scale is:

- 1 point for describing behavior
- 2 points for identifying ownership
- 3 points for defending the tradeoff
- 4 points for proposing an adversarial test

## Round 1: Choosing Task Boundaries

1. `Application.run()` creates long-lived tasks for the display, network
   manager, and reporter, but directly awaits `State.update()`. What
   distinguishes an operation that deserves its own task from one that should
   remain part of the caller's sequential flow?

2. Startup currently follows display initialization, Wi-Fi connection,
   backend ping, reporter startup, and then the sampling loop. Which
   dependencies make that ordering necessary? Which stages could safely
   overlap, and what new failure states would overlap introduce?

3. The BH1750 spends approximately half a second converting a measurement
   while the moisture ADC could be sampled almost immediately. Would you
   measure them concurrently? Defend your answer in terms of snapshot
   consistency, task overhead, timing accuracy, cancellation, and hardware
   independence.

4. The LED controller creates an animation task only for animated states. Who
   must own that task? What should happen if the state changes twice before the
   cancelled animation has finished cleaning up?

## Round 2: Shared State and Resources

5. `State.update()` gathers results into local variables before updating its
   public fields. What inconsistent snapshots could consumers observe if
   fields were mutated one at a time while other tasks could read them?

6. The BH1750 and LCD receive the same composition-owned I2C lock. Why would
   giving each driver its own lock fail even though each driver individually
   "uses a lock"?

7. The BH1750 releases the I2C lock during its conversion delay, then
   reacquires it for the read. Why is this potentially safe? What device
   behavior would make it unsafe? How would you decide from a datasheet?

8. `NetworkManager` owns the WLAN interface, credentials, connection flags,
   and reconnect loop. Which of those values may other components read, and
   which operations must they request through the manager instead of
   performing themselves?

## Round 3: Choosing Communication Mechanisms

9. Choose between an event, a single-slot channel, and a direct awaited method
   call for each case, and explain why:

   - Waiting for Wi-Fi to become connected
   - Submitting a new display frame
   - Testing one credential candidate
   - Requesting an immediate error message on the LCD
   - Informing the application that a worker failed

10. A `SingleValueChannel` replaces an unconsumed value with the newest one.
    Why is this useful for display frames and accumulated sensor reports? Why
    could the same behavior be dangerous for commands, credential
    transactions, or audit records?

11. Display renders may be discarded, while critical status and error messages
    wait for completion. Define the guarantees each class of display command
    should have. What prevents a render from replacing a critical command?

12. An event normally represents a condition, not a history of occurrences.
    Which project events are conditions, and which would become incorrect if
    multiple occurrences needed to be counted?

## Round 4: Provisioning and Operating Modes

13. A provisioning transaction spans BLE input, Wi-Fi testing, flash
    persistence, and BLE acknowledgment. Put those operations in order and
    explain the consequences of a disconnect or reset between every adjacent
    pair.

14. The uncredentialed boot delays importing sensors, display, HTTP, and
    application state until after provisioning has completed and the machine
    has reset. Why can import order be an architectural concern on a
    microcontroller but rarely on a desktop application?

15. What does resetting after successful provisioning buy us compared with
    stopping BLE and immediately constructing the running application? What
    costs does the reset introduce?

16. Suppose a running device loses Wi-Fi for two hours. Explain why it should
    normally keep reconnecting without launching BLE or deleting credentials.
    What explicit signal should be required to cross from running mode into
    provisioning mode?

## Round 5: Failures, Cancellation, and Cleanup

17. Distinguish these outcomes and propose different handling for each:

    - Wrong Wi-Fi password
    - Access point temporarily unavailable
    - WLAN driver raises an unexpected exception
    - Credential flash commit fails
    - BLE client disconnects during testing
    - Application task is cancelled during shutdown

18. Some workers record an exception and expose `raise_if_failed()`. Others
    propagate exceptions directly. What are the benefits and risks of each
    model? How long could a recorded reporter or display failure remain
    unnoticed in the current supervision design?

19. If the main operation fails and BLE shutdown also fails, which exception
    should the caller receive? What diagnostics should be retained about the
    cleanup failure?

20. Design a cleanup order for provisioning involving the coordinator, BLE
    provisioner, candidate Wi-Fi connection, and BLE controller. Which
    ordering constraints are invariants, and which are merely preferences?

## Round 6: Timing, Overload, and Validation

21. The reporter currently drops failed deliveries because readings are
    cumulative. Under what assumptions is that valid? How does a daily
    rollover violate those assumptions, and what delivery semantics would then
    be needed?

22. The sampling loop sleeps after completing its work. Compare that with
    scheduling each sample against an absolute deadline. How do work duration,
    clock drift, slow displays, and actual timestamp-based lux integration
    affect the choice?

23. The five-second cooperative recovery period makes serial recovery possible
    before hardware startup. What are its costs? How would you distinguish a
    legitimate recovery window from a boot hang in production?

24. Design adversarial tests for four architectural invariants:

    - Only `NetworkManager` mutates WLAN state.
    - All I2C components use the exact same lock.
    - Failed credentials are never persisted.
    - Running and provisioning components are never active in the same boot.

    Avoid testing method names alone. Describe the hostile scheduling,
    injected failure, or fake resource that would prove each rule is genuinely
    enforced.

## Final Design Challenge

25. Design the asynchronous architecture for an ESP32-based building access
    terminal with these requirements:

    - An NFC reader and external audit flash share one SPI bus.
    - An OLED and real-time clock share one I2C bus.
    - A keypad, door-contact sensor, fire-alarm input, relay, buzzer, and status
      LED operate concurrently.
    - A valid badge should unlock the door within 100 ms and relock it after
      five seconds.
    - A fire-alarm signal must override normal access rules immediately.
    - Wi-Fi synchronizes permissions and uploads audit records, but the
      terminal must work offline for days.
    - Audit records must never be silently discarded; heartbeat telemetry may
      be replaced or dropped.
    - BLE enrollment is permitted only in an explicitly authorized maintenance
      mode.
    - Power can fail at any point, including while writing an audit record.
    - RAM is limited, and network requests can stall or fail.

    Produce a design covering modes, invariants, state ownership, task
    boundaries, priorities, bus locks, queues/events/direct calls, overload
    policy, durable data handling, startup, shutdown, cancellation, reconnect
    behavior, watchdog strategy, and adversarial tests. Explicitly identify
    which operations must remain sequential even though the overall system is
    asynchronous.
