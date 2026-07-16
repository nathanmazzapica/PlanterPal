import asyncio
import network
from web.wifi_config import cfg

async def connect_wifi():
    ssid = cfg['ssid']
    password = cfg['pw']

    wlan = network.WLAN(network.STA_IF)

    if not wlan.isconnected():
        print('connecting to network...')
        wlan.active(True)
        wlan.connect(ssid, password)

        # Wait for the connection to establish, yielding to the event loop.
        while not wlan.isconnected():
            await asyncio.sleep(0.5)
            print('not connected')
            print('.', end='')

    print('\nnetwork config:', wlan.ifconfig())
