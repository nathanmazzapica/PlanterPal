# PlanterPal

PlanterPal is an IoT project that started when I built a planter for my succulent then thought:

> "Hey I wonder exactly how much sun this guy is getting throughout the day"

With that thought I set off to teach myself some basics of using microcontrollers and sensors. From there
I've just kept following my curiosity. I went from just wanting a simple on-board csv logging system to
building a small backend the device could send updates too. The next steps include a tiny mobile app, and designing
a PCB and a functional, weather-proof enclosure so that I can actually keep this thing outside 24/7 in my planter.

As of writing this I have 0 experience with CAD, 3D printing, or PCB design so it might be a little while but I'll get there.

Anything for my succulent

## Project Setup

### Components
This project uses a standard ESP32 board, a BH1750 Lux sensor, an EK1940 capacitive soil moisture sensor,
and, during development, a 2x16 character LCD with an I2C backpack. The LCD is primarily a debugging
accessory and would not be populated on a hypothetical production device. The local LED indicator is
therefore the production-facing way to communicate lifecycle state without a phone.

### Pins
The I2C SCL pin is 27, and the SDA pin is 26. The EK1940 uses pin 32 for analog input.

### Libraries
This project uses [Thomascountz's HD44780 LCD Controller Interface](https://github.com/Thomascountz/micropython_i2c_lcd)

BLE provisioning uses `aioble`. Install it on the device with:

```sh
mpremote connect <port> mip install aioble
```

The optional host provisioning client uses
[Bleak](https://bleak.readthedocs.io/). Install it on the host with:

```sh
python3 -m pip install bleak
```

### Config

Set the non-secret backend hostname in `config.py` as `API_HOST`. Wi-Fi
credentials are provisioned over BLE and stored in ESP32 NVS; they are not
source configuration.

On boot, a device with stored credentials imports the running application and
never imports or starts BLE. A device without credentials imports only the
minimal provisioning graph and advertises as `PlanterPal`; the display,
sensors, running NeoPixel controller, HTTP client, and reporting graph are not
imported. Credentials are committed only after Wi-Fi association and DHCP succeed. After the BLE
success indication is acknowledged, provisioning shuts down and resets the
machine. The credentialed reboot then enters running mode.

The running LED controller and NeoPixel are excluded from provisioning. Once
the BLE service has registered, a separate hardware-PWM owner slowly blinks the
simple GPIO2 status LED. PWM provides the visible pattern without another
asyncio task, and it is deinitialized before BLE cleanup or reset.

Every boot begins with a five-second cooperative recovery period before NVS,
BLE, Wi-Fi, display, or application state starts. Some CP2102 adapters reset
the ESP32 when the serial port opens, so enter raw REPL during that window by
delaying the `mpremote` command for less than five seconds:

```sh
mpremote connect <port> sleep 1 fs ls
mpremote connect <port> sleep 1 reset
```

Before installing `main.py` on a freshly prepared board, copy the supporting
modules and exercise the Wi-Fi-first, import-isolated provisioning composition:

```sh
mpremote connect <port> run tests/hardware/application_composition_hardware_probe.py
```

The provisioning runtime allocates one inactive station handle before reserving
the ESP32 BLE controller. It then imports only the provisioning dependencies.
`NetworkManager` receives that exact handle and remains the only component that
mutates Wi-Fi state. Keep this order and the reset boundary intact: NimBLE may
fail to allocate its controller heap if the running application is loaded in
the same interpreter.

A missing, corrupt, wrong-type, or oversized credential record is treated as
an unprovisioned device and exposes BLE provisioning. This recovery-open policy
avoids permanently stranding a device with unreadable NVS, but it reinforces
the requirement that provisioning be used only in a physically trusted
environment.

#### BLE provisioning protocol

- Service: `2bd127f3-ea4c-48f2-8234-32bf0660aecb`
- Command characteristic (write): `f4320080-4ba2-4307-918a-b49e9a1dbff5`
- Status characteristic (read, notify, indicate):
  `7d26a2f2-f4df-4dc3-8c49-078ca1c9b1ec`

Before sending credentials, the central must enable status notifications and
indications. The firmware configures a preferred MTU of 259, which accommodates
its 256-byte command limit. The negotiated MTU must be at least the encoded command length plus 3 bytes;
the central still initiates the exchange, normally through its host Bluetooth
stack during connection.

Send exactly one complete UTF-8 JSON value with a write-with-response. Do not
split the JSON over independent characteristic writes. A compact command is:

```json
{"type":"wifi_credentials","ssid":"Garden WiFi","password":"secret"}
```

The JSON payload must be at most 256 bytes. SSID and password limits are 32
and 64 UTF-8 bytes respectively; open networks use an empty password. Clients
should emit UTF-8 directly rather than ASCII-escaping non-ASCII credentials.

The status characteristic contains JSON with `status` and, for failures, a
safe `reason`. `ready` and `testing` are notifications. Terminal `success`,
`error`, and `invalid` values are indications; the firmware waits for the ATT
acknowledgment before closing provisioning on success or accepting another
command after an error. If valid credentials were already committed but that
final indication is lost, the device keeps the durable credentials and enters
running mode. Outbound status updates fit the default 20-byte ATT payload. An
`error` or `invalid` indication is therefore a compact status token; read the
status characteristic before sending another command to obtain its full safe
`reason`.

Provisioning is currently unauthenticated and unencrypted. Use it only in a
trusted, physically controlled environment; pairing or proof-of-possession is
required before treating this as a production-secure enrollment channel. NVS
storage is also plaintext unless flash/NVS encryption is enabled in the
firmware and partition configuration.

An ordinary access-point outage does not automatically expose provisioning.
To deliberately clear credentials and provision again:

```sh
mpremote connect <port> run tests/hardware/reset_credentials_hardware.py
mpremote connect <port> reset
```

Provision from a host after the uncredentialed reboot. The default mode prompts
for the password without echo or shell-history exposure:

```sh
python3 tools/ble_provision_client.py --ssid "Garden WiFi"
```

For an open network, pass `--open`. If name-based discovery is ambiguous, pass
`--address` with the BLE address (or the CoreBluetooth device UUID on macOS).
The client prints `ready`, `testing`, and the terminal result but never prints
the submitted password.

### Migration test sequence

Run the host suite first:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

Install `aioble`, then copy the supporting files to the board before copying
`main.py`:

```sh
mpremote connect <port> fs cp app/provisioning_runtime.py :app/provisioning_runtime.py
mpremote connect <port> fs cp app/provisioning.py :app/provisioning.py
mpremote connect <port> fs cp app/application.py :app/application.py
mpremote connect <port> fs cp config.py :config.py
mpremote connect <port> fs cp device_hardware.py :device_hardware.py
mpremote connect <port> fs cp display/null_display.py :display/null_display.py
mpremote connect <port> fs cp display/probe.py :display/probe.py
mpremote connect <port> fs cp led/provisioning_indicator.py :led/provisioning_indicator.py
mpremote connect <port> fs cp lib/ble_bootstrap.py :lib/ble_bootstrap.py
mpremote connect <port> fs cp lib/ble_provisioning.py :lib/ble_provisioning.py
mpremote connect <port> fs cp web/client.py :web/client.py
mpremote connect <port> fs cp web/credentials.py :web/credentials.py
mpremote connect <port> fs cp web/exceptions.py :web/exceptions.py
mpremote connect <port> fs cp web/network_config.py :web/network_config.py
mpremote connect <port> fs cp web/reporter.py :web/reporter.py
mpremote connect <port> fs cp web/wifi.py :web/wifi.py
mpremote connect <port> run tests/hardware/reset_credentials_hardware.py
mpremote connect <port> run tests/hardware/application_composition_hardware_probe.py
mpremote connect <port> run tests/hardware/optional_display_hardware_probe.py
```

Only after the required hardware scripts pass, install and reset the entry
point:

```sh
mpremote connect <port> fs cp main.py :main.py
mpremote connect <port> reset
python3 tools/ble_provision_client.py --ssid "Garden WiFi"
```

Expected result: while uncredentialed, the device slowly blinks GPIO2,
advertises over BLE, tests one credential candidate at a time, persists only a
connected candidate, acknowledges success, and resets. After the reset it stops advertising and runs
the normal display/sensor/reporting application. A later Wi-Fi outage remains
inside `NetworkManager`'s reconnect loop and does not initialize BLE or reset
the machine.

The legacy gitignored `web/wifi_config.py` is read only as a backend-host
fallback for existing deployments. New deployments should use `API_HOST`.

`config.py` contains side-effect-free hardware assignments and policy values.
`device_hardware.py` constructs the GPIO and I2C objects only when the running
application is imported; provisioning can therefore read `STATUS_LED_PIN`
without initializing the debug LCD bus.

#### Optional debug LCD

Running mode checks for the LCD address once during each boot while holding the
same I2C lock used by the display and BH1750. If the address is absent, the
application selects a lifecycle-compatible `NullDisplay` and continues with
Wi-Fi, sensing, reporting, and NeoPixel behavior unchanged. If the address is
present but LCD initialization raises `OSError`, the failed display task is
settled before the application continues headless. A failed I2C bus scan,
unexpected initialization exception, or LCD failure after initialization
remains fatal so a shared-bus or software fault is not mistaken for an absent
debug accessory.

LCD selection is fixed for that running-mode boot. Power the board off before
attaching or removing I2C devices, then boot again; live hot-plugging is not
supported. Headless selection is a functional fallback, not a memory-saving
mode, because the running application still imports the real display stack.

#### Backend HTTP policy

Each HTTP request has one monotonic 10-second deadline configured by
`HTTP_REQUEST_TIMEOUT_S` in `config.py`. Connection establishment, request
drain, response status and headers, and cooperative writer shutdown all consume
that same budget; a new phase does not restart the timer. Deadline expiry
closes the writer best-effort and raises typed `ErrTimedOut`, while external
task cancellation remains `asyncio.CancelledError`.

Both `/healthz` and `/api/v1/readings` accept only `2xx` responses. A `4xx` or
`5xx` raises `ErrHttpStatus`, which records only the status code and never the
response body or submitted payload. A rejected health check prevents running
mode and turns the NeoPixel red. A rejected reading is discarded exactly once,
like other failed deliveries, and the reporter remains available for the next
single-slot payload. Because the server was reachable, rejection does not turn
off the separate backend-reachability GPIO; transport failures and timeouts do.

## Architecture

Notes on the architecture can be found in the docs/ directory
