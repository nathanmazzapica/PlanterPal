import network
import time
from web.wifi_config import cfg

def connect_wifi():
    ssid = cfg['ssid']
    password = cfg['pw']
    
    wlan = network.WLAN(network.STA_IF)
    
    if not wlan.isconnected():
        print('connecting to network...')
        wlan.active(True)
        wlan.connect(ssid, password)
        
        # Wait for the connection to establish
        while not wlan.isconnected():
            time.sleep(0.5)
            print('not connected')
            print('.', end='')
            
    print('\nnetwork config:', wlan.ifconfig())

