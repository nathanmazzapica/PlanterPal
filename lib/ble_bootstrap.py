DEFAULT_MAX_PAYLOAD_BYTES = 256
ATT_WRITE_OVERHEAD_BYTES = 3
MAX_ATT_MTU = 517


def prepare_ble_controller(
    max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
    aioble_module=None,
):
    """Reserve the aioble controller before the larger app graph is imported.

    ESP32 NimBLE startup needs a sizeable contiguous heap allocation. The
    entry point calls this only for an uncredentialed boot, after collecting
    transient credential-read garbage and before importing the much larger
    application graph. BleProvisioner repeats the idempotent configuration
    when it assumes lifecycle ownership.
    """

    if not isinstance(max_payload_bytes, int) or isinstance(
        max_payload_bytes, bool
    ):
        raise TypeError("max_payload_bytes must be an integer")
    if max_payload_bytes <= 0:
        raise ValueError("max_payload_bytes must be positive")
    if max_payload_bytes + ATT_WRITE_OVERHEAD_BYTES > MAX_ATT_MTU:
        raise ValueError("max_payload_bytes exceeds the ATT MTU limit")

    if aioble_module is None:
        import aioble as aioble_module

    config = getattr(aioble_module, "config", None)
    if not callable(config):
        raise RuntimeError("aioble.config is unavailable")

    try:
        config(mtu=max_payload_bytes + ATT_WRITE_OVERHEAD_BYTES)
    except Exception:
        # aioble.config() activates the controller before applying the MTU. A
        # failed allocation/configuration can therefore still need teardown.
        # Preserve the configuration failure if that best-effort cleanup also
        # fails; it is the useful root cause for a failed boot.
        try:
            release_ble_controller(aioble_module)
        except Exception:
            pass
        raise


def release_ble_controller(aioble_module=None):
    """Release an early BLE reservation; safe after provisioner shutdown."""

    if aioble_module is None:
        import aioble as aioble_module

    stop = getattr(aioble_module, "stop", None)
    if callable(stop):
        stop()
