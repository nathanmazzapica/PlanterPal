import ast
import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = PROJECT_ROOT / "lib"
HD44780_PATH = LIB_ROOT / "hd44780.py"
PCF8574_PATH = LIB_ROOT / "pcf8574.py"


def import_hd44780_module():
    module_names = (
        "utime",
        "hd44780_4bit_driver",
        "hd44780_4bit_payload",
        "hd44780",
    )
    old_modules = {name: sys.modules.get(name) for name in module_names}
    old_path = list(sys.path)
    utime = types.ModuleType("utime")
    utime.sleep_us = lambda delay: None
    utime.sleep_ms = lambda delay: None
    sys.modules["utime"] = utime
    sys.path.insert(0, str(LIB_ROOT))
    sys.modules.pop("hd44780", None)

    try:
        return importlib.import_module("hd44780")
    finally:
        sys.path[:] = old_path
        for name, previous in old_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


hd44780_module = import_hd44780_module()


class CancellingDriver:
    def __init__(self):
        self.pulses = []
        self._scheduled = False

    def write(self, payload):
        self.pulses.append((payload.e, payload.rs, payload.data))
        if not self._scheduled:
            self._scheduled = True
            task = asyncio.current_task()
            asyncio.get_running_loop().call_soon(task.cancel)


class DisplayProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def assert_cancellation_after_atomic_call(self, operation):
        async def worker():
            operation()
            await asyncio.sleep(0)

        task = asyncio.create_task(worker())
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_nibble_cannot_be_cancelled_between_enable_edges(self):
        driver = CancellingDriver()
        hd = hd44780_module.HD44780(driver, num_lines=2, num_columns=16)

        await self.assert_cancellation_after_atomic_call(
            lambda: hd._write_nibble(0x0A)
        )

        self.assertEqual(
            driver.pulses,
            [(1, 0, 0x0A), (0, 0, 0x0A)],
        )

    async def test_byte_cannot_be_cancelled_between_nibbles(self):
        driver = CancellingDriver()
        hd = hd44780_module.HD44780(driver, num_lines=2, num_columns=16)

        await self.assert_cancellation_after_atomic_call(
            lambda: hd._write_byte(0xAB)
        )

        self.assertEqual(
            driver.pulses,
            [
                (1, 1, 0x0A),
                (0, 1, 0x0A),
                (1, 1, 0x0B),
                (0, 1, 0x0B),
            ],
        )


class DisplayProtocolStructureTests(unittest.TestCase):
    def function(self, path, class_name, function_name):
        tree = ast.parse(path.read_text(), filename=str(path))
        class_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        return next(
            node
            for node in class_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        )

    def test_no_await_can_split_hd44780_protocol_edges(self):
        for function_name in ("_write_nibble", "_write_byte"):
            with self.subTest(function_name=function_name):
                function = self.function(
                    HD44780_PATH,
                    "HD44780",
                    function_name,
                )
                self.assertIsInstance(function, ast.FunctionDef)
                self.assertFalse(
                    any(isinstance(node, ast.Await) for node in ast.walk(function)),
                    "cancellation must not split enable edges or byte nibbles",
                )

    def test_pcf8574_bus_write_remains_one_bounded_atomic_operation(self):
        function = self.function(PCF8574_PATH, "PCF8574", "_write_byte")

        self.assertIsInstance(function, ast.FunctionDef)
        self.assertFalse(any(isinstance(node, ast.Await) for node in ast.walk(function)))
        sleep_calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sleep_ms"
        ]
        self.assertEqual(len(sleep_calls), 1)
        self.assertEqual(sleep_calls[0].args[0].value, 1)


if __name__ == "__main__":
    unittest.main()
