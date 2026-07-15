import aioble
import bluetooth
import asyncio
import json
from micropython import const

SERVICE_UUID = bluetooth.UUID("2bd127f3-ea4c-48f2-8234-32bf0660aecb")
COMMAND_CHAR_UUID = bluetooth.UUID("f4320080-4ba2-4307-918a-b49e9a1dbff5")

ADV_INTERVAL = const(250_000)

service = aioble.Service(SERVICE_UUID)
command_char = aioble.Characteristic(
    service,
    COMMAND_CHAR_UUID,
    write=True,
    capture=True,
    initial=bytearray(256)
)

aioble.register_services(service)

async def run_provisioning(credentials_channel):
    while True:
        connection = await aioble.advertise(
            ADV_INTERVAL,
            name="PlanterPal",
            services=[SERVICE_UUID],
        )  # pyright: ignore[reportAssignmentType]

        while connection.is_connected():
            _, raw_data = await command_char.written()  # pyright: ignore[reportGeneralTypeIssues]
            print(raw_data)
            print(raw_data.decode())

            command = json.loads(raw_data)
            print(command)

            if command.get("type") == "wifi_credentials":
                await credentials_channel.put(
                    (
                        command["ssid"],
                        command["password"],
                    )
                )
