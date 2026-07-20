# AGENTS.md

## Important rules

NEVER TOUCH `boot.py`. MicroPython executes it before `main.py`, and recovery
depends on leaving it unchanged.

This repository contains PlanterPal, MicroPython firmware for an ESP32. It
monitors light and soil moisture, renders readings to a development-only I2C
LCD, reports to an HTTP backend, and provisions Wi-Fi credentials over BLE.
Code runs on the device; there is no build step.

## Runtime and compatibility

- Target runtime: MicroPython on ESP32, currently exercised on
  `ESP32_GENERIC` MicroPython 1.28.0.
- Device APIs include `machine`, `network`, `neopixel`, `esp32.NVS`, and
  MicroPython's reduced `asyncio` and stream implementations.
- Do not introduce CPython-only runtime assumptions. Host tests install fake
  device modules where necessary.
- `pyrightconfig.json` exists for editor stubs only. No formatter or linter is
  configured.
- `aioble` must be installed on the ESP32 for provisioning:

  ```sh
  mpremote connect <port> mip install aioble
  ```

- The optional host BLE client requires Bleak:

  ```sh
  python3 -m pip install bleak
  ```

## Boot and mode selection

`main.py` is deliberately small. Every boot:

1. waits cooperatively for a five-second serial recovery period;
2. loads credentials through `CredentialStore`;
3. imports exactly one mode-specific graph;
4. runs that graph under `asyncio.run(main())`.

A credentialed boot imports `app/application.py` and never imports BLE or
provisioning dependencies. An uncredentialed boot imports
`app/provisioning_runtime.py` and never imports the display, sensors, HTTP
client, reporter, application state, or NeoPixel controller.

The separation is a memory invariant, not an optimization. ESP32 NimBLE may
fail to reserve controller heap if the running graph was imported first. The
provisioning runtime therefore constructs one inactive station handle before
starting NimBLE, gives NimBLE a cooperative settling period, and injects that
same handle into `NetworkManager`. Successful provisioning cleans up and resets
the device; running mode begins only in the fresh interpreter on the next boot.

Ordinary Wi-Fi loss in running mode stays inside `NetworkManager`'s reconnect
loop. It must not load BLE, clear credentials, or reset the machine. To change
credentials, explicitly clear the stored record and reset into provisioning.

## Architectural invariants

Preserve these rules when changing the firmware:

- Only `NetworkManager` may activate, connect, disconnect, or otherwise mutate
  the station interface, its connection events, or its active in-memory
  credentials. Provisioning runtime may allocate the inactive WLAN handle but
  may not operate it. Transient station interface read failures enter the
  ordinary reconnect/backoff path; successful state is published only after
  association and a non-zero IP. Consumers observe the monotonic connection
  version and never clear or set the owned connection Event directly.
- BLE provisioning owns GATT transport and credential-request parsing. It
  submits credential candidates through the single-slot channel; it does not
  connect Wi-Fi or write NVS directly.
- `ProvisioningCoordinator` is the only normal runtime component that joins
  BLE requests, candidate Wi-Fi testing, and credential persistence.
- Credentials may be persisted only after `NetworkManager` confirms both Wi-Fi
  association and a non-zero IP address. A failed or cancelled candidate must
  not replace the previous active or durable credentials.
- `CredentialStore` exclusively owns the versioned NVS record. Credentials are
  immutable values, passwords stay redacted in representations, and malformed,
  unsupported, or oversized records load as unprovisioned rather than partly
  trusted data.
- Provisioning and running modes are mutually exclusive. Do not remove their
  reset/import boundary without resolving the measured heap constraint.
- All components using I2C bus 0 must receive the same `asyncio.Lock` instance.
  Hold it only around physical bus transactions; never hold it across the
  BH1750 measurement delay or LCD marquee delay.
- `Display` exclusively owns the LCD stack and serializes LCD commands through
  its task. Other components submit immutable values or direct critical
  commands; they do not write the LCD driver themselves.
- `Application` selects exactly one display implementation per running-mode
  boot. LCD presence is probed under the shared I2C lock. Address absence or an
  initialization `OSError` selects `NullDisplay`; a scan failure, unexpected
  initialization exception, or post-readiness display failure remains fatal.
  The failed real task must be settled before the null task starts.
