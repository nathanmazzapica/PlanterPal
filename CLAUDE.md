# CLAUDE.md

## Important Rules
NEVER TOUCH BOOT.PY or else terrible things happen and it will be YOUR fault

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PlanterPal — MicroPython firmware for an ESP32 that monitors a planter (light + soil moisture),
shows readings on an I2C LCD, and reports them to an HTTP backend. This runs on-device, not on the
host: there is no build step and no test suite. Code is deployed to the board with `mpremote`, and
the standard library is MicroPython's (`machine`, `neopixel`, `urequests`, `network`, MicroPython's
cut-down `asyncio`), not CPython's.

## Deploying and running

The board enumerates as a CP2102 USB-UART bridge (e.g. `/dev/cu.usbserial-0001` on macOS). Find it
with `mpremote devs`.

```sh
# Copy a changed file to the board (dirs like led/, lib/, web/ already exist on device)
mpremote connect <port> fs cp main.py :main.py

# Copy several at once
mpremote connect <port> fs cp -r main.py :main.py + fs cp -r web/wifi.py :web/wifi.py

# Inspect / manage the device filesystem
mpremote connect <port> fs ls
mpremote connect <port> fs cat lib/ws2811b.py
mpremote connect <port> fs rm :led/led.py

# Run the REPL / see prints from the running program
mpremote connect <port> repl
```

`main.py` is the entrypoint; MicroPython runs `boot.py` then `main.py` on power-up. `main.py` runs
under `asyncio.run(main())` — the event loop must stay live for animated LED states and any BLE
provisioning task to be scheduled.

There is no linter or formatter configured. `pyrightconfig.json` exists only to give the editor
MicroPython stubs (`typings/`); type checking is advisory. Do not add CPython-only tooling
assumptions.

## Config and secrets

- `config.py` — hardware wiring and tuning as module constants: pins (`SCL`=27, `SDA`=26,
  `EK1940_PIN`=32, `STATUS_LED`=GPIO 2), the shared `SENSOR_BUS` I2C(0), loop `INTERVAL_S`, plant
  thresholds (`TEST_PLANT`), `OUTBOX_LEN`. Import as `import config as cfg`.
- `web/wifi_config.py` holds wifi SSID/password and backend `host` in a `cfg` dict. It is
  **gitignored**; `web/wifi_config.example.py` is the template. Anything importing `web.wifi_config`
  will fail on a fresh checkout until it is created.

## Architecture

The data flows one direction each tick: **sensors → State → {Display, Client}**.

- **Sensor drivers** (`lib/`) are thin, stateless-ish wrappers over one device each and return raw
  units: `BH1750.lux()` (I2C, blocks ~0.5s per read for the measurement), `EK1940.moisture()`
  (raw 16-bit ADC). The LCD stack is `PCF8574` (I2C expander) → `HD44780` → `LCD` (vendored from
  Thomascountz's driver).
- **Monitors** (`sensors/light.py`, `sensors/moisture.py`) wrap a driver and hold derived,
  accumulating state. `LightMonitor.update()` integrates lux over time (trapezoidal area between
  samples) into `lux_seconds` and estimates `dli`; `MoistureMonitor.update()` converts the raw ADC
  value to a `moisture_percent` using hardcoded DRY/WET calibration constants.
- **`app/state.py` (`State`)** is the aggregate snapshot. `State.update()` ticks both monitors,
  copies their fields, and derives `plant_status` from moisture thresholds. `to_dict()`/`to_json()`
  are the wire format sent to the backend — keep them in sync with what the API expects.
- **`display/display.py` (`Display`)** renders a `State` to the 2x16 LCD and has helpers for status
  lines and error messages (`display_err`). It is a pure sink; it never reads back.
- **`web/client.py` (`Client`)** posts `state.to_json()` to `POST /api/v1/readings` and pings
  `GET /healthz`. OSError from the socket layer is translated into the typed exceptions in
  `web/exceptions.py` (all subclass `ErrNetwork`), so callers catch `ErrNetwork` and its subtypes,
  not raw `OSError`. HTTP calls are synchronous (`urequests`) and briefly block the event loop.
- **`led/controller.py` (`Controller`)** maps a hardcoded device-state string to an LED behavior on
  the `WS2811B` pixel: `"provisioning"` = blue fade (async `blink()` task), `"ready"` = solid green,
  `"error"` = solid red. State is stored in `_state` and driven through `set_state()`. States are
  bare strings on purpose — an enum is a deliberate later step, so do not introduce one unprompted.
  `STATUS_LED` in `config.py` is a *separate*, simple on/off GPIO LED used to signal backend
  reachability in the main loop.

### Async and MicroPython gotchas

- MicroPython's `asyncio` lacks `asyncio.Queue`; `lib/async_channel.py` (`SingleValueChannel`) is the
  in-house single-slot replacement built on `asyncio.Event`, used to hand wifi credentials from BLE
  provisioning to the main flow.
- Anything that blocks (a raw `time.sleep`, a synchronous HTTP call) stalls every other task,
  including LED animations. Prefer `await asyncio.sleep(...)`; `wifi.connect_wifi` is async for this
  reason (it awaits while polling `wlan.isconnected()`).
- **BLE provisioning** (`lib/ble_provisioning.py`, `run_provisioning`) advertises as "PlanterPal",
  receives wifi credentials as JSON over a GATT characteristic, and pushes them onto a channel. It
  needs `aioble` on the device and is not yet wired into `main.py`.

### In-flight / not yet wired

`web/outbox.py` (`Outbox`, a bounded `deque` of `State`s to retry failed sends) and the BLE
provisioning path exist but are not integrated into `main.py` yet, and carry `TODO`s. `main.py`
currently reports directly via `Client` inside the main loop. Treat these as the current direction
of travel rather than finished infrastructure.

## Conventions

- Commits follow Conventional Commits (`feat:`, `refactor:`, `docs:`). Keep to that style.
- Hardware constants live in `config.py`, not inline in modules.
