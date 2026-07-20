import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDICATOR_PATH = PROJECT_ROOT / "led" / "provisioning_indicator.py"


class FakePin:
    OUT = object()
    instances = []
    construct_error = None

    def __init__(self, number, mode, value=None):
        if self.construct_error is not None:
            raise self.construct_error
        self.number = number
        self.mode = mode
        self.initial_value = value
        self.off_calls = 0
        self.instances.append(self)

    def off(self):
        self.off_calls += 1


class FakePWM:
    instances = []
    construct_error = None

    def __init__(self, pin, **kwargs):
        if self.construct_error is not None:
            raise self.construct_error
        self.pin = pin
        self.kwargs = kwargs
        self.duty_calls = []
        self.deinit_calls = 0
        self.instances.append(self)

    def duty_u16(self, value):
        self.duty_calls.append(value)

    def deinit(self):
        self.deinit_calls += 1


def import_indicator():
    machine = types.ModuleType("machine")
    machine.Pin = FakePin
    machine.PWM = FakePWM
    previous = sys.modules.get("machine")
    sys.modules["machine"] = machine
    try:
        spec = importlib.util.spec_from_file_location(
            "provisioning_indicator_under_test",
            INDICATOR_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("machine", None)
        else:
            sys.modules["machine"] = previous


indicator_module = import_indicator()


class ProvisioningIndicatorTests(unittest.TestCase):
    def setUp(self):
        FakePin.instances.clear()
        FakePin.construct_error = None
        FakePWM.instances.clear()
        FakePWM.construct_error = None

    def test_start_uses_one_hz_hardware_pwm_without_an_async_task(self):
        indicator = indicator_module.ProvisioningIndicator(2)
        indicator.start()

        self.assertTrue(indicator.running)
        self.assertEqual(len(FakePWM.instances), 1)
        self.assertIs(FakePWM.instances[0].pin, FakePin.instances[0])
        self.assertEqual(
            FakePWM.instances[0].kwargs,
            {"freq": 1, "duty_u16": 13_000},
        )

        tree = ast.parse(INDICATOR_PATH.read_text(), filename=str(INDICATOR_PATH))
        task_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_task"
        ]
        self.assertEqual(task_calls, [])

    def test_start_and_stop_are_idempotent(self):
        indicator = indicator_module.ProvisioningIndicator(2)
        indicator.start()
        indicator.start()
        pwm = FakePWM.instances[0]

        indicator.stop()
        indicator.stop()

        self.assertFalse(indicator.running)
        self.assertEqual(len(FakePWM.instances), 1)
        self.assertEqual(pwm.duty_calls, [0])
        self.assertEqual(pwm.deinit_calls, 1)
        self.assertEqual(FakePin.instances[0].off_calls, 2)

    def test_pwm_construction_failure_leaves_pin_off_and_is_retryable(self):
        indicator = indicator_module.ProvisioningIndicator(2)
        FakePWM.construct_error = OSError("PWM unavailable")

        with self.assertRaisesRegex(OSError, "PWM unavailable"):
            indicator.start()

        self.assertFalse(indicator.running)
        self.assertEqual(FakePin.instances[0].off_calls, 1)

        FakePWM.construct_error = None
        indicator.start()
        self.assertTrue(indicator.running)

    def test_pin_number_validation_precedes_hardware_construction(self):
        for value in (True, 2.0, "2", None):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    indicator_module.ProvisioningIndicator(value)
        self.assertEqual(FakePin.instances, [])


if __name__ == "__main__":
    unittest.main()