- `NullDisplay` owns no hardware and performs no I2C access. It preserves the
  display lifecycle and command interface so the rest of `Application` has no
  headless conditionals.
- `Controller` exclusively writes the running-mode NeoPixel. It owns its
  animation task, cancelling and awaiting it before every transition. Current
  states are bare strings by design:
  `"connecting"` fades cyan, `"ready"` is solid green, and `"error"` is solid
  red. The legacy `"provisioning"` blue fade remains supported but is not used
  by the memory-constrained provisioning graph. `Application` observes network
  state and requests LED transitions without mutating either owner. Deliberate
  cancellation leaves the pixel off; fatal application failure leaves red
  latched until reset or power cycle.
- `ProvisioningIndicator` exclusively owns the separate GPIO2 LED while BLE is
  ready. It uses hardware PWM, is best-effort, and must be stopped/deinitialized
  on every cleanup path. Indicator failure must never block credential recovery.
- `Reporter` exclusively owns backend delivery. Producers submit immutable
  payload strings over a `SingleValueChannel`; a newer unsent payload replaces
  the older one. Failed reports are intentionally discarded for now. Preserve
  the explicit comment acknowledging the unresolved final pre-rollover loss
  case until daily rollover is implemented.
- Every HTTP request consumes one finite monotonic deadline from
  `HTTP_REQUEST_TIMEOUT_S`; individual connection, drain, response, header, and
  shutdown phases must not restart it. Expiry raises `ErrTimedOut`, closes the
  writer best-effort, and must remain distinguishable from external task
  cancellation.
- `Client` accepts only `2xx` responses and raises `ErrHttpStatus` for backend
  rejection without including bodies or payloads. `Reporter` drops a rejected
  report once and continues, but does not mark the reachability GPIO offline;
  only transport errors do. A rejected startup health check prevents the
  NeoPixel from entering `"ready"`.
- The monitors own their accumulating sensor state. `State.update()` is the
  application-level update boundary and copies a coherent snapshot for display
  and reporting.
- Owned long-running tasks must be cancelled and awaited during shutdown.
  `asyncio.CancelledError` is control flow: propagate it rather than rendering
  it as an operational failure. Background failures are recorded and surfaced
  through `raise_if_failed()`.

## Current component model

- `config.py` contains side-effect-free pin numbers and policy values.
  `device_hardware.py` constructs running-mode GPIO and I2C objects. Never
  import `device_hardware` from provisioning.
- `web/credentials.py` owns immutable `Credentials` values and the
  `CredentialStore` NVS record. Wi-Fi credentials are not source configuration.
- `web/network_config.py` contains Wi-Fi timing policy without importing
  running hardware configuration.
- `web/wifi.py` owns connection attempts, typed results, monitoring, and
  reconnect backoff.
- `lib/ble_bootstrap.py`, `lib/ble_provisioning.py`, `app/provisioning.py`, and
  `app/provisioning_runtime.py` form the import-isolated provisioning path.
- `lib/async_channel.py` provides `SingleValueChannel`, because MicroPython
  `asyncio` has no `Queue`. It has one replaceable slot, not FIFO semantics.
- `lib/bh1750.py` issues the sensor command under the shared I2C lock, releases
  the lock during the cooperative 0.5-second measurement wait, then reacquires
  it for the read.
- `display/display.py` runs as a sink task. Replaceable reading frames share a
  channel; startup/error line commands wait for confirmed completion.
- `display/probe.py` performs the once-per-boot LCD address check under the
  composition-owned I2C lock. `display/null_display.py` is the selected sink
  when the optional debug LCD is absent or unavailable during initialization.
- `web/client.py` uses `asyncio.open_connection` for cooperative HTTP/1.1
  requests. It writes and drains through the stream API, reads the response
  status and headers asynchronously, and translates socket `OSError`s to the
  typed exceptions in `web/exceptions.py`. One shared request deadline covers
  all awaited phases and successful responses are restricted to `2xx`.
