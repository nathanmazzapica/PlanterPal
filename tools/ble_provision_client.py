#!/usr/bin/env python3
"""Provision a PlanterPal from a host computer using Bleak.

The password is prompted without echo by default so it does not land in shell
history. This is a host-side utility; do not copy it to the ESP32.
"""

import argparse
import asyncio
import getpass
import json
import sys


# Keep these stable identifiers synchronized with lib/ble_provisioning.py.
SERVICE_UUID = "2bd127f3-ea4c-48f2-8234-32bf0660aecb"
COMMAND_CHAR_UUID = "f4320080-4ba2-4307-918a-b49e9a1dbff5"
STATUS_CHAR_UUID = "7d26a2f2-f4df-4dc3-8c49-078ca1c9b1ec"
DEFAULT_DEVICE_NAME = "PlanterPal"
MAX_PAYLOAD_BYTES = 256
MAX_SSID_BYTES = 32
MAX_PASSWORD_BYTES = 64
TERMINAL_STATUSES = {"success", "error", "invalid"}


def build_command(ssid, password):
    if not isinstance(ssid, str) or not ssid:
        raise ValueError("SSID must be a non-empty string")
    if len(ssid.encode("utf-8")) > MAX_SSID_BYTES:
        raise ValueError("SSID exceeds 32 UTF-8 bytes")
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError("password exceeds 64 UTF-8 bytes")

    payload = json.dumps(
        {
            "type": "wifi_credentials",
            "ssid": ssid,
            "password": password,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("credential command exceeds 256 UTF-8 bytes")
    return payload


def decode_status(raw):
    try:
        status = json.loads(bytes(raw).decode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("device returned an invalid status payload") from error

    if not isinstance(status, dict) or not isinstance(status.get("status"), str):
        raise ValueError("device returned an invalid status object")
    return status


async def find_device(scanner, address, name, timeout):
    if address:
        return await scanner.find_device_by_address(address, timeout=timeout)
    return await scanner.find_device_by_name(name, timeout=timeout)


async def wait_for_terminal_status(client, status_queue, disconnected, timeout):
    async def wait_loop():
        while True:
            status_task = asyncio.create_task(status_queue.get())
            disconnect_task = asyncio.create_task(disconnected.wait())
            done, pending = await asyncio.wait(
                (status_task, disconnect_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            if disconnect_task in done and disconnected.is_set():
                if status_task in done:
                    status = status_task.result()
                    if status.get("status") in TERMINAL_STATUSES:
                        return status
                raise ConnectionError(
                    "device disconnected before reporting a terminal result"
                )

            status = status_task.result()
            state = status["status"]
            print("device status:", state)
            if state == "protocol_error":
                raise ValueError(status.get("reason", "invalid BLE status"))
            if state not in TERMINAL_STATUSES:
                continue

            # Error/invalid indications are compacted to fit the default ATT
            # payload. The full safe reason remains readable on the server.
            if state != "success" and "reason" not in status:
                status = decode_status(
                    await client.read_gatt_char(STATUS_CHAR_UUID)
                )
            return status

    return await asyncio.wait_for(wait_loop(), timeout=timeout)


async def provision(args, password):
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as error:
        raise RuntimeError(
            "Bleak is not installed; run: python3 -m pip install bleak"
        ) from error

    payload = build_command(args.ssid, password)
    device = await find_device(
        BleakScanner,
        args.address,
        args.name,
        args.scan_timeout,
    )
    if device is None:
        target = args.address or args.name
        raise RuntimeError("PlanterPal BLE device not found: " + target)

    disconnected = asyncio.Event()
    statuses = asyncio.Queue()

    def on_disconnect(_client):
        disconnected.set()

    def on_status(_characteristic, raw):
        try:
            statuses.put_nowait(decode_status(raw))
        except ValueError as error:
            statuses.put_nowait({"status": "protocol_error", "reason": str(error)})

    async with BleakClient(
        device,
        disconnected_callback=on_disconnect,
        timeout=args.connect_timeout,
    ) as client:
        service = client.services.get_service(SERVICE_UUID)
        if service is None:
            raise RuntimeError("PlanterPal provisioning service was not found")

        await client.start_notify(STATUS_CHAR_UUID, on_status)
        initial = decode_status(await client.read_gatt_char(STATUS_CHAR_UUID))
        print("connected; device status:", initial["status"])
        await client.write_gatt_char(
            COMMAND_CHAR_UUID,
            payload,
            response=True,
        )
        result = await wait_for_terminal_status(
            client,
            statuses,
            disconnected,
            args.result_timeout,
        )

    if result["status"] != "success":
        reason = result.get("reason", "unknown")
        raise RuntimeError(
            "provisioning rejected: {} ({})".format(result["status"], reason)
        )
    print("PASS credentials verified and persisted; device is rebooting")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssid", required=True, help="Wi-Fi SSID")
    password_group = parser.add_mutually_exclusive_group()
    password_group.add_argument(
        "--open",
        action="store_true",
        help="use an empty password for an open network",
    )
    password_group.add_argument(
        "--password-stdin",
        action="store_true",
        help="read one password line from standard input",
    )
    parser.add_argument(
        "--address",
        help="BLE address or macOS device UUID; skips name-based discovery",
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_DEVICE_NAME,
        help="advertised device name (default: %(default)s)",
    )
    parser.add_argument("--scan-timeout", type=float, default=15.0)
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--result-timeout", type=float, default=45.0)
    return parser.parse_args(argv)


def read_password(args):
    if args.open:
        return ""
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    return getpass.getpass("Wi-Fi password: ")


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(provision(args, read_password(args)))
    except (ValueError, ConnectionError, RuntimeError, TimeoutError) as error:
        print("ERROR:", error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
