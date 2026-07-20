"""Clear PlanterPal Wi-Fi credentials from ESP32 NVS.

Run with ``mpremote connect <port> run tests/hardware/reset_credentials_hardware.py``.
The script does not reset the board; reset it after the PASS message to enter
the provisioning branch.
"""

import os

from web.credentials import CredentialStore


def run():
    implementation = os.uname()
    print("Firmware implementation:", implementation)

    store = CredentialStore()
    if store.load() is None:
        print("PASS no valid credentials were stored")
        return

    store.clear()
    if CredentialStore().load() is not None:
        raise AssertionError("credentials remained after clear/reopen")

    print("PASS credentials cleared and verified after reopening NVS")
    print("Reset the board to enter BLE provisioning mode")


run()