- `web/reporter.py` runs backend delivery as a separate consumer task.
- `app/application.py` composes the running graph, creates the single shared I2C
  lock, starts and stops component tasks, and owns the main sensor/update loop.
- The 2x16 LCD is primarily a development/debug accessory and would not be
  populated on a production device. LED lifecycle indication is therefore
  more important than the LCD for production-facing behavior.
- `web/wifi_config.py`, when present, is a gitignored legacy backend-host
  fallback. New deployments set non-secret `API_HOST` in `config.py`; NVS owns
  Wi-Fi credentials.

The running data path is:

```text
BH1750 + EK1940 -> monitors -> State -> Display
                                      -> Reporter -> Client
```

## Testing

There is a CPython host test suite. Run it before hardware probes:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

Hardware probes run directly from the host with the deployed device modules:

```sh
mpremote connect <port> run tests/hardware/asyncio_contract_probe.py
mpremote connect <port> run tests/hardware/display_hardware_probe.py
mpremote connect <port> run tests/hardware/optional_display_hardware_probe.py
mpremote connect <port> run tests/hardware/network_hardware_probe.py
mpremote connect <port> run tests/hardware/network_led_hardware_probe.py
mpremote connect <port> run tests/hardware/ble_credentials_hardware_probe.py
mpremote connect <port> run tests/hardware/application_composition_hardware_probe.py
```

Read each probe's header before running it. In particular:

- deploy changed dependencies before a probe that imports from the device;
- the asyncio contract probe verifies that firmware timeout expiry is distinct
  from external cancellation and that both settle their owned worker;
- the network probe expects the legacy `web/wifi_config.py` test credentials;
- the network/LED probe uses saved NVS credentials without changing them,
  injects one transient interface-read failure, and visibly exercises
  cyan-green-cyan-green-red before deliberate cleanup turns the pixel off;
- the BLE credential probe uses and cleans only its disposable `pp_probe` NVS
  namespace;
- the composition probe refuses persistence and preserves production
  credentials;
- the display probe changes LCD contents and takes a real BH1750 reading.
- the optional-display probe must be run in separate powered-off attachment
  configurations; live I2C hot-plugging is unsupported.

To deliberately clear production credentials:

```sh
mpremote connect <port> run tests/hardware/reset_credentials_hardware.py
mpremote connect <port> reset
```

This is destructive to the saved Wi-Fi record. The reset script does not reset
the board itself.

Provision an uncredentialed device from the host with:

```sh
python3 tools/ble_provision_client.py --ssid "Garden WiFi"
```

The client prompts for the password without echo. See `README.md` for protocol,
security, and deployment details.

## Deploying and recovery

The board normally enumerates as a CP2102 USB-UART bridge, for example
`/dev/cu.usbserial-0001` on macOS. Find it with `mpremote devs`.

```sh
# Copy a changed file; destination directories must already exist.
mpremote connect <port> fs cp app/application.py :app/application.py

# Chain several copies in one session.
mpremote connect <port> fs cp config.py :config.py + fs cp web/wifi.py :web/wifi.py

# Inspect the device filesystem.
mpremote connect <port> fs ls
mpremote connect <port> fs cat :lib/ws2811b.py

# Open the REPL.
mpremote connect <port> repl
```

Install supporting modules and verify their hardware probes before replacing
`main.py`. Copy `main.py` last so an incomplete deployment cannot select a mode
whose dependencies are missing. Do not copy or edit `boot.py`.

Opening the CP2102 port can reset the board. Use the recovery period when raw
REPL entry would otherwise hang:

```sh
mpremote connect <port> sleep 1 fs ls
mpremote connect <port> sleep 1 reset
```

## Conventions

- Commits use Conventional Commit prefixes such as `feat:`, `fix:`, `test:`,
  `refactor:`, and `docs:`. Keep unrelated concerns in separate commits.
- Preserve unrelated user changes in a dirty worktree.
- Use `import config as cfg` for configuration.
- Put new hardware assignments and policy constants in `config.py`; construct
  running-mode hardware in `device_hardware.py`. The running NeoPixel is
  currently composed on GPIO21 in `app/application.py`.
- Do not introduce a device-state enum unless explicitly requested; state
  strings are intentional at this migration stage.
