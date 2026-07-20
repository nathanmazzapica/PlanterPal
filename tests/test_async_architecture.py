import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BH1750_PATH = PROJECT_ROOT / "lib" / "bh1750.py"
LIGHT_PATH = PROJECT_ROOT / "sensors" / "light.py"
STATE_PATH = PROJECT_ROOT / "app" / "state.py"
APPLICATION_PATH = PROJECT_ROOT / "app" / "application.py"
CONFIG_PATH = PROJECT_ROOT / "config.py"
DEVICE_HARDWARE_PATH = PROJECT_ROOT / "device_hardware.py"
DISPLAY_PATH = PROJECT_ROOT / "display" / "display.py"
NULL_DISPLAY_PATH = PROJECT_ROOT / "display" / "null_display.py"
DISPLAY_PROBE_PATH = PROJECT_ROOT / "display" / "probe.py"


def parse(path):
    return ast.parse(path.read_text(), filename=str(path))


def import_symbols(tree):
    symbols = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                symbols[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                symbols[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return symbols


def qualified_name(node, symbols):
    if isinstance(node, ast.Name):
        return symbols.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = qualified_name(node.value, symbols)
        if base is not None:
            return f"{base}.{node.attr}"
    return None


def function_named(tree, name):
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def assigned_name(node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def assigned_value(node):
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        return node.value
    return None


class AsyncArchitectureTests(unittest.TestCase):
    def test_bh1750_driver_has_no_resolved_blocking_sleep_call(self):
        tree = parse(BH1750_PATH)
        symbols = import_symbols(tree)
        violations = []
        lux = function_named(tree, "lux")

        blocking_calls = {
            "time.sleep",
            "time.sleep_ms",
            "time.sleep_us",
            "utime.sleep",
            "utime.sleep_ms",
            "utime.sleep_us",
            "machine.lightsleep",
        }
        for node in ast.walk(lux):
            if not isinstance(node, ast.Call):
                continue
            name = qualified_name(node.func, symbols)
            if name in blocking_calls:
                violations.append((node.lineno, name))

        for node in ast.walk(lux):
            if isinstance(node, (ast.For, ast.While)) and not any(
                isinstance(descendant, ast.Await) for descendant in ast.walk(node)
            ):
                violations.append((node.lineno, type(node).__name__))

        self.assertEqual(
            violations,
            [],
            "BH1750 conversion must use a cooperative asyncio sleep",
        )

    def test_driver_and_config_do_not_construct_private_i2c_locks(self):
        violations = []
        for path in (BH1750_PATH, CONFIG_PATH):
            tree = parse(path)
            symbols = import_symbols(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = qualified_name(node.func, symbols)
                if name in {"asyncio.Lock", "uasyncio.Lock"}:
                    violations.append((path.name, node.lineno, name))

        self.assertEqual(
            violations,
            [],
            "the composition root, not the driver or config module, owns lock construction",
        )

    def test_sensor_pipeline_does_not_spawn_tasks(self):
        violations = []
        for path in (BH1750_PATH, LIGHT_PATH, STATE_PATH):
            tree = parse(path)
            symbols = import_symbols(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and qualified_name(node.func, symbols) in {
                    "asyncio.create_task",
                    "uasyncio.create_task",
                }:
                    violations.append((path.name, node.lineno))

        application_tree = parse(APPLICATION_PATH)
        application_symbols = import_symbols(application_tree)
        sensor_terms = {"lux", "light", "monitor", "sample", "sensor", "state"}
        for node in ast.walk(application_tree):
            if not isinstance(node, ast.Call):
                continue
            if qualified_name(node.func, application_symbols) not in {
                "asyncio.create_task",
                "uasyncio.create_task",
            }:
                continue
            target = qualified_name(node.args[0].func, application_symbols) if node.args and isinstance(node.args[0], ast.Call) else None
            if target and any(term in target.lower() for term in sensor_terms):
                violations.append((APPLICATION_PATH.name, node.lineno))

        self.assertEqual(
            violations,
            [],
            "light, moisture, and aggregate sampling remain one owned operation",
        )

    def test_composition_root_creates_and_injects_one_lock_for_sensor_bus(self):
        tree = parse(APPLICATION_PATH)
        symbols = import_symbols(tree)
        create_application = function_named(tree, "create_application")
        lock_bindings = set()

        for node in create_application.body:
            value = assigned_value(node)
            name = assigned_name(node)
            if name is None or not isinstance(value, ast.Call):
                continue
            if qualified_name(value.func, symbols) in {"asyncio.Lock", "uasyncio.Lock"}:
                lock_bindings.add(name)

        self.assertTrue(
            lock_bindings,
            "create_application must create the lock for the running sensor bus",
        )

        bus_calls = []
        injected_lock_names = []
        for node in ast.walk(create_application):
            if not isinstance(node, ast.Call):
                continue
            if qualified_name(node.func, symbols) != "lib.bh1750.BH1750":
                continue

            values = [*node.args, *(keyword.value for keyword in node.keywords)]
            bus_values = [
                value
                for value in values
                if qualified_name(value, symbols) == "device_hardware.SENSOR_BUS"
            ]
            if not bus_values:
                continue
            bus_calls.append(node)
            injected = {
                value.id
                for value in values
                if isinstance(value, ast.Name)
                and value.id in lock_bindings
            }
            self.assertEqual(
                len(injected),
                1,
                "every BH1750 using the running sensor bus must receive one composition-owned lock",
            )
            injected_lock_names.extend(injected)

        self.assertTrue(
            bus_calls,
            "create_application must construct BH1750 for the running sensor bus",
        )
        self.assertEqual(
            len(set(injected_lock_names)),
            1,
            "all BH1750 instances on the running sensor bus must share the same lock binding",
        )

    def test_application_run_loop_awaits_state_update(self):
        tree = parse(APPLICATION_PATH)
        symbols = import_symbols(tree)
        run_loop = function_named(tree, "_run_loop")
        awaited_state_updates = [
            node
            for node in ast.walk(run_loop)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and qualified_name(node.value.func, symbols) == "self.state.update"
        ]

        self.assertEqual(
            len(awaited_state_updates),
            1,
            "Application._run_loop must await exactly one aggregate State update per cycle",
        )

    def test_application_submits_serialized_state_to_reporter(self):
        tree = parse(APPLICATION_PATH)
        symbols = import_symbols(tree)
        run_loop = function_named(tree, "_run_loop")
        submissions = [
            node.value
            for node in ast.walk(run_loop)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and qualified_name(node.value.func, symbols) == "self.reporter.submit"
        ]

        self.assertEqual(len(submissions), 1)
        self.assertEqual(len(submissions[0].args), 1)
        payload = submissions[0].args[0]
        self.assertIsInstance(payload, ast.Call)
        self.assertEqual(
            qualified_name(payload.func, symbols),
            "self.state.to_json",
            "reporting must receive an immutable snapshot, not mutable State",
        )

    def test_reporting_cadence_condition_is_preserved(self):
        run_loop = function_named(parse(APPLICATION_PATH), "_run_loop")
        conditions = [
            node.test
            for node in ast.walk(run_loop)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.BinOp)
            and isinstance(node.test.op, ast.Mod)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "tick"
        ]

        self.assertEqual(len(conditions), 1)
        self.assertIsInstance(conditions[0].right, ast.Constant)
        self.assertEqual(
            conditions[0].right.value,
            5,
            "retain the existing `if tick % 5` reporting behavior",
        )

    def test_reporting_runs_as_one_application_owned_task(self):
        tree = parse(APPLICATION_PATH)
        symbols = import_symbols(tree)
        run = function_named(tree, "run")
        reporter_tasks = []

        for node in ast.walk(run):
            if not isinstance(node, ast.Call):
                continue
            if qualified_name(node.func, symbols) != "asyncio.create_task":
                continue
            if not node.args or not isinstance(node.args[0], ast.Call):
                continue
            if qualified_name(node.args[0].func, symbols) == "self.reporter.run":
                reporter_tasks.append(node)

        self.assertEqual(
            len(reporter_tasks),
            1,
            "Application must own exactly one reporter task",
        )

    def test_application_supervises_reporter_failures(self):
        tree = parse(APPLICATION_PATH)
        symbols = import_symbols(tree)
        run_loop = function_named(tree, "_run_loop")
        checks = [
            node
            for node in ast.walk(run_loop)
            if isinstance(node, ast.Call)
            and qualified_name(node.func, symbols) == "self.reporter.raise_if_failed"
        ]

        self.assertEqual(
            len(checks),
            1,
            "unexpected reporter failures must remain visible to Application",
        )

    def test_every_sensor_bus_owner_receives_the_same_composition_lock(self):
        tree = parse(APPLICATION_PATH)
        symbols = import_symbols(tree)
        create_application = function_named(tree, "create_application")
        lock_bindings = set()

        for node in create_application.body:
            value = assigned_value(node)
            name = assigned_name(node)
            if name is None or not isinstance(value, ast.Call):
                continue
            if qualified_name(value.func, symbols) == "asyncio.Lock":
                lock_bindings.add(name)

        self.assertEqual(
            len(lock_bindings),
            1,
            "the running sensor bus must have exactly one composition-owned lock",
        )

        injected = {}
        for node in ast.walk(create_application):
            if not isinstance(node, ast.Call):
                continue
            target = qualified_name(node.func, symbols)
            if target not in {
                "lib.bh1750.BH1750",
                "display.display.Display",
                "display.probe.LCDPresenceProbe",
            }:
                continue
            values = [*node.args, *(keyword.value for keyword in node.keywords)]
            if not any(
                qualified_name(value, symbols) == "device_hardware.SENSOR_BUS"
                for value in values
            ):
                continue
            injected[target] = {
                value.id
                for value in values
                if isinstance(value, ast.Name) and value.id in lock_bindings
            }

        self.assertEqual(
            injected,
            {
                "lib.bh1750.BH1750": lock_bindings,
                "display.display.Display": lock_bindings,
                "display.probe.LCDPresenceProbe": lock_bindings,
            },
            "BH1750, Display, and its presence probe must share one bus lock",
        )

    def test_null_display_has_no_hardware_or_i2c_dependency(self):
        tree = parse(NULL_DISPLAY_PATH)
        imports = set(import_symbols(tree).values())
        self.assertFalse(
            imports.intersection(
                {
                    "machine",
                    "device_hardware",
                    "display.display",
                    "lib.pcf8574",
                    "lib.hd44780",
                    "lib.lcd",
                }
            )
        )

        bus_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {"scan", "writeto", "readfrom", "readfrom_into", "writevto"}
        ]
        self.assertEqual(bus_calls, [])

    def test_config_is_side_effect_free_and_hardware_builds_running_devices(self):
        config_tree = parse(CONFIG_PATH)
        config_symbols = import_symbols(config_tree)
        config_calls = [
            qualified_name(node.func, config_symbols)
            for node in ast.walk(config_tree)
            if isinstance(node, ast.Call)
        ]
        self.assertNotIn("machine.Pin", config_calls)
        self.assertNotIn("machine.I2C", config_calls)
        self.assertNotIn("machine", set(import_symbols(config_tree).values()))

        hardware_tree = parse(DEVICE_HARDWARE_PATH)
        hardware_symbols = import_symbols(hardware_tree)
        hardware_calls = {
            qualified_name(node.func, hardware_symbols)
            for node in ast.walk(hardware_tree)
            if isinstance(node, ast.Call)
        }
        self.assertIn("machine.Pin", hardware_calls)
        self.assertIn("machine.I2C", hardware_calls)

    def test_application_awaits_all_display_submissions(self):
        tree = parse(APPLICATION_PATH)
        symbols = import_symbols(tree)
        application = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Application"
        )
        display_methods = {"write", "write_line", "display_err", "render"}
        calls = []
        awaited = set()

        for node in ast.walk(application):
            if isinstance(node, ast.Call):
                name = qualified_name(node.func, symbols)
                if name and name.startswith("self.display."):
                    if name.rsplit(".", 1)[-1] in display_methods:
                        calls.append(node)
            elif isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                awaited.add(id(node.value))

        self.assertTrue(calls)
        self.assertEqual(
            [call.lineno for call in calls if id(call) not in awaited],
            [],
            "Application must enqueue/await display work, never mutate LCD directly",
        )

    def test_display_render_receives_only_immutable_scalar_fields(self):
        tree = parse(APPLICATION_PATH)
        symbols = import_symbols(tree)
        run_loop = function_named(tree, "_run_loop")
        calls = [
            node
            for node in ast.walk(run_loop)
            if isinstance(node, ast.Call)
            and qualified_name(node.func, symbols) == "self.display.render"
        ]

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [qualified_name(argument, symbols) for argument in calls[0].args],
            [
                "self.state.lux_seconds",
                "self.state.moisture",
                "self.state.dli",
            ],
            "mutable State must not cross into the display task",
        )

    def test_application_owns_and_supervises_one_display_task(self):
        tree = parse(APPLICATION_PATH)
        symbols = import_symbols(tree)
        activate_display = function_named(tree, "_activate_display")
        run_loop = function_named(tree, "_run_loop")
        display_tasks = []

        for node in ast.walk(activate_display):
            if not isinstance(node, ast.Call):
                continue
            if qualified_name(node.func, symbols) != "self._create_task":
                continue
            if not node.args or not isinstance(node.args[0], ast.Call):
                continue
            if qualified_name(node.args[0].func, symbols) == "self.display.run":
                display_tasks.append(node)

        failure_checks = [
            node
            for node in ast.walk(run_loop)
            if isinstance(node, ast.Call)
            and qualified_name(node.func, symbols) == "self.display.raise_if_failed"
        ]
        self.assertEqual(len(display_tasks), 1)
        self.assertEqual(len(failure_checks), 1)

    def test_expected_cancellation_bypasses_display_error_marquee(self):
        tree = parse(APPLICATION_PATH)
        symbols = import_symbols(tree)

        for function_name in ("_connect_wifi", "_ping_server"):
            with self.subTest(function_name=function_name):
                function = function_named(tree, function_name)
                try_node = next(
                    node for node in function.body if isinstance(node, ast.Try)
                )
                self.assertTrue(try_node.handlers)
                self.assertEqual(
                    qualified_name(try_node.handlers[0].type, symbols),
                    "asyncio.CancelledError",
                )
                self.assertTrue(
                    any(isinstance(node, ast.Raise) for node in try_node.handlers[0].body)
                )

    def test_low_level_lcd_is_private_to_display_owner(self):
        application_tree = parse(APPLICATION_PATH)
        display_tree = parse(DISPLAY_PATH)

        application_imports = set(import_symbols(application_tree).values())
        self.assertFalse(
            application_imports.intersection(
                {"lib.pcf8574.PCF8574", "lib.hd44780.HD44780", "lib.lcd.LCD"}
            )
        )

        public_lcd_attributes = [
            node
            for node in ast.walk(display_tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr == "LCD"
        ]
        forbidden_marquee_calls = [
            node
            for node in ast.walk(display_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"marquee_text", "scroll_content_off_screen"}
        ]

        self.assertEqual(public_lcd_attributes, [])
        self.assertEqual(
            forbidden_marquee_calls,
            [],
            "Display must release the bus around its own cooperative marquee delay",
        )

if __name__ == "__main__":
    unittest.main()
