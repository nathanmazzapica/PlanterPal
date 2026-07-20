"""Minimal hardware-PWM indicator for the provisioning runtime."""

from machine import PWM, Pin


BLINK_FREQUENCY_HZ = 1
BLINK_DUTY_U16 = 13_000


class ProvisioningIndicator:
    """Own one GPIO LED while the device is in provisioning mode."""

    def __init__(self, pin_number):
        if not isinstance(pin_number, int) or isinstance(pin_number, bool):
            raise TypeError("pin_number must be an integer")

        self._pin = Pin(pin_number, Pin.OUT, value=0)
        self._pwm = None

    @property
    def running(self):
        return self._pwm is not None

    def start(self):
        if self._pwm is not None:
            return

        try:
            pwm = PWM(
                self._pin,
                freq=BLINK_FREQUENCY_HZ,
                duty_u16=BLINK_DUTY_U16,
            )
        except BaseException:
            try:
                self._pin.off()
            except Exception:
                pass
            raise

        self._pwm = pwm

    def stop(self):
        pwm = self._pwm
        self._pwm = None
        first_error = None

        if pwm is not None:
            try:
                pwm.duty_u16(0)
            except Exception as error:
                first_error = error

            try:
                pwm.deinit()
            except Exception as error:
                if first_error is None:
                    first_error = error

        try:
            self._pin.off()
        except Exception as error:
            if first_error is None:
                first_error = error

        if first_error is not None:
            raise first_error
