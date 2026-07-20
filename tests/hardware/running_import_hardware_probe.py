"""Verify the complete credentialed graph from deployed support files.

The deployment tool runs this before copying ``main.py``. It composes the real
running graph without starting tasks or touching credential persistence, then
proves that BLE/provisioning modules were not imported.
"""

import sys

from app.application import create_application
from web.credentials import Credentials


FORBIDDEN_RUNNING_IMPORTS = (
    "aioble",
    "bluetooth",
    "lib.ble_bootstrap",
    "lib.ble_provisioning",
    "app.provisioning",
    "app.provisioning_runtime",
)


application = create_application(Credentials("manifest-probe", ""))

loaded_forbidden = [
    name for name in FORBIDDEN_RUNNING_IMPORTS if name in sys.modules
]
if loaded_forbidden:
    raise AssertionError(
        "running graph imported provisioning dependencies: {}".format(
            loaded_forbidden
        )
    )

assert application.network_manager.has_credentials
assert application.display._bus_lock is application._display_probe._bus_lock
assert application.display._bus_lock is application.state.LIGHT_MONITOR._sensor.I2C_LOCK

print("PASS credentialed running graph composed from deployed support files")
print("PASS running graph did not import BLE or provisioning modules")
